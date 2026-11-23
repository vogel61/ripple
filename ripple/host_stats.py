"""Per-host ledger: what each node did since RIPPLE started (issue #33).

Three questions, and where the answer comes from — none of them is guessed:

* **How long was a node idle, loading, prefilling, decoding?** Not derivable
  from RIPPLE's own bookkeeping: two requests overlap, a node is idle between
  them, and a load RIPPLE did not cause is invisible from here. So the node is
  asked — `GET /admin/api/activity` once per probe tick — and every tick maps to
  exactly **one** of five exclusive states. `loading` is a state of its own on
  purpose: a 453 GB cold load takes about two minutes and is neither idle nor
  decode. The five states add up to the sampled window (`sampled_seconds`).
* **How much prefill and decode time did it really spend?** oMLX measures it
  itself and returns it in `usage` on every response, streamed or not:
  `prompt_eval_duration` is the prefill, `generation_duration` the decode. The
  ledger only sums what it is told. A response without those fields is counted
  in `requests_without_usage` and its split stays **unknown** — never estimated
  from `prompt_chars` (a real run estimated 25 000 tokens against 82 510
  actual).
* **What did the client wait for?** `client_seconds` is RIPPLE's own wall clock
  from dispatch to the last byte. It is not the node's time: a request that sat
  in a queue waited longer than the node worked, and the two numbers are kept
  apart for exactly that reason.

The ledger is persisted (`stats.file`, atomic write: `.tmp` + `rename`) and can
be zeroed from the dashboard (`POST /admin/stats/reset`). It survives a restart;
`reset_at` says when it was last zeroed, `started_at` when this process started.

The nodes' own SQLite usage (`GET /admin/api/usage?range=today`) is carried
beside it as a **cross-check**, not as a second ledger: the gap between the two
is the traffic that went past RIPPLE, which is the point of showing it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger("ripple")

# The five exclusive states a tick can be in. Order is display order: `mixed`
# sits between the two states it combines, so the bar reads as one movement —
# idle, loading, prefill, prefill+decode, decode — instead of ending in a
# leftover sliver at the right edge.
STATES = ("idle", "loading", "prefill", "mixed", "decode")

# The work counters. `requests` counts answers the node gave RIPPLE (a failed
# connect is an `errors` entry and no request: the node never saw it).
WORK = ("requests", "errors", "prompt_tokens", "completion_tokens",
        "prefill_seconds", "generation_seconds", "client_seconds",
        "requests_without_usage")

# The three fields of the node's own count that are comparable with the ledger.
CROSS_FIELDS = ("requests", "prefill_seconds", "generation_seconds")


def _number(value: Any) -> float | None:
    """A finite non-negative number, or None. `bool` is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:      # NaN or negative
        return None
    return float(value)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _non_empty(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 0


def state_from_activity(payload: Any) -> str | None:
    """The one state a node is in, read from `GET /admin/api/activity`.

    The node reports per model which requests are prefilling and which are
    generating; the state is the node's, so a node that prefills one request
    while another generates is `mixed`. Anything else — no model loaded, no
    request anywhere — is `idle`; a model that is still being loaded is
    `loading`, which is why that check comes before `idle`.

    `None` means the answer could not be read: an unparsable body or a missing
    `active_models` is not a statement about the node, and the tick then
    contributes to no state at all rather than to a guessed one.
    """
    if not isinstance(payload, dict):
        return None
    active = payload.get("active_models")
    if not isinstance(active, dict):
        return None
    models = active.get("models")
    if models is None:
        models = []
    if not isinstance(models, list):
        return None
    rows = [m for m in models if isinstance(m, dict)]
    prefilling = any(_non_empty(m.get("prefilling")) for m in rows)
    generating = any(_non_empty(m.get("generating")) for m in rows)
    loading = any(bool(m.get("is_loading")) for m in rows)
    if prefilling and generating:
        return "mixed"
    if prefilling:
        return "prefill"
    if generating:
        return "decode"
    if loading:
        return "loading"
    return "idle"


def cross_from_usage(payload: Any) -> dict[str, int | float] | None:
    """The three comparable numbers of `GET /admin/api/usage`, or None.

    Tolerant on purpose: the shape is the node's own (`totals` plus a range),
    and a field the node does not report stays 0 instead of failing the whole
    cross-check.
    """
    if not isinstance(payload, dict):
        return None
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        return None
    out: dict[str, int | float] = {}
    for key in CROSS_FIELDS:
        value = totals.get(key)
        number = _count(value) if key == "requests" else _number(value)
        out[key] = number if number is not None else (0 if key == "requests" else 0.0)
    return out


@dataclass
class HostCounters:
    """One row of the ledger: work counters plus the five exclusive states."""

    requests: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prefill_seconds: float = 0.0
    generation_seconds: float = 0.0
    client_seconds: float = 0.0
    requests_without_usage: int = 0
    idle: float = 0.0
    loading: float = 0.0
    prefill: float = 0.0
    decode: float = 0.0
    mixed: float = 0.0
    # The window this row's five states add up to. Equal to the ledger's own
    # `sampled_seconds` as long as the node answered every tick; a tick the node
    # did not answer is in no state, so the row's window can be smaller.
    sampled_seconds: float = 0.0

    def zero(self) -> None:
        for f in fields(self):
            setattr(self, f.name, 0 if f.type in (int, "int") else 0.0)

    def add_state(self, state: str, seconds: float) -> None:
        if state not in STATES or seconds <= 0:
            return
        setattr(self, state, getattr(self, state) + seconds)
        self.sampled_seconds += seconds

    def add_work(self, *, usage: dict[str, Any] | None, client_seconds: float,
                 ok: bool = True) -> None:
        """One answered request. The split is summed only when the node gave it."""
        self.requests += 1
        if not ok:
            self.errors += 1
        waited = _number(client_seconds)
        if waited is not None:
            self.client_seconds += waited
        usage = usage if isinstance(usage, dict) else None
        prompt = _count(usage.get("prompt_tokens")) if usage else None
        completion = _count(usage.get("completion_tokens")) if usage else None
        if prompt is not None:
            self.prompt_tokens += prompt
        if completion is not None:
            self.completion_tokens += completion
        prefill = _number(usage.get("prompt_eval_duration")) if usage else None
        generation = _number(usage.get("generation_duration")) if usage else None
        if prefill is None or generation is None:
            # The tokens may be known, the split is not: an OpenAI-compatible
            # backend answers plain usage, a client abort answers nothing. Count
            # it and leave the split alone — the point of the number is that the
            # ledger does not shrink silently.
            self.requests_without_usage += 1
            return
        self.prefill_seconds += prefill
        self.generation_seconds += generation

    def as_dict(self, name: str | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {}
        if name is not None:
            row["name"] = name
        for key in WORK:
            value = getattr(self, key)
            row[key] = value if key in ("requests", "errors", "prompt_tokens",
                                        "completion_tokens",
                                        "requests_without_usage") else round(value, 3)
        for key in STATES:
            row[key] = round(getattr(self, key), 3)
        row["sampled_seconds"] = round(self.sampled_seconds, 3)
        return row

    @classmethod
    def from_dict(cls, raw: Any) -> "HostCounters":
        out = cls()
        if not isinstance(raw, dict):
            return out
        for key in WORK:
            value = raw.get(key)
            number = _count(value) if key in ("requests", "errors", "prompt_tokens",
                                              "completion_tokens",
                                              "requests_without_usage") \
                else _number(value)
            if number is not None:
                setattr(out, key, number)
        for key in STATES:
            number = _number(raw.get(key))
            if number is not None:
                setattr(out, key, number)
        window = _number(raw.get("sampled_seconds"))
        if window is not None:
            out.sampled_seconds = window
        return out


class HostStats:
    """The ledger: one row per host plus a `total`, persisted and resettable."""

    def __init__(self, names: list[str] | tuple[str, ...] = (), *,
                 path: str | Path | None = None, persist: bool = True,
                 flush_seconds: float = 30.0) -> None:
        self.path: Path | None = Path(path).expanduser() if path else None
        self.persist = bool(persist and self.path is not None)
        self.flush_seconds = max(0.0, float(flush_seconds))
        self.started_at: float = time.time()
        self.reset_at: float | None = None
        self.sampled_seconds: float = 0.0
        self.hosts: dict[str, HostCounters] = {name: HostCounters() for name in names}
        # The fleet the ledger was built for. A host that the file still knows
        # but the configuration no longer has keeps its numbers on disk and is
        # not shown: the page shows the fleet, not the history of a rename.
        self._fleet: list[str] = list(names)
        self._order: list[str] = list(names)
        # The state observed at the previous tick per host, and when that tick
        # was: the elapsed interval is charged to the state that held *during*
        # it, not to the one that happened to be seen at its end.
        self._observed: dict[str, str] = {}
        self._last_tick: float | None = None
        self._last_flush: float = 0.0
        self.dirty = False
        self.cross: dict[str, Any] = {
            "range": "today",
            "hosts": [],
            "total": {key: 0 for key in CROSS_FIELDS},
            "at": None,
            "error": None,
        }

    # -- rows --------------------------------------------------------------
    def host(self, name: str) -> HostCounters:
        """The row for `name`, created on first use and kept in display order."""
        if name not in self.hosts:
            self.hosts[name] = HostCounters()
            self._order.append(name)
        return self.hosts[name]

    @property
    def names(self) -> list[str]:
        return list(self._order)

    def add_host(self, name: str) -> None:
        """A node joined the fleet at runtime (issue #70).

        The ledger was built for the fleet that existed at start-up (`_fleet`
        decides which rows `snapshot()` shows); a node added at the page has to
        appear there too, from the next tick on.
        """
        self.host(name)
        if name not in self._fleet:
            self._fleet.append(name)

    def forget_host(self, name: str) -> None:
        """A node left the fleet. Its numbers stay in `hosts`, off the page.

        Same rule the class already had for a rename: the file keeps the history,
        the page shows the fleet. Nothing is deleted from disk here — a node that
        comes back under the same name finds its counters again.
        """
        self._fleet = [n for n in self._fleet if n != name]
        self._order = [n for n in self._order if n != name]

    # -- work counters -----------------------------------------------------
    def record(self, name: str, *, usage: dict[str, Any] | None,
               client_seconds: float, ok: bool = True) -> None:
        """One request the node answered (streamed or not)."""
        self.host(name).add_work(usage=usage, client_seconds=client_seconds, ok=ok)
        self.dirty = True

    def error(self, name: str) -> None:
        """A request that never reached the node: an error, and no request."""
        self.host(name).errors += 1
        self.dirty = True

    # -- state sampling ----------------------------------------------------
    def sample(self, states: dict[str, str], *, now: float | None = None) -> None:
        """One probe tick: `states` is host → state, for the hosts that answered.

        The interval since the previous tick is charged to the state that was
        observed *at* that previous tick (a left Riemann sum: what was seen then
        is what held in between). A host that is missing from `states` this time
        keeps the state it was last seen in for that interval — the alternative,
        dropping the time, would break the one claim the block makes, that the
        five states add up to the window. A host that has never answered is in no
        state and contributes nothing.

        The very first tick after a start or a reset has nothing to charge, so it
        only records the observation.
        """
        now = time.time() if now is None else float(now)
        previous = self._last_tick
        self._last_tick = now
        if previous is not None:
            delta = max(0.0, now - previous)
            if delta > 0:
                self.sampled_seconds += delta
                for name, state in self._observed.items():
                    self.host(name).add_state(state, delta)
                self.dirty = True
        for name, state in states.items():
            if state in STATES:
                self._observed[name] = state

    def forget_tick(self) -> None:
        """Drop the sampling clock (after a reset: nothing before it counts)."""
        self._observed = {}
        self._last_tick = None

    # -- cross-check -------------------------------------------------------
    def set_cross(self, rows: list[dict[str, Any]], *,
                  range_name: str = "today", at: float | None = None,
                  error: str | None = None) -> None:
        """Store the nodes' own count beside the ledger — never inside it."""
        total: dict[str, Any] = {key: 0 for key in CROSS_FIELDS}
        clean: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {"name": str(row.get("name") or "")}
            for key in CROSS_FIELDS:
                value = row.get(key)
                number = _count(value) if key == "requests" else _number(value)
                entry[key] = number if number is not None else 0
                total[key] += entry[key]
            clean.append(entry)
        self.cross = {"range": range_name, "hosts": clean, "total": total,
                      "at": round(time.time() if at is None else at, 3),
                      "error": error}

    # -- reset -------------------------------------------------------------
    def reset(self, *, now: float | None = None) -> float:
        """Zero every counter, set `reset_at`, and persist at once.

        The file on disk holds zeros when this returns — that is the promise the
        endpoint makes, so a crash right after the click cannot resurrect the
        old numbers.
        """
        at = time.time() if now is None else float(now)
        for row in self.hosts.values():
            row.zero()
        self.sampled_seconds = 0.0
        self.reset_at = at
        self.forget_tick()
        self.dirty = True
        self.flush(force=True, now=at)
        return at

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        """Read the file. Missing or unparseable means zeros, never a crash."""
        if self.path is None or not self.persist:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # A missing ledger on the first start is the normal case, not a
            # finding (issue #175): a fresh install should not open with a warning.
            log.info("host_stats: no ledger at %s — starting from zero", self.path)
            return
        except (OSError, ValueError) as exc:
            log.warning("host_stats: ledger at %s unreadable (%s) — starting from zero",
                        self.path, exc)
            return
        if not isinstance(raw, dict):
            log.warning("host_stats: ledger at %s is not an object — starting from zero",
                        self.path)
            return
        started = _number(raw.get("started_at"))
        if started is not None and started > 0:
            # The file's start is older than this process: the ledger spans a
            # restart, which is the whole reason it is on disk.
            self.started_at = started
        reset = _number(raw.get("reset_at"))
        self.reset_at = reset if reset else None
        window = _number(raw.get("sampled_seconds"))
        if window is not None:
            self.sampled_seconds = window
        rows = raw.get("hosts")
        if isinstance(rows, dict):
            rows = [{"name": name, **(value if isinstance(value, dict) else {})}
                    for name, value in rows.items()]
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = row.get("name")
                if not isinstance(name, str) or not name:
                    continue
                self.hosts[name] = HostCounters.from_dict(row)
                if name not in self._order:
                    self._order.append(name)
        self.dirty = False

    def flush(self, *, force: bool = False, now: float | None = None) -> None:
        """Write the ledger if it changed and the flush interval has passed."""
        if not self.persist or self.path is None:
            return
        at = time.time() if now is None else float(now)
        if not force and not self.dirty:
            return
        if not force and at - self._last_flush < self.flush_seconds:
            return
        payload = {
            "version": 1,
            "started_at": round(self.started_at, 3),
            "reset_at": round(self.reset_at, 3) if self.reset_at else None,
            "sampled_seconds": round(self.sampled_seconds, 3),
            "hosts": [row.as_dict(name) for name, row in
                      ((n, self.hosts[n]) for n in self._order)],
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
            # Atomic: a reader sees the old file or the new one, never half.
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("host_stats: cannot write %s (%s)", self.path, exc)
            return
        self._last_flush = at
        self.dirty = False

    # -- view --------------------------------------------------------------
    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        """The read shape of `/stats → host_stats` and `/healthz → load`.

        `window_seconds` is the wall clock since the last reset, `sampled_seconds`
        the time the states were actually sampled over — they differ when a node
        did not answer a tick, and the page shows both instead of pretending the
        five states cover a window they do not.
        """
        at = time.time() if now is None else float(now)
        since = self.reset_at or self.started_at
        shown = self._fleet or self._order
        rows = [self.hosts[name].as_dict(name) for name in shown if name in self.hosts]
        total = HostCounters()
        for name in shown:
            row = self.hosts.get(name)
            if row is None:
                continue
            for key in WORK + STATES + ("sampled_seconds",):
                setattr(total, key, getattr(total, key) + getattr(row, key))
        return {
            "started_at": round(self.started_at, 3),
            "reset_at": round(self.reset_at, 3) if self.reset_at else None,
            "since": round(since, 3),
            "window_seconds": round(max(0.0, at - since), 3),
            "sampled_seconds": round(self.sampled_seconds, 3),
            "hosts": rows,
            "total": total.as_dict(),
            "cross": dict(self.cross),
        }
