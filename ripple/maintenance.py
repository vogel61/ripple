"""Operator-declared maintenance windows — taking a node out of service.

This is the one thing an operator does to RIPPLE rather than to oMLX: no model is
loaded, unloaded or patched, the balancer only stops handing new work to the node.
Running requests finish normally, and a thread that was bound to the node falls
back to the other host when that host offers the model (FA-11 already does that
once the node leaves the pool). Nothing in oMLX changes — which is why this is a
smaller exception than the Calibrate button (requirements FA-22, NZ-4): it is
routing state, not a second control plane.

The window is **persisted**. A node taken out for an update must not reappear in
the pool because RIPPLE was restarted for a deploy — which is exactly what an
operator does while the node is out. An optional `minutes` bounds the window, so a
forgotten flag cannot halve the fleet for a week.

A window set by a calibration job is deliberately *not* persisted: if RIPPLE dies
mid-job the node must come back by itself.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("ripple.maintenance")

VERSION = 1
DEFAULT_FILE = "~/.ripple/maintenance.yaml"


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
class Window:
    """One node's maintenance window."""

    node: str
    since: datetime = field(default_factory=_now)
    until: datetime | None = None
    by: str = "operator"
    note: str = ""

    def active(self, now: datetime | None = None) -> bool:
        if self.until is None:
            return True
        return (now or _now()) < self.until

    def minutes_left(self, now: datetime | None = None) -> float | None:
        if self.until is None:
            return None
        return round(max(0.0, (self.until - (now or _now())).total_seconds()) / 60.0, 1)

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "node": self.node,
            "since": self.since.isoformat(timespec="seconds"),
            "until": self.until.isoformat(timespec="seconds") if self.until else None,
            "minutes_left": self.minutes_left(now),
            "by": self.by,
            "note": self.note,
        }


class Maintenance:
    """The windows, keyed by host name (the operator's name, not the node_id).

    Host names are what the operator sees in the dashboard and what the admin
    endpoints take; a `node_id` would be more stable across a rename, but a rename
    is exactly when the operator must look at the window again.
    """

    def __init__(self, path: str | Path | None = None,
                 windows: dict[str, Window] | None = None) -> None:
        self.path = Path(path).expanduser() if path else None
        self.windows: dict[str, Window] = dict(windows or {})

    # -- reads -------------------------------------------------------------
    def active(self, node: str, now: datetime | None = None) -> Window | None:
        win = self.windows.get(node)
        return win if win is not None and win.active(now) else None

    def nodes(self, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        now = now or _now()
        return {name: win.to_dict(now) for name, win in self.windows.items()
                if win.active(now)}

    def is_out(self, node: str) -> bool:
        return self.active(node) is not None

    # -- writes ------------------------------------------------------------
    def set(self, node: str, *, minutes: float | None = None, by: str = "operator",
            note: str = "") -> Window:
        """Put `node` into maintenance, replacing any window it already had."""
        now = _now()
        until = now + timedelta(minutes=minutes) if minutes else None
        win = Window(node=node, since=now, until=until, by=by, note=note)
        self.windows[node] = win
        return win

    def clear(self, node: str) -> bool:
        return self.windows.pop(node, None) is not None

    def prune(self, now: datetime | None = None) -> list[str]:
        """Drop expired windows. Returns the names that were removed."""
        now = now or _now()
        gone = [name for name, win in self.windows.items() if not win.active(now)]
        for name in gone:
            self.windows.pop(name, None)
        return gone


# -- file ------------------------------------------------------------------
def load(path: str | Path = DEFAULT_FILE) -> Maintenance:
    """Read the maintenance file. Missing or broken is simply "no windows".

    A corrupt file must never keep RIPPLE from starting; the nodes then simply
    stay in the pool, which is the state a missing file describes too.
    """
    p = Path(path).expanduser()
    store = Maintenance(path=p)
    if not p.is_file():
        return store
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("maintenance_unreadable path=%s error=%s — no windows", p, exc)
        return store
    if not isinstance(raw, dict):
        log.warning("maintenance_invalid path=%s — root is not a mapping", p)
        return store
    nodes = raw.get("nodes")
    if isinstance(nodes, dict):
        for name, entry in nodes.items():
            if not isinstance(entry, dict):
                continue
            store.windows[str(name)] = Window(
                node=str(name),
                since=_parse(entry.get("since")) or _now(),
                until=_parse(entry.get("until")),
                by=str(entry.get("by") or "operator"),
                note=str(entry.get("note") or ""),
            )
    return store


def save(store: Maintenance, path: str | Path | None = None) -> None:
    """Write atomically with mode 0600 (operator data, next to a running proxy).

    Windows that a job set (`by="calibration"`) are **not** written: they exist
    only while the job runs, and if RIPPLE dies mid-job the node has to come back
    by itself rather than stay out until someone notices.
    """
    target = Path(path or store.path or DEFAULT_FILE).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": VERSION,
        "nodes": {
            name: {
                "since": win.since.isoformat(timespec="seconds"),
                "until": win.until.isoformat(timespec="seconds") if win.until else None,
                "by": win.by,
                "note": win.note,
            }
            for name, win in store.windows.items()
            if win.by != "calibration"
        },
    }
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".maintenance-", suffix=".tmp")
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
