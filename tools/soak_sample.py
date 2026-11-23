#!/usr/bin/env python3
"""Sample a running RIPPLE instance for the M4 soak period.

Two modes:

    python tools/soak_sample.py                     # append one sample
    python tools/soak_sample.py --report            # evaluate what was collected

The sample goes into `tools/soak/samples-YYYY-MM-DD.jsonl`, one line per run. An
unreachable instance is recorded as `reachable: false` instead of being skipped —
a gap in the series is evidence, not noise.

`counters`, `by_host` and `by_model` in `/stats` are cumulative for the running
process. Every sample therefore carries a `delta` next to them: the change since
the previous sample of the same run. The delta is `null` — never zero — when the
two samples are not comparable: the first sample of a run, a predecessor that was
unreachable, `uptime_seconds` that went backwards (the process restarted), or a
counter that decreased (#132).

The report answers the M4 question: did it run for days without intervention, how
did the requests split over the hosts, and what went wrong. The host share is the
sum of the per-run deltas, so it counts requests instead of counting each sample
once more.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8010"
DEFAULT_DIR = Path(__file__).resolve().parent / "soak"

# The three maps in /stats that are cumulative for the running process.
DELTA_KEYS = ("by_host", "by_model", "counters")


def get(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def delta_map(current: dict | None, previous: dict | None) -> dict | None:
    """`current - previous` per key, or None if any value would go backwards.

    A missing predecessor map is None as well: without a baseline the difference
    would be the whole cumulative value, which is not a delta.
    """
    if current is None or previous is None:
        return None
    before = previous
    out: dict = {}
    for key, value in current.items():
        if not isinstance(value, (int, float)):
            continue
        old = before.get(key)
        old = old if isinstance(old, (int, float)) else 0
        diff = value - old
        if diff < 0:
            return None
        out[key] = diff
    return out


def derive_delta(current: dict, previous: dict | None) -> dict | None:
    """Delta of the cumulative counters since the previous sample of the same run.

    None means "not a delta", never "zero": no predecessor, a predecessor that was
    unreachable (the run may have restarted in the gap), uptime that went backwards
    or stood still (a restart, or a clock that jumped), or a counter that decreased.
    """
    if not previous or not previous.get("reachable"):
        return None
    now_up = current.get("uptime_seconds")
    then_up = previous.get("uptime_seconds")
    if not isinstance(now_up, (int, float)) or not isinstance(then_up, (int, float)):
        return None
    if now_up <= then_up:
        return None
    delta: dict = {}
    for key in DELTA_KEYS:
        value = current.get(key)
        if value is None:
            delta[key] = None
            continue
        diff = delta_map(value, previous.get(key))
        if diff is None:
            return None
        delta[key] = diff
    return delta


def load(out_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(out_dir.glob("samples-*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def last_record(out_dir: Path) -> dict | None:
    """The most recent sample on disk — the predecessor for the new delta."""
    rows = load(out_dir)
    return rows[-1] if rows else None


def sample(url: str, out_dir: Path) -> dict:
    record: dict = {"ts": round(time.time(), 3),
                    "at": datetime.now().isoformat(timespec="seconds")}
    try:
        health = get(f"{url}/healthz")
        stats = get(f"{url}/stats")
        record["reachable"] = True
        record["uptime_seconds"] = stats.get("uptime_seconds")
        record["hosts"] = {h["name"]: h["status"] for h in health.get("hosts", [])}
        record["threads_active"] = health.get("threads", {}).get("active")
        record["guard"] = health.get("capabilities", {}).get("eviction_guard")
        record["warnings"] = health.get("warnings", [])
        record["counters"] = {k: stats.get(k) for k in (
            "requests_total", "requests_ok", "errors_total",
            "http_503_total", "http_507_total", "threads_new", "thread_hits",
            "prefix_matches", "spillover", "fallbacks", "addr_switches")}
        record["by_host"] = {k: v.get("requests")
                             for k, v in stats.get("by_host", {}).items()}
        record["by_model"] = {k: v.get("requests")
                              for k, v in stats.get("by_model", {}).items()}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        record["reachable"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"

    # The predecessor has to be read before this sample is appended.
    record["delta"] = derive_delta(record, last_record(out_dir))

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"samples-{datetime.now():%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def row_delta(rows: list[dict], index: int) -> dict | None:
    """The stored delta, or one derived from the series for files written before #132."""
    row = rows[index]
    stored = row.get("delta")
    if isinstance(stored, dict):
        return stored
    if "delta" in row:
        return None  # explicitly null: a restart, not a missing field
    return derive_delta(row, rows[index - 1] if index else None)


def totals(rows: list[dict], key: str) -> tuple[Counter, int]:
    """Sum of the per-run deltas of `key`, and the number of deltas that went in."""
    out: Counter = Counter()
    used = 0
    for index in range(len(rows)):
        part = (row_delta(rows, index) or {}).get(key)
        if not part:
            continue
        used += 1
        out.update({k: v for k, v in part.items()
                    if isinstance(v, (int, float)) and v})
    return out, used


