"""Concurrency series: how much a node should take at once.

AS-6 measured decode throughput one request at a time and confirmed the weights
3:2. This answers the other half of the placement question: what `max_inflight`
and `spill_threshold` should be. Both were set by hand — and the M5, the stronger
node with weight 3, got the *lower* cap (3, against 6 on the M3), which
contradicts the weights and makes the M5 count as "full" for new threads from 2
concurrent requests on.

Method: N concurrent requests of ~8.5k prompt tokens, each with a unique marker,
so every request is a distinct thread with its own prefix. A shared prefix would
bind them all to one host through the affinity rule and measure nothing.

Run it once per configuration and compare the two JSON files:

    tools/concurrency_series.py --label m5cap3 --out tools/concurrency/results
    # change max_inflight/spill_threshold, restart, then
    tools/concurrency_series.py --label m5cap6 --out tools/concurrency/results

Only requests are sent — no model is loaded or unloaded (NZ-4). The model must
already be resident, otherwise the first request pays a load and the numbers lie.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx

FILLER = (
    "The placement of a conversation is a property of the thread, not of the "
    "request. A thread that stays on one host keeps its prefix cache warm, and a "
    "warm prefix cache is the difference between a prefill that costs a second "
    "and one that costs a minute. "
)


def build_prompt(target_tokens: int, marker: str) -> str:
    """A long prompt that forces a long answer and carries a unique marker.

    The marker sits at the front, so each request hashes to a different prefix
    and becomes its own thread — which is what has to be distributed.
    """
    chars = max(200, int(target_tokens * 5.1))
    body = (FILLER * (chars // len(FILLER) + 1))[:chars]
    return (
        f"Measurement run {marker}.\n\n{body}\n\n"
        "Repeat the text above verbatim, word for word, without any introduction "
        "or commentary. Start immediately with the first word."
    )


async def one_request(client, base, key, model, prompt, max_tokens) -> dict:
    started = time.perf_counter()
    out: dict[str, object] = {"started": started}
    try:
        r = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=900.0,
        )
        out["status"] = r.status_code
        out["host"] = r.headers.get("x-ripple-host")
        out["reason"] = r.headers.get("x-ripple-reason")
        if r.status_code == 200:
            body = r.json()
            usage = body.get("usage") or {}
            out["prompt_tokens"] = usage.get("prompt_tokens")
            out["completion_tokens"] = usage.get("completion_tokens")
        else:
            out["error"] = r.text[:200]
    except Exception as exc:  # a dead host is a result, not a crash
        out["status"] = 0
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    out["latency"] = time.perf_counter() - started
    return out


async def health(client, root) -> dict:
    """RIPPLE's /healthz, when the target is RIPPLE. A direct oMLX host has no
    such endpoint, and a throughput run against it does not need one."""
    try:
        r = await client.get(f"{root}/healthz", timeout=15.0)
        d = r.json()
    except Exception:
        return {}
    if not isinstance(d, dict) or "hosts" not in d:
        return {}   # oMLX's own /healthz, not RIPPLE's
    return {
        h["name"]: {
            "max_inflight": h["max_inflight"],
            "spill_threshold": h["spill_threshold"],
            "inflight": h["inflight"],
        }
        for h in d["hosts"]
    }


def summarise(level: dict) -> str:
    parts = [f"{level['concurrency']:>2} parallel  wall {level['wall_s']:6.1f}s  "
             f"{level['req_s']:.3f} req/s  "
             f"{level['processed_tok_s']:6.0f} tok/s processed"]
    for name, h in sorted(level["hosts"].items()):
        parts.append(
            f"\n      {name:<10} {h['count']:>2} req  {h['mean_latency']:6.1f}s "
            f"mean  {h['processed_tok_s']:6.0f} tok/s  "
            f"{h['prompt_tokens']:>6} in / {h['completion_tokens']:>5} out"
        )
    if level["errors"]:
        parts.append(f"\n      errors: {level['errors']}")
    return "".join(parts)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:8010/v1")
    ap.add_argument("--key-file", default="~/.omlx/api_key.txt")
    ap.add_argument("--model", default="DeepSeek-V4.1-Flash-1017-oQ4e-mtp")
    ap.add_argument("--levels", default="3,6,9")
    ap.add_argument("--prompt-tokens", type=int, default=8500)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--label", required=True, help="name of this configuration")
    ap.add_argument("--out", default="tools/concurrency/results")
    args = ap.parse_args()

    key = Path(args.key_file).expanduser().read_text().strip()
    root = args.base.rstrip("/").removesuffix("/v1")
    levels = [int(x) for x in args.levels.split(",")]
    run = {
        "label": args.label,
        "model": args.model,
        "base": args.base,
        "prompt_tokens_target": args.prompt_tokens,
        "max_tokens": args.max_tokens,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "levels": [],
    }

    async with httpx.AsyncClient() as client:
        before = await health(client, root)
        run["config"] = before
        print(f"configuration '{args.label}': "
              + ", ".join(f"{k} cap {v['max_inflight']} spill {v['spill_threshold']}"
                          for k, v in sorted(before.items())))

        for n in levels:
            marker = f"{args.label}-{n}-{uuid.uuid4().hex[:6]}"
            prompts = [build_prompt(args.prompt_tokens, f"{marker}-{i}") for i in range(n)]
            t0 = time.perf_counter()
            results = await asyncio.gather(*[
                one_request(client, args.base, key, args.model, p, args.max_tokens)
                for p in prompts
            ])
            wall = time.perf_counter() - t0

            hosts: dict[str, dict] = {}
            for r in results:
                name = r.get("host") or f"unrouted({r.get('status')})"
                h = hosts.setdefault(name, {"count": 0, "latencies": [],
                                            "completion_tokens": 0,
                                            "prompt_tokens": 0})
                h["count"] += 1
                h["latencies"].append(r["latency"])
                h["completion_tokens"] += r.get("completion_tokens") or 0
                h["prompt_tokens"] += r.get("prompt_tokens") or 0

            tokens = sum(h["completion_tokens"] for h in hosts.values())
            processed = sum(h["prompt_tokens"] + h["completion_tokens"]
                            for h in hosts.values())
            level = {
                "concurrency": n,
                "wall_s": round(wall, 2),
                "req_s": round(n / wall, 3),
                "tok_s": round(tokens / wall, 1),
                "processed_tokens": processed,
                "processed_tok_s": round(processed / wall, 1),
                "completion_tokens": tokens,
                "errors": [f"{r.get('status')} {r.get('error', '')}".strip()
                           for r in results if r.get("status") != 200],
                "hosts": {
                    name: {
                        "count": h["count"],
                        "mean_latency": round(statistics.fmean(h["latencies"]), 2),
                        "max_latency": round(max(h["latencies"]), 2),
                        "completion_tokens": h["completion_tokens"],
                        "prompt_tokens": h["prompt_tokens"],
                        "processed_tok_s": round(
                            (h["prompt_tokens"] + h["completion_tokens"]) / wall, 1),
                    }
                    for name, h in hosts.items()
                },
                "requests": [
                    {k: (round(v, 2) if isinstance(v, float) else v)
                     for k, v in r.items() if k != "started"}
                    for r in results
                ],
            }
            run["levels"].append(level)
            print(summarise(level), flush=True)
            await asyncio.sleep(5)  # let the hosts settle between levels

        run["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        run["config_after"] = await health(client, root)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"concurrency-{time.strftime('%Y%m%d-%H%M%S')}-{args.label}.json"
    path.write_text(json.dumps(run, indent=2))
    print(f"\nwritten: {path}")
    return 0 if not any(l["errors"] for l in run["levels"]) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
