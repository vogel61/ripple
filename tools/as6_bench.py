#!/usr/bin/env python3
"""AS-6: measure decode throughput per (host, model) and justify the weights.

The weights 3:2 are a specification, not a measurement (RB-6). This tool
measures them: the same prompt, the same length, once per host, and reports the
ratio of decode tokens per second. That ratio is what the weights should follow.

It talks to the oMLX hosts **directly**, not through RIPPLE — the point is to
measure the machines, not the proxy. It refuses to touch a model that is not
already loaded: loading a 400 GB model costs 60-70 s and can evict a running
session (FA-18).

Usage:
    ./.venv/bin/python tools/as6_bench.py --config config.yaml --dry-run
    ./.venv/bin/python tools/as6_bench.py --config config.yaml --runs 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ripple.config import load_config  # noqa: E402


FILLER = (
    "The quick brown fox jumps over the lazy dog. "
    "A proxy decides where a conversation lives, and a conversation that moves "
    "loses its prefix cache. "
)


def build_prompt(target_tokens: int, marker: str) -> str:
    """A prompt of roughly `target_tokens` tokens that forces a long answer.

    Asking for "one word" is not measurable — a well-behaved model answers with
    one token and decode throughput becomes noise (observed: the M3 returned
    1 token, the M5 rambled for 256). The model is therefore asked to repeat a
    block of text verbatim, which it cannot do in fewer tokens than the block is
    long.
    """
    chars = max(200, int(target_tokens * 5.1))
    body = (FILLER * (chars // len(FILLER) + 1))[:chars]
    return (
        f"Reference block for measurement {marker}:\n\n{body}\n\n"
        "Repeat the reference block above verbatim, word for word, without any "
        "introduction, commentary or extra whitespace. Start immediately with "
        "the first word."
    )


async def loaded_models(client: httpx.AsyncClient, base: str, key: str) -> dict:
    r = await client.get(f"{base}/v1/models/status",
                         headers={"authorization": f"Bearer {key}"} if key else {})
    r.raise_for_status()
    data = r.json()
    return {m["id"]: m for m in data.get("models", [])}


async def measure(client: httpx.AsyncClient, base: str, key: str, model: str,
                  prompt: str, max_tokens: int) -> dict:
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    t0 = time.perf_counter()
    r = await client.post(f"{base}/v1/chat/completions", json=payload, headers=headers,
                          timeout=1800.0)
    wall = time.perf_counter() - t0
    r.raise_for_status()
    data = r.json()
    usage = data.get("usage") or {}
    completion = usage.get("completion_tokens") or 0
    ttft = usage.get("time_to_first_token")
    total = usage.get("total_time")
    decode_seconds = None
    if isinstance(total, (int, float)) and isinstance(ttft, (int, float)):
        decode_seconds = max(total - ttft, 1e-6)
    elif completion:
        decode_seconds = max(wall - (ttft or 0), 1e-6)
    # A one-token answer says nothing about decode throughput: below the
    # minimum the sample is marked invalid instead of producing 1e6 tok/s.
    valid = bool(completion >= 64 and decode_seconds and decode_seconds >= 0.2)
    tg = None
    if valid and decode_seconds:
        tg = completion / decode_seconds
    return {
        "valid": valid,
        "completion_tokens": completion,
        "prompt_tokens": usage.get("prompt_tokens"),
        "ttft_s": ttft,
        "total_s": total,
        "wall_s": round(wall, 3),
        "decode_s": round(decode_seconds, 4) if decode_seconds else None,
        "tg_tps": round(tg, 1) if tg else None,
    }


async def run_host(cfg, host, model: str, runs: int, prompt_tokens: int,
                   max_tokens: int, dry: bool) -> dict:
    address = host.addresses[0].rstrip("/")
    async with httpx.AsyncClient() as client:
        try:
            models = await loaded_models(client, address, host.api_key)
        except (httpx.HTTPError, ValueError) as exc:
            return {"host": host.name, "error": f"{type(exc).__name__}: {exc}"}
        info = models.get(model)
        if info is None:
            return {"host": host.name, "error": f"model {model} not offered"}
        if not info.get("loaded"):
            return {"host": host.name,
                    "error": "model not loaded — refusing to load it for a measurement "
                             "(would cost 60-70 s and can evict a running session)"}

        samples = []
        for i in range(runs):
            prompt = build_prompt(prompt_tokens, f"run-{i}")
            try:
                samples.append(await measure(client, address, host.api_key, model,
                                             prompt, max_tokens))
            except (httpx.HTTPError, ValueError) as exc:
                samples.append({"error": f"{type(exc).__name__}: {exc}"})
        ok = [s for s in samples if s.get("tg_tps")]
        tg = [s["tg_tps"] for s in ok]
        pp = [s["prompt_tokens"] for s in ok if s.get("prompt_tokens")]
        return {
            "host": host.name,
            "address": address,
            "weight": host.weight,
            "model": model,
            "estimated_size": info.get("estimated_size"),
            "runs": runs,
            "valid_runs": len(ok),
            "invalid_runs": runs - len(ok),
            "prompt_tokens_measured": (round(statistics.mean(pp)) if pp else None),
            "tg_tps_median": round(statistics.median(tg), 1) if tg else None,
            "tg_tps_min": min(tg) if tg else None,
            "tg_tps_max": max(tg) if tg else None,
            "ttft_median_s": (round(statistics.median([s["ttft_s"] for s in ok]), 3)
                              if ok and ok[0].get("ttft_s") is not None else None),
            "samples": samples,
            "dry_run": dry,
        }


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    hosts = [h for h in cfg.enabled_hosts() if h.backend == "omlx"]
    if args.host:
        hosts = [h for h in hosts if h.name in args.host]
    if not hosts:
        print("no omlx host selected", file=sys.stderr)
        return 2

    runs = 1 if args.dry_run else args.runs
    prompt_tokens = 200 if args.dry_run else args.prompt_tokens
    max_tokens = 16 if args.dry_run else args.max_tokens

    print(f"AS-6 measurement — model {args.model}, {runs} run(s) per host, "
          f"~{prompt_tokens} prompt tokens, {max_tokens} completion tokens"
          f"{' (DRY RUN)' if args.dry_run else ''}\n")

    results = await asyncio.gather(
        *(run_host(cfg, h, args.model, runs, prompt_tokens, max_tokens, args.dry_run)
          for h in hosts))

    for r in results:
        if r.get("error"):
            print(f"[{r['host']}] ERROR: {r['error']}")
            continue
        print(f"[{r['host']}] {r['address']}  weight {r['weight']}")
        print(f"  decode: {r['tg_tps_median']} tok/s (min {r['tg_tps_min']}, "
              f"max {r['tg_tps_max']})  ttft {r['ttft_median_s']} s  "
              f"prompt {r['prompt_tokens_measured']} tokens  "
              f"valid {r['valid_runs']}/{r['runs']}")
        print(f"  estimated_size: {r['estimated_size']} bytes")
        print()

    ok = [r for r in results if r.get("tg_tps_median")]
    if len(ok) == 2:
        a, b = ok[0], ok[1]
        ratio = a["tg_tps_median"] / b["tg_tps_median"]
        weight_ratio = (a["weight"] / b["weight"]) if b["weight"] else None
        print(f"measured ratio {a['host']} : {b['host']} = {ratio:.2f} : 1")
        if weight_ratio:
            print(f"configured weights            = {weight_ratio:.2f} : 1")
            delta = abs(ratio - weight_ratio) / weight_ratio
            verdict = "confirmed" if delta <= 0.15 else "DEVIATES — document it"
            print(f"deviation {delta:.0%} → weights {verdict}")

    out = REPO / "tools" / "as6" / "results"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out / f"as6-{stamp}.json"
    path.write_text(json.dumps({"model": args.model, "results": results,
                                "dry_run": args.dry_run}, indent=2), encoding="utf-8")
    print(f"\nraw data: {path}")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--model", default="Qwen3.5-9B-oQ4e-mtp",
                   help="model to measure (must already be loaded on every host)")
    p.add_argument("--runs", type=int, default=10, help="runs per host (AS-6 asks for 10)")
    p.add_argument("--prompt-tokens", type=int, default=8000,
                   help="approximate prompt size in tokens (AS-6 asks for 8k)")
    p.add_argument("--max-tokens", type=int, default=256,
                   help="completion tokens per run (decode measurement)")
    p.add_argument("--host", action="append", help="limit to this host name (repeatable)")
    p.add_argument("--dry-run", action="store_true",
                   help="one tiny request per host to validate the wiring")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
