#!/usr/bin/env python3
"""AS-6 (prefill half): measure cold prompt processing per host and size.

`as6_bench.py` measures *decode* on one prompt length. This tool measures the
other half of the cost model that `placement.rebind` needs: how long a host
takes to swallow a prompt of 2k / 8k / 24k tokens, expressed as prompt tokens
per second (`hosts[].pp_tps`).

Cold means cold: the prompt is built from random words, so no prefix cache can
serve it, and every repetition uses a different sequence. The fixed overhead
(network, scheduling, one decoded token) is measured once per host with a tiny
prompt and subtracted, otherwise the small sizes would look far too slow.

Like `as6_bench.py` it talks to the oMLX hosts directly and refuses to load a
model that is not already resident (FA-18: a 400 GB load can evict a session).

Usage:
    ./.venv/bin/python tools/prefill_bench.py --config config.yaml --dry-run
    ./.venv/bin/python tools/prefill_bench.py --config config.yaml \
        --model DeepSeek-V4.1-Flash-1017-oQ4e-mtp --sizes 2000 8000 24000 --reps 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ripple.config import load_config  # noqa: E402

RESULTS = REPO / "tools" / "as6" / "results"

# Words per token, calibrated 14.11.2026 against oMLX's own usage block: the
# syllable filler costs ~2.15 tokens per word (measured at 3.5k/13.8k/41.3k
# tokens). The true value is read back per run, so this only aims the target.
WORDS_PER_TOKEN = 2.15
SYLLABLES = ("ka", "ro", "mi", "tu", "ne", "sa", "lo", "vi", "da", "pu", "ge", "zu")


def random_prompt(target_tokens: int, rng: random.Random) -> str:
    """A prompt of roughly `target_tokens` tokens that no cache can serve."""
    words = max(16, int(target_tokens / WORDS_PER_TOKEN))
    parts = []
    for _ in range(words):
        parts.append("".join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 3))))
    return " ".join(parts) + "\n\nReply with OK."


async def loaded_models(client: httpx.AsyncClient, base: str, key: str) -> dict:
    r = await client.get(f"{base}/v1/models/status",
                         headers={"authorization": f"Bearer {key}"} if key else {})
    r.raise_for_status()
    return {m["id"]: m for m in r.json().get("models", [])}


async def one(client: httpx.AsyncClient, base: str, key: str, model: str,
              prompt: str) -> dict:
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "max_tokens": 1, "temperature": 0, "stream": False}
    t0 = time.perf_counter()
    r = await client.post(f"{base}/v1/chat/completions", json=payload, headers=headers,
                          timeout=1800.0)
    wall = time.perf_counter() - t0
    r.raise_for_status()
    usage = (r.json().get("usage") or {})
    return {"wall_s": wall, "prompt_tokens": usage.get("prompt_tokens"),
            "ttft_s": usage.get("time_to_first_token")}


async def run_host(host, model: str, sizes: list[int], reps: int, dry: bool) -> dict:
    base = host.addresses[0].rstrip("/")
    rng = random.Random(f"{host.name}-{time.time()}")
    async with httpx.AsyncClient() as client:
        try:
            models = await loaded_models(client, base, host.api_key)
        except (httpx.HTTPError, ValueError) as exc:
            return {"host": host.name, "error": f"{type(exc).__name__}: {exc}"}
        info = models.get(model)
        if info is None:
            return {"host": host.name, "error": f"model {model} not offered"}
        if not info.get("loaded"):
            return {"host": host.name,
                    "error": "model not loaded — refusing to load it for a measurement"}

        # Fixed overhead: a prompt so short that its prefill is noise.
        overhead = statistics.median(
            [(await one(client, base, host.api_key, model, "Reply with OK."))["wall_s"]
             for _ in range(max(1, min(reps, 3)))])

        rows = []
        for size in sizes:
            samples = []
            for _ in range(1 if dry else reps):
                prompt = random_prompt(size, rng)
                try:
                    sample = await one(client, base, host.api_key, model, prompt)
                except (httpx.HTTPError, ValueError) as exc:
                    samples.append({"error": f"{type(exc).__name__}: {exc}"})
                    continue
                prefill = max(sample["wall_s"] - overhead, 1e-3)
                tokens = sample["prompt_tokens"] or 0
                samples.append({"target_tokens": size, "prompt_tokens": tokens,
                                "wall_s": round(sample["wall_s"], 3),
                                "prefill_s": round(prefill, 3),
                                "pp_tps": round(tokens / prefill, 1) if tokens else None})
            ok = [s for s in samples if s.get("pp_tps")]
            rates = [s["pp_tps"] for s in ok]
            tokens = [s["prompt_tokens"] for s in ok]
            rows.append({
                "target_tokens": size,
                "prompt_tokens_median": round(statistics.median(tokens)) if tokens else None,
                "pp_tps_median": round(statistics.median(rates), 1) if rates else None,
                "pp_tps_min": min(rates) if rates else None,
                "pp_tps_max": max(rates) if rates else None,
                "valid_runs": len(ok),
                "samples": samples,
            })
        return {"host": host.name, "address": base, "model": model,
                "overhead_s": round(overhead, 3), "sizes": rows, "dry_run": dry}


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    hosts = [h for h in cfg.enabled_hosts() if h.backend == "omlx"]
    if args.host:
        hosts = [h for h in hosts if h.name in args.host]
    if not hosts:
        print("no omlx host selected", file=sys.stderr)
        return 2

    sizes = [200] if args.dry_run else args.sizes
    print(f"Prefill measurement — model {args.model}, sizes {sizes}, "
          f"{args.reps} repetition(s), random prompts (cold)"
          f"{' (DRY RUN)' if args.dry_run else ''}\n")

    results = await asyncio.gather(
        *(run_host(h, args.model, sizes, args.reps, args.dry_run) for h in hosts))

    for r in results:
        if r.get("error"):
            print(f"[{r['host']}] ERROR: {r['error']}")
            continue
        print(f"[{r['host']}] {r['address']}  overhead {r['overhead_s']} s")
        for row in r["sizes"]:
            print(f"  {row['target_tokens']:>6} target → {row['prompt_tokens_median']:>6} tokens: "
                  f"{row['pp_tps_median']} tok/s (min {row['pp_tps_min']}, "
                  f"max {row['pp_tps_max']}) valid {row['valid_runs']}")
        print()

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"prefill-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"model": args.model, "hosts": results}, indent=1))
    print(f"raw data: {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--model", default="DeepSeek-V4.1-Flash-1017-oQ4e-mtp")
    ap.add_argument("--sizes", type=int, nargs="+", default=[2000, 8000, 24000])
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--host", action="append")
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
