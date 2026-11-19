#!/usr/bin/env python3
"""Sample a running RIPPLE instance for the M4 soak period.

Two modes:

    python tools/soak_sample.py                     # append one sample
    python tools/soak_sample.py --report            # evaluate what was collected

The sample goes into `tools/soak/samples-YYYY-MM-DD.jsonl`, one line per run. An
unreachable instance is recorded as `reachable: false` instead of being skipped —
a gap in the series is evidence, not noise.

The report answers the M4 question: did it run for days without intervention, how
did the requests split over the hosts, and what went wrong.
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


def get(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


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

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"samples-{datetime.now():%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def load(out_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(out_dir.glob("samples-*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


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
    print(f"counters     : {json.dumps(last_ok.get('counters', {}))}")
    print(f"by_host      : {json.dumps(last_ok.get('by_host', {}))}")
    print(f"by_model     : {json.dumps(last_ok.get('by_model', {}))}")

    hosts = Counter()
    for r in ok:
        hosts.update({k: v for k, v in (r.get("by_host") or {}).items() if v})
    total = sum(hosts.values())
    if total:
        share = ", ".join(f"{h} {100 * n / total:.0f} %" for h, n in hosts.most_common())
        print(f"host share   : {share}")

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
