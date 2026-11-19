"""Client-key store: who may call RIPPLE, and what they may call (FA-15, AS-10).

Why this module exists: RIPPLE used to forward every request without looking at
`Authorization` at all. Measured 14.11.2026 against the running service: a plain
`POST http://localhost:8010/v1/chat/completions` with **no** auth header answered
`200`, while oMLX itself answers `401` for the same request. The proxy was
therefore an unauthenticated door in front of an authenticated backend, and the
open door was invisible in the logs because nothing ever compared a token.

The store is the missing comparison. It holds the client keys (layer A of §5.1:
hand-maintained, persistent, never written back by the request path) and answers
exactly two questions — *is this token a client key, and which one* — plus the
admin question. Wiring it into the proxy is a separate step; this module owns no
HTTP.

Two properties are load-bearing and easy to break:

* **Never log or print a key value.** Not in a docstring example, not in an
  exception message, not in `repr`. Every error raised here names the *key name*
  or the *path*, never the secret. A key that reached a log line would have to be
  rotated, and the log is the one place that survives a restart (FA-15, AS-10).
* **Never compare tokens with `==`.** `==` stops at the first differing byte and
  leaks, through timing, how much of a guessed token was right. Every comparison
  here goes through `hmac.compare_digest`, including the admin key — the admin
  key is the more valuable secret, so it gets the same treatment, not less.

The file is YAML so the operator can read and diff it, written atomically so a
crash mid-save cannot leave a half-written key store behind (an empty store would
lock every client out at once), and `0600` because the file is a list of secrets.
"""

from __future__ import annotations

import hmac
import math
import os
import secrets
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# The three traffic classes RIPPLE distinguishes. The kind is not enforcement by
# itself — it is what the placement and fairness rules read to tell an
# interactive request (a person waiting, FA-18) from a batch job (throughput,
# yields its slot) from a one-shot (no thread to keep warm).
KINDS = ("interactive", "batch", "one-shot")

_FILE_VERSION = 1


class KeyStoreError(Exception):
    """The key store cannot be used — unknown key, duplicate name, bad file.

    Messages carry the key *name* or the file *path*, never a key value: this
    exception ends up in logs and tracebacks, and a secret must not (FA-15).
    """


