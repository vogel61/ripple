"""Operator-declared model policy — switching one (host, model) pair off.

Two levels, and neither of them touches oMLX: no model is loaded, unloaded or
patched, RIPPLE only stops using it.

* ``disabled`` — RIPPLE does not dispatch this model to this host any more. The
  model stays in ``/v1/models`` as long as another host offers it, so a client
  that has it in its list keeps working; the request simply lands on the other
  machine.
* ``hidden`` — the model is not offered to clients at all: it disappears from
  ``/v1/models`` once **every** host that lists it hides it. Hermes and opencode
  then lose it from their own list, which is exactly what \"do not offer it to
  clients\" means — and why the dashboard has to ask before it sets one.

``hidden`` implies ``disabled``. Hiding a model while still routing to it would
answer a client that happens to know the id while telling every other client the
model does not exist — the one combination nobody can reason about.

**The lock** (issue #21) is the third operator decision, and it is a different
kind: `disabled`/`hidden` stop RIPPLE from *routing to* a model, the lock stops
RIPPLE from *unloading* one. It is keyed by the model alone, not by
(host, model): what the operator wants to protect is the availability of that
model wherever it is resident, and a per-host flag would be blind to the eviction
that a *request for another model* causes on exactly the host that holds it. So
one lock protects the model on every node it is loaded on, and every path that
empties a node (a dispatch that would evict, a calibration run, a replication
job) refuses while it is in force.

The state is **persisted** (``~/.ripple/models.yaml``, the maintenance store's
pattern). A model switched off for a reason — a broken quantisation, a licence
question, a machine that must not carry it — must not come back because a deploy
restarted RIPPLE. The same holds for the lock: a restart is not an operator
decision to release it. Only the operator endpoints write it; the balancer only
reads, so nothing here can appear on disk behind the operator's back.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("ripple.model_policy")

VERSION = 1
DEFAULT_FILE = "~/.ripple/models.yaml"

# `disabled` first: it is the weaker of the two and the default of the endpoint.
LEVELS = ("disabled", "hidden")


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.astimezone()


@dataclass
class Rule:
    """One (host, model) pair and what the operator decided about it."""

    host: str
    model: str
    level: str
    since: datetime = field(default_factory=_now)
    by: str = "operator"
    note: str = ""

    @property
    def blocks_dispatch(self) -> bool:
        """Both levels stop the dispatch; only ``hidden`` also stops the listing."""
        return self.level in LEVELS

    @property
    def hides(self) -> bool:
        return self.level == "hidden"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "model": self.model,
            "level": self.level,
            "since": self.since.isoformat(timespec="seconds"),
            "by": self.by,
            "note": self.note,
        }


@dataclass
class Lock:
    """One model the operator wants to stay resident, and why (issue #21).

    Keyed by the model id alone: the lock is about the model's availability, not
    about a machine. `note` is what the operator typed, and it is the text the
    dashboard and `/healthz` show next to the padlock.
    """

    model: str
    since: datetime = field(default_factory=_now)
    by: str = "operator"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "since": self.since.isoformat(timespec="seconds"),
            "by": self.by,
            "note": self.note,
        }


class ModelPolicy:
    """The rules, keyed by (host name, model id), and the locks by model id.

    Host **names** and model ids are what the operator sees in the dashboard and
    what the admin endpoints take — the same choice the maintenance store makes,
    and for the same reason: a rename is exactly when the rule has to be looked
    at again. The lock has no host in its key, because it is about the model.
    """

    def __init__(self, path: str | Path | None = None,
                 rules: dict[tuple[str, str], Rule] | None = None,
                 locks: dict[str, Lock] | None = None) -> None:
        self.path = Path(path).expanduser() if path else None
        self.rules: dict[tuple[str, str], Rule] = dict(rules or {})
        self.locks: dict[str, Lock] = dict(locks or {})

    # -- reads -------------------------------------------------------------
    def rule(self, host: str, model: str) -> Rule | None:
        return self.rules.get((host, model))

    def level(self, host: str, model: str) -> str | None:
        rule = self.rule(host, model)
        return rule.level if rule is not None else None

    def blocks(self, host: str, model: str) -> bool:
        """May RIPPLE dispatch `model` to `host`? (`disabled` and `hidden` say no)"""
        rule = self.rule(host, model)
        return rule is not None and rule.blocks_dispatch

    def hides(self, host: str, model: str) -> bool:
        """Is `model` to be kept out of `/v1/models` for this host?"""
        rule = self.rule(host, model)
        return rule is not None and rule.hides

    def for_host(self, host: str) -> dict[str, Rule]:
        return {model: rule for (name, model), rule in self.rules.items()
                if name == host}

    # -- reads: the lock (issue #21) ---------------------------------------
    def lock(self, model: str) -> Lock | None:
        return self.locks.get(str(model))

    def locked(self, model: str) -> bool:
        """Is `model` protected from being unloaded?"""
        return str(model) in self.locks

    def locked_models(self) -> list[str]:
        """Every locked model id, sorted — the shape the endpoints report."""
        return sorted(self.locks)

    def __len__(self) -> int:
        return len(self.rules)

    # -- writes ------------------------------------------------------------
    def set(self, host: str, model: str, level: str, *, by: str = "operator",
            note: str = "") -> Rule:
        """Set (or replace) the rule for one pair. The newer decision wins."""
        if level not in LEVELS:
            raise ValueError(f"unknown level {level!r} — one of {', '.join(LEVELS)}")
        rule = Rule(host=host, model=model, level=level, since=_now(), by=by,
                    note=note)
        self.rules[(host, model)] = rule
        return rule

    def clear(self, host: str, model: str) -> bool:
        return self.rules.pop((host, model), None) is not None

    def set_lock(self, model: str, *, by: str = "operator", note: str = "") -> Lock:
        """Lock `model` (idempotent: re-locking refreshes the stamp and the note)."""
        lock = Lock(model=str(model), since=_now(), by=by, note=note)
        self.locks[str(model)] = lock
        return lock

    def clear_lock(self, model: str) -> bool:
        """Unlock. `False` when there was nothing to release."""
        return self.locks.pop(str(model), None) is not None


# -- file ------------------------------------------------------------------
def load(path: str | Path = DEFAULT_FILE) -> ModelPolicy:
    """Read the policy file. Missing or broken is simply "nothing switched off".

    A corrupt file must never keep RIPPLE from starting. The models then stay
    available, which is the state a missing file describes too — the wrong way
    round (starting with every model hidden because a YAML line broke) would
    take the whole fleet's model list down.
    """
    p = Path(path).expanduser()
    store = ModelPolicy(path=p)
    if not p.is_file():
        return store
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("model_policy_unreadable path=%s error=%s — nothing disabled",
                    p, exc)
        return store
    if not isinstance(raw, dict):
        log.warning("model_policy_invalid path=%s — root is not a mapping", p)
        return store
    hosts = raw.get("models")
    if isinstance(hosts, dict):
        for host, entries in hosts.items():
            if not isinstance(entries, dict):
                continue
            for model, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                level = str(entry.get("level") or "")
                if level not in LEVELS:
                    # Written by hand, or by a version that knew a level this one
                    # does not: ignoring it would silently re-enable the model.
                    log.warning("model_policy_unknown_level host=%s model=%s level=%s",
                                host, model, level or "-")
                    continue
                store.rules[(str(host), str(model))] = Rule(
                    host=str(host), model=str(model), level=level,
                    since=_parse(entry.get("since")) or _now(),
                    by=str(entry.get("by") or "operator"),
                    note=str(entry.get("note") or ""),
                )
    # The locks (issue #21) live in the same file, one level up: they belong to
    # the model, not to a (host, model) pair.
    locks = raw.get("locks")
    if isinstance(locks, dict):
        for model, entry in locks.items():
            if not isinstance(entry, dict):
                # `locks: {m1: true}` reads as "locked, nothing else known" —
                # a hand edit must not silently release the model.
                if entry is True:
                    store.locks[str(model)] = Lock(model=str(model))
                continue
            store.locks[str(model)] = Lock(
                model=str(model),
                since=_parse(entry.get("since")) or _now(),
                by=str(entry.get("by") or "operator"),
                note=str(entry.get("note") or ""),
            )
    return store


def save(store: ModelPolicy, path: str | Path | None = None) -> None:
    """Write atomically with mode 0600 (operator data, next to a running proxy)."""
    target = Path(path or store.path or DEFAULT_FILE).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for rule in store.rules.values():
        grouped.setdefault(rule.host, {})[rule.model] = {
            "level": rule.level,
            "since": rule.since.isoformat(timespec="seconds"),
            "by": rule.by,
            "note": rule.note,
        }
    payload: dict[str, Any] = {"version": VERSION, "models": grouped}
    if store.locks:
        # Only written when there is a lock, so a fleet that never uses the
        # feature keeps the file it has always had.
        payload["locks"] = {model: lock.to_dict()
                            for model, lock in sorted(store.locks.items())}

    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".models-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    store.path = target