def uncovered(rows: list[dict]) -> Counter:
    """Requests in the cumulative counters of samples that carry no delta.

    That is the head of each run — the requests between the restart and the first
    sample — plus whatever a gap hid. It is reported so the share is not mistaken
    for the complete traffic.
    """
    out: Counter = Counter()
    for index, row in enumerate(rows):
        if not row.get("reachable") or row_delta(rows, index) is not None:
            continue
        out.update({k: v for k, v in (row.get("by_host") or {}).items()
                    if isinstance(v, (int, float)) and v})
    return out


def shares(counts: dict, digits: int = 1) -> list[tuple[str, float]]:
    """Percentages that add up to exactly 100 — largest remainder, not plain rounding."""
    total = sum(counts.values())
    if not total:
        return []
    scale = 10 ** digits
    exact = {k: 100 * v * scale / total for k, v in counts.items()}
    out = {k: int(x) for k, x in exact.items()}
    short = 100 * scale - sum(out.values())
    order = sorted(exact, key=lambda k: (exact[k] - int(exact[k]), counts[k]),
                   reverse=True)
    for i in range(max(0, short)):
        out[order[i % len(order)]] += 1
    return [(k, out[k] / scale) for k in sorted(out, key=lambda k: (-counts[k], k))]


def report(out_dir: Path) -> int:
    rows = load(out_dir)
    if not rows:
        print(f"no samples in {out_dir} — run the sampler first")
        return 1

    first, last = rows[0], rows[-1]
    span_h = (last["ts"] - first["ts"]) / 3600
    down = [r for r in rows if not r.get("reachable")]
    gaps = 0
    for a, b in zip(rows, rows[1:]):
        if b["ts"] - a["ts"] > 900:  # more than three missed 5-minute samples
            gaps += 1

    print(f"samples      : {len(rows)}  ({first['at']} .. {last['at']}, {span_h:.1f} h)")
    print(f"unreachable  : {len(down)}" + (f"  last: {down[-1]['error']}" if down else ""))
    print(f"gaps > 15 min: {gaps}")

    ok = [r for r in rows if r.get("reachable")]
    if not ok:
        return 1
    last_ok = ok[-1]
    print(f"uptime (last): {last_ok['uptime_seconds'] / 3600:.1f} h"
          if last_ok.get("uptime_seconds") else "uptime (last): unknown")
    # These three are cumulative for the running process, not for the window.
    print(f"counters     : last sample, cumulative since process start — "
          f"{json.dumps(last_ok.get('counters', {}))}")
    print(f"by_host      : last sample, cumulative since process start — "
          f"{json.dumps(last_ok.get('by_host', {}))}")
    print(f"by_model     : last sample, cumulative since process start — "
          f"{json.dumps(last_ok.get('by_model', {}))}")

    hosts, used = totals(rows, "by_host")
    total = sum(hosts.values())
    if total:
        share = ", ".join(f"{h} {p:.1f} %" for h, p in shares(hosts))
        print(f"host share   : {share}")
        print(f"host requests: {total}  (sum of {used} deltas over {len(ok)} reachable "
              f"samples; {json.dumps(dict(hosts.most_common()))})")
    missing = uncovered(rows)
    if missing:
        print(f"no delta     : {sum(missing.values())} requests before the first sample "
              f"of a run — {json.dumps(dict(missing.most_common()))}")

    models, model_deltas = totals(rows, "by_model")
    if models:
        print(f"by_model Δ   : {json.dumps(dict(models.most_common()))}  "
              f"(sum of {model_deltas} deltas)")
    counters, counter_deltas = totals(rows, "counters")
    if counters:
        print(f"counters Δ   : {json.dumps(dict(counters))}  "
              f"(sum of {counter_deltas} deltas)")

    statuses = Counter(s for r in ok for s in (r.get("hosts") or {}).values())
    print(f"host status  : {dict(statuses)}")
    warns = Counter(w for r in ok for w in (r.get("warnings") or []))
    print(f"warnings     : {dict(warns) if warns else 'none'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sample RIPPLE for the soak period")
    p.add_argument("--url", default=os.environ.get("RIPPLE_URL", DEFAULT_URL))
    p.add_argument("--out", default=str(DEFAULT_DIR))
    p.add_argument("--report", action="store_true", help="evaluate the samples")
    p.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = p.parse_args(argv)

    out_dir = Path(args.out).expanduser()
    if args.report:
        return report(out_dir)

    record = sample(args.url, out_dir)
    if not args.quiet:
        print(json.dumps(record, ensure_ascii=False))
    return 0 if record.get("reachable") else 1


if __name__ == "__main__":
    sys.exit(main())