def _now() -> str:
    """Creation timestamp, ISO 8601 with an explicit UTC offset.

    Naive local time would be ambiguous across a DST switch and unorderable
    against a timestamp written before it; the offset costs nine characters.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_models(models: list[str] | None) -> list[str]:
    """Sorted, de-duplicated strings — or an empty list meaning "all models".

    Sorted because the file is read by humans and diffed by tools: an unsorted
    list reorders itself and makes every save look like a change. Empty is not
    "nothing allowed" but "no restriction", the same convention as a host's
    allow-list (`HostConfig.model_allowed`).
    """
    if not models:
        return []
    return sorted({str(m) for m in models})


def _same(a: str | None, b: str | None) -> bool:
    """Constant-time token comparison over UTF-8 bytes.

    Encoded because `hmac.compare_digest` refuses `str` containing non-ASCII —
    a client could otherwise turn a bad token into a `TypeError`/500 instead of
    a clean 401, and the 500 would be reachable without a key. The `isinstance`
    guard covers the same hole from the other side: an `Authorization` header
    parsed into something that is not a string must read as "wrong token", not
    as a traceback.
    """
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8", "surrogatepass"),
                               b.encode("utf-8", "surrogatepass"))


def _copy(k: ClientKey) -> ClientKey:
    """A row the caller may keep. `models` is copied too: `dataclasses.replace`
    is shallow, and a shared list would let a view mutate the store."""
    return replace(k, models=list(k.models))


@dataclass
class ClientKey:
    """One caller's credential and the traffic class it belongs to.

    `key` is the secret. Anything that renders this dataclass for an operator
    (dashboard, `/stats`, log line) must use `KeyStore.masked()` instead of
    `asdict()`/`repr()` — `repr` of a dataclass prints every field (FA-15).
    """

    name: str
    key: str
    kind: str = "interactive"        # interactive | batch | one-shot
    models: list[str] = field(default_factory=list)   # empty = all models
    enabled: bool = True
    created_at: str = ""             # ISO 8601, UTC
    created_by: str = ""
    note: str = ""

    def model_allowed(self, model: str) -> bool:
        """An empty list means: every model this deployment serves."""
        return not self.models or model in self.models

    def masked_key(self) -> str:
        """`first6…last4` — enough to tell two keys apart, not enough to use.

        Six and four characters of a 36-character `token_urlsafe(24)` secret
        leave 32 unknown characters; the point is that an operator can match a
        key to its row without the value ever being on screen.
        """
        if len(self.key) <= 10:
            return "…"
        return f"{self.key[:6]}…{self.key[-4:]}"


# Five wrong admin keys per minute from one client address (issue #6). The
# number is the spec, and the arithmetic behind it is worth keeping: the admin
# key is four digits by nature (`config.yaml`), so 10 000 guesses are a minute of
# work for anything that can reach :8010 — at five a minute they are a 33-hour
# job that also writes an `admin_blocked` line every window, i.e. an attack that
# has to stay visible for a day and a half. Low enough that an operator who
# fat-fingers the key twice still has room.
ADMIN_FAILURES_PER_MINUTE = 5
ADMIN_WINDOW_SECONDS = 60.0


class AdminRateLimiter:
    """Failed admin-key attempts per client address, in a rolling window.

    Why this is not part of `KeyStore`: the store answers *whether* a token is
    the admin key, and it must answer that question identically every time.
    Counting is a property of the caller — one address, one window — and it has
    to be consulted *before* the comparison (see `Ripple._require_admin`),
    otherwise the limiter does not limit guessing, it only relabels it.

    Time comes from `time.monotonic`, so a wall-clock jump (NTP, a manual
    `date`, a laptop waking up) can neither extend a block nor cut one short.
    Per-address state is a deque of the timestamps still inside the window and
    is pruned on every call: an address that stops guessing disappears with its
    last timestamp, so the dict cannot grow with every address that ever failed
    — the same shape as `ThreadRegistry._prune_ephemeral`.

    `address=None` (a request without a transport peer) shares one bucket under
    `"?"`. That is the honest reading: nothing distinguishes those requests, and
    treating them as unlimited would be a way around the limiter.
    """

    def __init__(self, limit: int = ADMIN_FAILURES_PER_MINUTE,
                 window_seconds: float = ADMIN_WINDOW_SECONDS) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window_seconds))
        self._failures: dict[str, deque[float]] = {}

    @staticmethod
    def _key(address: str | None) -> str:
        return address or "?"

    def _sweep(self, now: float) -> None:
        """Drop timestamps that have left the window, and empty buckets."""
        for address, stamps in list(self._failures.items()):
            while stamps and now - stamps[0] >= self.window:
                stamps.popleft()
            if not stamps:
                self._failures.pop(address, None)

    def retry_after(self, address: str | None, now: float | None = None) -> int | None:
        """Seconds to wait if `address` is over budget, otherwise `None`.

        At least 1: a `Retry-After: 0` would invite an immediate retry and
        describe a block that is already over. The value counts down as the
        oldest failure leaves the window, so a client that keeps hammering gets
        a shrinking number instead of a fixed 60.
        """
        now = time.monotonic() if now is None else now
        self._sweep(now)
        stamps = self._failures.get(self._key(address))
        if not stamps or len(stamps) < self.limit:
            return None
        return max(1, math.ceil(self.window - (now - stamps[0])))

    def note_failure(self, address: str | None, now: float | None = None) -> int | None:
        """Record one failed attempt; returns the retry delay if it used up the budget.

        A non-`None` return means *this* attempt was the last one before the
        block — the moment worth one log line, because the requests that follow
        are refused without reaching the comparison and without another line.
        """
        now = time.monotonic() if now is None else now
        self._sweep(now)
        stamps = self._failures.setdefault(self._key(address), deque())
        stamps.append(now)
        if len(stamps) < self.limit:
            return None
        return max(1, math.ceil(self.window - (now - stamps[0])))

    def clear(self, address: str | None) -> None:
        """Forget an address's failures — the correct key was presented.

        Only reachable while the address is *not* blocked, since a blocked one
        never gets compared. It keeps a stale near-limit counter from making the
        operator's next typo the one that blocks.
        """
        self._failures.pop(self._key(address), None)

    def tracked(self) -> int:
        """Addresses currently holding a non-empty window — for tests and triage."""
        return len(self._failures)


class KeyStore:
    """Client keys on disk, loaded once, consulted per request.

    The constructor loads the file immediately: a proxy that starts with a
    corrupt key store must fail at startup, not on the first request, and an
    unloaded store would answer "unknown token" to every valid client.
    """

    def __init__(self, path: str | Path, admin_key: str = "") -> None:
        self.path = Path(path).expanduser()
        # The constructor argument wins over the file: it is how the admin key
        # comes from the environment on a host where the file is shipped by a
        # deploy and must not carry the secret (FA-15, `${VAR}` expansion).
        self._admin_override = admin_key or ""
        self._admin_key = self._admin_override
        self.version = _FILE_VERSION
        self.keys: list[ClientKey] = []
        self.load()

    # -- file ---------------------------------------------------------------
    def load(self) -> None:
        """Read the YAML file. A missing or empty file is an empty store.

        Not raising on a missing file is deliberate: a fresh host has no key
        store yet, and the operator's first `create()` must work. A *corrupt*
        file is the opposite case — it raises with the path, because silently
        starting with zero keys would look like "every client lost its access"
        with no clue where to look.
        """
        self._admin_key = self._admin_override
        self.keys = []
        self.version = _FILE_VERSION

        if not self.path.is_file():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise KeyStoreError(f"cannot read key store {self.path}: {exc}") from exc
        if not text.strip():
            return
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise KeyStoreError(f"key store {self.path} is not valid YAML: {exc}") from exc
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise KeyStoreError(f"key store {self.path}: root must be a mapping")

        self.version = int(raw.get("version") or _FILE_VERSION)
        if not self._admin_override:
            self._admin_key = str(raw.get("admin_key") or "")

        entries = raw.get("keys") or []
        if not isinstance(entries, list):
            raise KeyStoreError(f"key store {self.path}: 'keys' must be a list")

        seen: set[str] = set()
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise KeyStoreError(f"key store {self.path}: keys[{idx}] must be a mapping")
            name = str(entry.get("name") or "").strip()
            key = str(entry.get("key") or "").strip()
            if not name:
                raise KeyStoreError(f"key store {self.path}: keys[{idx}] has no name")
            if not key:
                raise KeyStoreError(f"key store {self.path}: key {name!r} has no value")
            if name in seen:
                # Two rows with one name would make revoke()/rotate() act on an
                # arbitrary one of them.
                raise KeyStoreError(f"key store {self.path}: duplicate key name {name!r}")
            seen.add(name)
            kind = str(entry.get("kind") or "interactive").strip().lower()
            if kind not in KINDS:
                raise KeyStoreError(
                    f"key store {self.path}: key {name!r} has unknown kind {kind!r} "
                    f"(allowed: {', '.join(KINDS)})")
            self.keys.append(ClientKey(
                name=name,
                key=key,
                kind=kind,
                models=_norm_models(entry.get("models") or []),
                enabled=bool(entry.get("enabled", True)),
                created_at=str(entry.get("created_at") or ""),
                created_by=str(entry.get("created_by") or ""),
                note=str(entry.get("note") or ""),
            ))

    def save(self) -> None:
        """Write the store atomically with mode `0600`.

        tmp file + `os.replace` because the alternative — truncate then write —
        leaves a window in which a crash produces an empty or half-parsed store,
        which reads as "no client may call us". `os.replace` is atomic on the
        same filesystem, so a reader sees either the old file or the new one.

        `0600` because the file *is* the credentials: group/other readable would
        hand every local user the ability to impersonate every client. The
        temporary file is created by `mkstemp` (already 0600) and its mode
        survives the rename, so the secret is never world-readable, not even for
        the microseconds before the chmod.
        """
        payload = {
            "version": _FILE_VERSION,
            "admin_key": self._admin_key,
            "keys": [asdict(k) for k in self.keys],
        }
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True,
                              default_flow_style=False)

        parent = self.path.parent
        try:
            # First start on a fresh host: the state directory need not exist.
            parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(parent),
                                            prefix=f".{self.path.name}.",
                                            suffix=".tmp")
        except OSError as exc:
            raise KeyStoreError(f"cannot write key store {self.path}: {exc}") from exc

        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                # The rename is only as durable as the data behind it: without
                # fsync a power cut can leave the new name pointing at nothing.
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException as exc:
            tmp.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise KeyStoreError(f"cannot write key store {self.path}: {exc}") from exc
            raise
        os.chmod(self.path, 0o600)

    # -- authentication -----------------------------------------------------
    def check(self, token: str | None) -> ClientKey | None:
        """The client key behind `token`, or `None` if unknown or disabled.

        Every entry is compared, even after a match: returning early would make
        the response time depend on the position of the matching key in the
        file, which is a small leak but a free one to avoid. Disabled keys are
        compared too and rejected afterwards — a disabled key is still a secret
        and must not behave differently from a wrong one in any way the caller
        can observe beyond the 401 it gets either way.

        Never logs the token (FA-15).
        """
        if not isinstance(token, str) or not token:
            return None
        found: ClientKey | None = None
        for candidate in self.keys:
            if _same(candidate.key, token) and found is None:
                found = candidate
        if found is None or not found.enabled:
            return None
        return found

    def is_admin(self, token: str | None) -> bool:
        """True only for the configured admin key; False when none is set.

        No admin key configured means no admin — never "everything is admin".
        Same constant-time comparison as the client keys, for the same reason.
        """
        if not self._admin_key:
            return False
        return _same(self._admin_key, token)

    @property
    def admin_key_set(self) -> bool:
        """Is an admin key configured at all (constructor or file)?

        The value itself never leaves the store: this is the one question an
        operator tool may ask about it — `ripple doctor` reports "admin key
        set" or "no admin key" and never the secret (FA-15).
        """
        return bool(self._admin_key)

    # -- mutation -----------------------------------------------------------
    def _find(self, name: str) -> ClientKey | None:
        wanted = str(name).strip()
        for k in self.keys:
            if k.name == wanted:
                return k
        return None

    def _generate(self) -> str:
        """`rip-` + 24 random bytes, url-safe. The prefix makes a leaked key
        recognisable in a log or a paste, which is what lets it be revoked."""
        while True:
            key = "rip-" + secrets.token_urlsafe(24)
            if not any(_same(k.key, key) for k in self.keys):
                return key

    def create(self, name: str, kind: str = "interactive",
               models: list[str] | None = None, created_by: str = "",
               note: str = "") -> ClientKey:
        """Add a key and persist the store. Returns it **including the secret**.

        This is the only moment the caller may show the value: it is not
        recoverable from the file by anyone who should not have the file, and it
        must never be written to a log (FA-15). `created_at` is set here.

        Unknown kind or a name that already exists raises — silently fixing a
        typo'd kind would file the traffic under the wrong class, and silently
        overwriting a name would revoke a working client without saying so.
        """
        clean_name = str(name).strip()
        if not clean_name:
            raise KeyStoreError("key name must not be empty")
        clean_kind = str(kind).strip().lower()
        if clean_kind not in KINDS:
            raise KeyStoreError(
                f"unknown kind {kind!r} for key {clean_name!r} "
                f"(allowed: {', '.join(KINDS)})")
        if self._find(clean_name) is not None:
            raise KeyStoreError(f"key {clean_name!r} already exists")

        client = ClientKey(
            name=clean_name,
            key=self._generate(),
            kind=clean_kind,
            models=_norm_models(models),
            enabled=True,
            created_at=_now(),
            created_by=str(created_by or ""),
            note=str(note or ""),
        )
        self.keys.append(client)
        self.save()
        return _copy(client)

    def revoke(self, name: str) -> bool:
        """Remove the key entirely; `False` if there was no such name.

        Not "disable": revoking is the answer to a leaked secret, and a disabled
        row keeps the secret in the file where it can be re-enabled by accident.
        Idempotent on purpose — the operator revoking twice is not an error.
        """
        client = self._find(name)
        if client is None:
            return False
        self.keys.remove(client)
        self.save()
        return True

    def rotate(self, name: str) -> ClientKey:
        """Give an existing key a new secret, keeping name, kind and models.

        Rotation is the cheap answer to "this key may have leaked but the client
        is still legitimate": the identity in the logs and the routing class stay
        the same, only the credential changes. The old value stops working the
        moment this returns, because `save()` has already replaced the file.
        """
        client = self._find(name)
        if client is None:
            raise KeyStoreError(f"unknown key {str(name)!r}")
        client.key = self._generate()
        self.save()
        return _copy(client)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Turn a key on or off without forgetting it; `False` if unknown.

        The point of disabling rather than revoking is that the client can be
        brought back with the same secret, e.g. a batch job paused for a week.
        """
        client = self._find(name)
        if client is None:
            return False
        client.enabled = bool(enabled)
        self.save()
        return True

    def get(self, name: str) -> ClientKey | None:
        """The key row by name — secret included, so callers must not log it.

        Raises `KeyStoreError` for an unknown name. That is the explicit rule for
        this store (only `revoke` and `set_enabled` report "not there" with
        `False`), and it makes a typo in a name visible instead of producing a
        `None` that reads like "key exists but is disabled".
        """
        client = self._find(name)
        if client is None:
            raise KeyStoreError(f"unknown key {str(name)!r}")
        return _copy(client)

    # -- views --------------------------------------------------------------
    def list(self) -> list[ClientKey]:
        """All keys with their secrets — the admin view, never a public one.

        Returns copies: a caller mutating a returned row must not change the
        store's state behind `save()`'s back, and a `KeyStore` that changes
        without a write is a key store that lies after the next reload.
        """
        return [_copy(k) for k in self.keys]

    def masked(self) -> list[dict[str, Any]]:
        """The same rows with `key` reduced to `first6…last4`.

        This is the only representation that may leave the process — dashboard,
        `/stats`, `/keys` (FA-15, AS-10). Built by hand rather than `asdict()`
        so adding a field to `ClientKey` cannot leak it by default; the copy in
        `list()` exists for the opposite case, where the secret is wanted.
        """
        return [
            {
                "name": k.name,
                "key": k.masked_key(),
                "kind": k.kind,
                "models": list(k.models),
                "enabled": k.enabled,
                "created_at": k.created_at,
                "created_by": k.created_by,
                "note": k.note,
            }
            for k in self.keys
        ]
