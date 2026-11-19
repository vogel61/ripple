"""Compare concurrency runs: where did the time go, and who was idle?

`concurrency_series.py` stores every request; this reads those files and puts the
configurations side by side. Throughput per host is divided by the host's *own*
span (its slowest request), not by the level's wall time — otherwise a fast host
looks slow merely because a slow host was still busy.

    tools/concurrency_report.py tools/concurrency/results/*.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def host_stats(level: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in level["requests"]:
        name = r.get("host") or f"unrouted({r.get('status')})"
        h = out.setdefault(name, {"n": 0, "lat": [], "tokens": 0})
        h["n"] += 1
        h["lat"].append(r["latency"])
        h["tokens"] += (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
    for h in out.values():
        span = max(h["lat"])
        h["mean"] = sum(h["lat"]) / len(h["lat"])
        h["span"] = span
        h["tok_s"] = h["tokens"] / span if span else 0
    return out


def main(paths: list[str]) -> int:
    runs = [json.loads(Path(p).read_text()) for p in paths]
    for run in runs:
        caps = ", ".join(f"{k} {v['max_inflight']}/{v['spill_threshold']}"
                         for k, v in sorted(run.get("config", {}).items()))
        print(f"\n=== {run['label']}  (cap/spill: {caps})  "
              f"{run['model']}, ~{run['prompt_tokens_target']} prompt tokens")

    levels = sorted({l["concurrency"] for r in runs for l in r["levels"]})
    for n in levels:
        print(f"\n--- {n} concurrent")
        print(f"    {'config':<18}{'wall':>7}{'split':>14}{'tok/s all':>11}"
              f"{'host':>12}{'req':>5}{'mean lat':>10}{'span':>7}{'tok/s':>8}{'idle':>7}")
        for run in runs:
            lvl = next((l for l in run["levels"] if l["concurrency"] == n), None)
            if not lvl:
                continue
            hs = host_stats(lvl)
            total = sum(h["tokens"] for h in hs.values())
            split = " ".join(f"{k.replace('-alpha','').replace('-beta','')}"
                             f":{h['n']}" for k, h in sorted(hs.items()))
            slowest = max(h["span"] for h in hs.values())
            idle = lvl["wall_s"] - slowest
            lead = (f"    {run['label']:<18}{lvl['wall_s']:>8.1f}{split:>14}"
                    f"{total / lvl['wall_s']:>11.0f}")
            for i, (name, h) in enumerate(sorted(hs.items())):
                if i == 0:
                    print(f"{lead}{name:>12}{h['n']:>5}{h['mean']:>9.1f}s"
                          f"{h['span']:>6.1f}s{h['tok_s']:>8.0f}{idle:>7.1f}s")
                else:
                    print(f"{'':<18}{'':>8}{'':>14}{'':>11}{name:>12}{h['n']:>5}"
                          f"{h['mean']:>9.1f}s{h['span']:>6.1f}s{h['tok_s']:>8.0f}"
                          f"{'':>7}")
            if lvl["errors"]:
                print(f"    {'':<18}errors: {lvl['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
