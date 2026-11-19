"""Counters, decision ring buffer, request log (FA-13, FA-14, FA-22)."""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

log = logging.getLogger("ripple.request")

# -- tachograph (issue #83) ---------------------------------------------------
# The chart's resolution and horizon: a 5 s bucket, 720 of them, one hour per
# (host, model) — the size the issue fixed so that a 2 s poll does not become
# 1800 mostly-noise points. The *served* window is smaller (15 min by default);
# the ring holds an hour so a wider window costs nothing but a query parameter.
BUCKET_SECONDS = 5
SERIES_BUCKETS = 720
SERIES_WINDOW_SECONDS = 900


def bucket_index(now: float, bucket_s: int = BUCKET_SECONDS) -> int:
    """Which bucket a wall-clock instant falls in. Floor, so a bucket is whole."""
    return int(now // max(1, int(bucket_s)))


def series_points(window_s: float, bucket_s: int = BUCKET_SECONDS) -> int:
    """How many buckets a window covers — at least one, never a fraction."""
    return max(1, int(max(0.0, float(window_s)) // max(1, int(bucket_s))))


# -- the drawn line (issue #96) ----------------------------------------------
# How many empty buckets the pen crosses without lifting: one.
#
# The probe runs every 10 s (`discovery.interval_seconds`) and a bucket is 5 s
# wide, so *every* pair of consecutive samples has exactly one empty bucket
# between it. That empty bucket is the resolution of the data, not a missing
# measurement. A renderer that lifts the pen there draws one `M` per sample and
# not a single `L` — and SVG paints nothing for a subpath of one point: measured
# on the live page, six `M` and no `L`, a chart that had been invisible for days
# while it held data.
#
# Two empty buckets in a row are something else: a probe that did not happen
# (a failed tick, a node that went down). There the line ends, because an absent
# node must not look like a busy one.
#
# The bucket itself stays at 5 s: `SeriesRing` and the concurrency curve (#80)
# are built on that width, and a 10 s bucket would halve the resolution of the
# curve to paper over a drawing bug.
SERIES_HOLD_BUCKETS = 1


def _is_number(value: Any) -> bool:
    """A drawable value: a finite number. `None` is a hole, `bool` is not a point."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def series_path(values: Sequence[Any], x: Callable[[int], float],
                y: Callable[[float], float], *,
                hold: int = SERIES_HOLD_BUCKETS) -> str:
    """The `d` of one line — **the reference the renderer is checked against.**

    `x(i)` and `y(v)` are the geometry; this function decides only where the pen
    goes down and where it lifts, and that decision is the whole of issue #96:

    * a run of at most `hold` empty buckets between two samples is **one**
      segment (`L`), not a new subpath (`M`) — see `SERIES_HOLD_BUCKETS`;
    * a longer run is a measurement that did not happen: the pen lifts, the next
      sample starts a new subpath, and the hole stays a hole;
    * a value that is not a number is never drawn — no zero, no interpolation,
      and the x axis keeps its slot.

    `dashboard.js:seriesPath()` is the same algorithm in JavaScript, because the
    drawing happens in the browser. `tests/test_dashboard.py` compares the two
    point for point, so the duplication is a checked contract and not a second
    opinion: this is the implementation a test can read, the script is the one
    that paints.
    """
    d = ""
    down = False        # the pen is on the paper
    skipped = 0         # empty buckets since the last drawn point
    for i, value in enumerate(values):
        if not _is_number(value):
            if down:
                skipped += 1
                if skipped > max(0, int(hold)):
                    down = False
            continue
        d += ("L" if down else "M") + f"{round(x(i), 1)} {round(y(value), 2)}"
        down = True
        skipped = 0
    return d


@dataclass
class Bucket:
    """One 5 s bucket of samples for one (host, model).

    Several samples fall into the same bucket — the refresh tick runs every 2 s
    while the bucket is 5 s wide — and they are **averaged**, not overwritten:
    the last one is a single moment, the mean is what the bucket is meant to
    say. `n` and `rate` are kept separately so the mean of the counts and the
    mean of the rates are both honest (they are not one measurement).
    """

    index: int
    n_sum: float = 0.0
    n_count: int = 0
    rate_sum: float = 0.0
    rate_count: int = 0

    def add(self, n: float, rate: float | None) -> "Bucket":
        """Fold one sample in. A sample without a rate still counts for `n`.

        `rate=None` means "this sample measured no rate", not "0 tok/s": a
        bucket in which nothing ran has no per-stream rate at all, and a zero
        would be a claim in its own right. Such a sample is left out of the
        rate mean instead of dragging it down.
        """
        self.n_sum += float(n)
        self.n_count += 1
        if rate is not None:
            self.rate_sum += float(rate)
            self.rate_count += 1
        return self

    @property
    def n(self) -> float:
        return self.n_sum / self.n_count if self.n_count else 0.0

    @property
    def rate(self) -> float | None:
        """The mean rate, or `None` when no sample in this bucket measured one."""
        return self.rate_sum / self.rate_count if self.rate_count else None

    @property
    def samples(self) -> int:
        return max(self.n_count, self.rate_count)


def derive_total(n: float, per_thread: float) -> float:
    """`total = n · per_thread` — **the only place this product is computed.**

    The renderer, the optimizer and the external sampler all read `total` from
    the payload; none of them re-derives it, because a second formula is a
    second answer. Both factors are kept at one decimal (`tg_tps` already is),
    so the product is rounded to one as well — the identity then holds to
    within 0.05 tok/s, which is far below the noise of a single request.
    """
    return round(float(n) * float(per_thread), 1)


def _count(value: float) -> float:
    """A mean count as JSON wants it: an int when it is whole, else one decimal."""
    number = round(float(value), 1)
    return int(number) if float(number).is_integer() else number


def series_entry(model: str, n: list, per_thread: list, *,
                 bucket_s: int = BUCKET_SECONDS,
                 window_s: int = SERIES_WINDOW_SECONDS) -> dict[str, Any]:
    """One model's series in the shape `/healthz → hosts[].series` carries.

    Oldest first, `None` where a bucket had no sample (the renderer draws no
    point there — it never interpolates and never drops the slot; a single
    empty bucket between two samples is crossed by the line, see
    `series_path`, because at a 10 s probe against a 5 s bucket it is the
    resolution of the data and not a hole). The three arrays are
    built in one pass over the same index range, so they cannot drift apart in
    length; `total` is derived from the two emitted values, so
    `total[i] == n[i] · per_thread[i]` is true of *this* payload, not of some
    intermediate the caller never sees.

    **A bucket with `n = 0` carries no rate.** `tg_tps` outlives the request
    that measured it, so an idle tick would otherwise be sampled with the rate
    of a *finished* stream: `n=0` next to `per_thread=55.5`, a point that says
    "nothing ran here at 55 tok/s per stream". The two lines then contradict
    each other, and `rate_h(n)` (issue #80) would read that point as measured
    concurrency. So at `n = 0` the per-thread rate is dropped to `None` (the
    dashed line breaks) while `total` stays `0.0` — nothing running really is
    zero throughput, and that is a statement about the fleet, not a per-stream
    rate. A rate without a positive count is not a point either way.
    """
    if len(n) != len(per_thread):
        raise ValueError(f"series {model}: {len(n)} n against {len(per_thread)} rates")
    counts = [None if value is None else _count(value) for value in n]
    rates = [None if value is None else round(float(value), 1) for value in per_thread]
    threads: list = []
    totals: list = []
    for count, rate in zip(counts, rates):
        if count is None:
            threads.append(None)
            totals.append(None)
        elif count == 0:
            # No stream ran in this bucket: total 0.0, no per-thread rate.
            threads.append(None)
            totals.append(0.0)
        elif rate is None:
            # Streams ran, but no rate was measured for them: a gap, not a zero.
            threads.append(None)
            totals.append(None)
        else:
            threads.append(rate)
            totals.append(derive_total(count, rate))
    return {"model": model, "bucket_s": int(bucket_s), "window_s": int(window_s),
            "n": counts, "total": totals, "per_thread": threads}


# -- the server's own measurements (issue #98) -------------------------------
# oMLX reports the truth about every answered request in its `usage` block, and
# until now RIPPLE read two of its fields (the decode rate and the two
# durations) and threw the rest away. The five numbers below are the ones that
# cannot be derived from anything RIPPLE sees: whether the prefix was warm
# (`cached_tokens`), how fast the node really prefilled and decoded
# (`prompt_tokens_per_second`, `generation_tokens_per_second`), and how long the
# client waited for the first token (`time_to_first_token`, seconds).
#
# They are harvested, not estimated. A backend that reports none of them is not
# an error — `usage_observation()` returns `None` and no row is written, so a
# node that stays silent leaves no zero behind that could be read as "the cache
# missed".
USAGE_FIELDS = ("cached_tokens", "prompt_tokens", "prompt_tokens_per_second",
                "generation_tokens_per_second", "time_to_first_token")

# How many observations the rolling mean covers. Eight is the order of a
# conversation's recent turns: long enough that one cold turn does not erase a
# warm node, short enough that a move's first turn is visible in the mean.
USAGE_WINDOW = 8

# The ledger is keyed by (thread, host) and threads come and go; a bound keeps
# a long uptime from turning it into a leak. The oldest row goes first.
USAGE_ROWS_MAX = 512


def _usage_number(value: Any, *, integral: bool = False) -> float | int | None:
    """One `usage` field as a number, or `None`. `bool` is not a measurement."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return int(value) if integral else float(value)


def usage_observation(usage: Any) -> dict[str, Any] | None:
    """The five measured fields of one `usage` block, or `None`.

    Tolerant by contract: a field the backend did not send is `None` in the
    result and never a zero — `cached_tokens: 0` is a statement ("the prefix
    was cold"), a missing field is not. Only when the block carries none of the
    five is the answer `None`, so that a node which reports nothing at all does
    not get a row that says "measured, all unknown".

    `cached_tokens` is read from **both** shapes the server has been seen to
    send it in: at the top of `usage` (the block the issue measured on 19.11.)
    and inside `prompt_tokens_details` (the OpenAI shape, measured live on
    `DeepSeek-V4.1-Flash-1017-oQ4e-mtp`: `prompt_tokens_details.cached_tokens
    = 688`). The nested one wins when both are there — it is the one the
    standard describes, and a top-level copy could be a stale echo.
    """
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    cached = None
    if isinstance(details, dict):
        cached = _usage_number(details.get("cached_tokens"), integral=True)
    if cached is None:
        cached = _usage_number(usage.get("cached_tokens"), integral=True)
    observation = {
        "cached_tokens": cached,
        "prompt_tokens": _usage_number(usage.get("prompt_tokens"), integral=True),
        "prompt_tokens_per_second": _usage_number(usage.get("prompt_tokens_per_second")),
        "generation_tokens_per_second": _usage_number(usage.get("generation_tokens_per_second")),
        "time_to_first_token": _usage_number(usage.get("time_to_first_token")),
    }
    if all(value is None for value in observation.values()):
        return None
    for key in ("prompt_tokens_per_second", "generation_tokens_per_second"):
        if observation[key] is not None:
            observation[key] = round(float(observation[key]), 2)
    if observation["time_to_first_token"] is not None:
        observation["time_to_first_token"] = round(float(observation["time_to_first_token"]), 3)
    return observation


@dataclass
class UsageRow:
    """What one `(thread, host)` pair — or one host — measured.

    `last` is the most recent observation, `mean` the rolling mean over the last
    `USAGE_WINDOW` ones, `samples` how many were folded in. The mean skips a
    field that a sample did not measure instead of counting it as zero, exactly
    like `Bucket.rate`: a missing number must not drag an average down.
    """

    host: str
    samples: int = 0
    last_at: float = 0.0
    last: dict[str, Any] | None = None
    history: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=USAGE_WINDOW))

    def add(self, observation: dict[str, Any], now: float) -> "UsageRow":
        self.samples += 1
        self.last = dict(observation)
        self.last_at = now
        self.history.append(dict(observation))
        return self

    @property
    def mean(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for key in USAGE_FIELDS:
            values = [float(v) for v in (o.get(key) for o in self.history)
                      if v is not None]
            out[key] = round(sum(values) / len(values), 2) if values else None
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"host": self.host, "samples": self.samples,
                "last_at": round(self.last_at, 3),
                "last": dict(self.last or {}), "mean": self.mean}


class UsageLedger:
    """The server's own measurements per `(thread, host)` and per host (#98).

    Two levels, one write: `rows` answers "what did this thread cost on this
    node", `hosts` answers "what does this node deliver" — the second is also
    where a request without a thread lands. A short request holds no slot
    (FA-7) and therefore has no thread row; its numbers still count for the
    node, because a node that only ever served one-shot requests must not look
    unmeasured.

    A thread that moves hosts keeps its old row. That is the point: the first
    turn on the new node with `cached_tokens ≈ 0` next to the last turn on the
    old one with `cached_tokens ≈ prompt_tokens` is the price of the move, and
    it is only visible because both rows survive.
    """

    def __init__(self, window: int = USAGE_WINDOW, max_rows: int = USAGE_ROWS_MAX) -> None:
        self.rows: dict[tuple[str, str], UsageRow] = {}
        self.hosts: dict[str, UsageRow] = {}
        self._max_rows = max(1, int(max_rows))
        self._window = max(1, int(window))

    def record(self, *, host: str, usage: Any, thread: str | None = None,
               now: float | None = None) -> dict[str, Any] | None:
        """Fold one response's `usage` in. Returns what was read, or `None`."""
        observation = usage_observation(usage)
        if observation is None:
            return None
        moment = time.time() if now is None else float(now)
        self._row(self.hosts, host, host).add(observation, moment)
        if thread:
            self._row(self.rows, (thread, host), host).add(observation, moment)
            self._prune()
        return observation

    def _row(self, table: dict, key: Any, host: str) -> UsageRow:
        row = table.get(key)
        if row is None:
            row = UsageRow(host=host, history=deque(maxlen=self._window))
            table[key] = row
        return row

    def _prune(self) -> None:
        while len(self.rows) > self._max_rows:
            oldest = min(self.rows, key=lambda k: self.rows[k].last_at)
            del self.rows[oldest]

    def thread_row(self, thread: str | None, host: str) -> dict[str, Any] | None:
        if not thread:
            return None
        row = self.rows.get((thread, host))
        return row.as_dict() if row is not None else None

    def thread_history(self, thread: str | None, host: str) -> list[dict[str, Any]]:
        """The same thread's rows on the *other* nodes, newest first."""
        if not thread:
            return []
        others = [row for (key, name), row in self.rows.items()
                  if key == thread and name != host]
        return [row.as_dict() for row in sorted(others, key=lambda r: r.last_at,
                                                reverse=True)]

    def host_row(self, host: str) -> dict[str, Any] | None:
        row = self.hosts.get(host)
        return row.as_dict() if row is not None else None

    def snapshot(self) -> dict[str, Any]:
        return {"rows": [row.as_dict() for row in
                         sorted(self.rows.values(), key=lambda r: r.last_at, reverse=True)],
                "hosts": {name: row.as_dict() for name, row in self.hosts.items()}}


@dataclass
class Stats:
    started_at: float = field(default_factory=time.time)

    requests_total: int = 0
    requests_ok: int = 0
    errors_total: int = 0
    http_503_total: int = 0
    http_507_total: int = 0
    threads_new: int = 0
    thread_hits: int = 0
    threads_evicted: int = 0
    # Of the evicted threads: how many belonged to a batch key. That is the
    # number that says whether the interactive-first order actually fires.
    threads_evicted_batch: int = 0
    # Batch threads that an interactive request adopted by prefix match and so
    # raised to interactive (own TTL dropped, eviction order normal again).
    threads_promoted: int = 0
    threads_rebound: int = 0
    # Threads the operator deleted from the page before their TTL ran out
    # (issue #35). A separate counter from `threads_evicted`: an eviction is the
    # slot limit doing its job, a delete is a person deciding a conversation's
    # prefix cache is not worth the slot.
    threads_deleted: int = 0
    # Requests from a `one-shot` key: they never held a slot, by construction.
    requests_one_shot: int = 0
    auth_failures: int = 0
    # Admin requests refused by the failed-attempt limiter (issue #6): the
    # address is over budget, so its key was not even compared. `auth_failures`
    # counts the 401s that *did* compare, so the two numbers answer different
    # questions — "wrong key" against "no longer being asked".
    admin_key_blocks: int = 0
    by_key: dict[str, int] = field(default_factory=dict)
    prefix_matches: int = 0
    spillover: int = 0
    fallbacks: int = 0
    addr_switches: int = 0
    evict_idle: int = 0
    cold_load: int = 0
    # Decisions in which a non-zero prefill bias was applied (deficit binding of
    # a prompt at or above `placement.prefill_bias.min_prompt_tokens` on a fleet
    # with at least two known `pp_tps`). Says whether the new term is doing work
    # at all — a fleet without `pp_tps` never moves this counter.
    prefill_bias: int = 0
    # Loading state (docs/loading-state.md §8). A cold load RIPPLE caused itself
    # suspends that host's probes; `probes_skipped` counts the probes that did not
    # happen because of it, so a suspended host cannot hide behind silence.
    loads_supervised: int = 0
    probes_skipped: int = 0
    # Supervised loads that ended without the model becoming resident: oMLX
    # stopped reporting the load and left the model at `loaded: false`, or
    # dropped it from the node. A successful load and a residency failure end the
    # state the same way, so this is the only number that tells them apart.
    loads_failed: int = 0
    # Placement *decisions* that chose a host with 1..flap_guard-1 consecutive
    # failed probes — not requests served from one: `choose()` also runs for a
    # retry hop and for a fallback, so one client request can count twice.
    # Non-zero means the flap guard is carrying traffic that used to be refused.
    degraded_pool_decisions: int = 0
    calibrations_started: int = 0
    calibrations_ok: int = 0
    # Calibrations that only ran because the operator passed `force` past a bound
    # thread — the one case where a session could lose its prefix cache.
    calibrations_forced: int = 0
    by_host: dict[str, dict[str, int]] = field(default_factory=dict)
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    # 200 entries (issue #101, was 50): a conversation spans more turns than the
    # old ring held, so filtering the log by sid would have shown a fragment of
    # the session. At the measured 293 bytes per entry that is ~59 kB in
    # `/stats` per poll — accepted, the payload is still one `json.dumps`.
    decisions: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))

    def host(self, name: str) -> dict[str, int]:
        return self.by_host.setdefault(name, {"requests": 0, "errors": 0, "http_5xx": 0})

    def model(self, name: str) -> dict[str, int]:
        return self.by_model.setdefault(name, {"requests": 0, "errors": 0})

    def decision(self, *, model: str, host: str | None, reason: str, prompt_chars: int,
                 duration_ms: float, status: int, thread: str | None = None,
                 thread_source: str | None = None, client: str | None = None,
                 prompt_tokens: int = 0, extra: str | None = None) -> None:
        entry = {
            "ts": round(time.time(), 3),
            "time": time.strftime("%H:%M:%S"),
            "model": model,
            "host": host,
            "reason": reason,
            "prompt_chars": prompt_chars,
            "prompt_tokens": prompt_tokens,
            "duration_ms": round(duration_ms, 1),
            "status": status,
            "client": client,
            "thread": thread,
            "thread_source": thread_source,
        }
        if extra:
            entry["note"] = extra
        self.decisions.append(entry)

    def as_dict(self) -> dict[str, Any]:
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "requests_total": self.requests_total,
            "requests_ok": self.requests_ok,
            "errors_total": self.errors_total,
            "http_503_total": self.http_503_total,
            "http_507_total": self.http_507_total,
            "threads_new": self.threads_new,
            "thread_hits": self.thread_hits,
            "threads_evicted": self.threads_evicted,
            "threads_evicted_batch": self.threads_evicted_batch,
            "threads_promoted": self.threads_promoted,
            "threads_rebound": self.threads_rebound,
            "threads_deleted": self.threads_deleted,
            "requests_one_shot": self.requests_one_shot,
            "auth_failures": self.auth_failures,
            "admin_key_blocks": self.admin_key_blocks,
            "by_key": self.by_key,
            "prefix_matches": self.prefix_matches,
            "spillover": self.spillover,
            "fallbacks": self.fallbacks,
            "addr_switches": self.addr_switches,
            "evict_idle": self.evict_idle,
            "cold_load": self.cold_load,
            "prefill_bias": self.prefill_bias,
            "loads_supervised": self.loads_supervised,
            "probes_skipped": self.probes_skipped,
            "loads_failed": self.loads_failed,
            "degraded_pool_decisions": self.degraded_pool_decisions,
            "calibrations_started": self.calibrations_started,
            "calibrations_ok": self.calibrations_ok,
            "calibrations_forced": self.calibrations_forced,
            "by_host": self.by_host,
            "by_model": self.by_model,
            "decisions": list(self.decisions),
        }


def log_request(*, model: str, host: str | None, reason: str, prompt_chars: int,
                duration_ms: float, status: int, thread: str | None = None,
                thread_source: str | None = None, client: str | None = None,
                prompt_tokens: int = 0, note: str | None = None,
                decision: str | None = None) -> None:
    """Exactly one line per request (FA-14). No prompt content, no keys.

    `client` is the address the request came from — the transport peer, or the
    `X-Forwarded-For` entry a *trusted* proxy appended (see
    `Ripple.client_address`); a forged header never reaches this line as an
    origin. `thread_source` says whether the thread id next to it was the
    client's own claim (`sid`, from `affinity.header`) or a guess from the
    prompt (`fp`) — without it the two are indistinguishable in the log. A
    third value, `sid+fp`, marks the case where a sid thread was reached by
    prompt similarity instead of the header (issue #68): the id is the
    client's, the source this request used was not.

    `prompt_chars` is the length of the *user* text (the prefix-match text) and
    `prompt_tokens` the estimate for the whole request — system prompt, history
    and tool schema included. They differ by orders of magnitude on an agent
    turn, and only the second one decides placement; both are on the line so the
    difference is visible instead of having to be inferred from oMLX's log
    (issue #14: `prompt_chars=101` against oMLX's `prompt: 83192`).

    `note` is the proxy's own remark (stream/retry), `decision` is the placement
    layer's note — the text that explains *why* this host: a prefill bias, a
    spill, an evicted thread. Without it a decision that the bias made is only
    visible as a counter, not as a reason.
    """
    log.info(
        "model=%s host=%s reason=%s client=%s thread=%s thread_source=%s "
        "prompt_chars=%d prompt_tokens=%d duration_ms=%.1f status=%d%s%s",
        model, host or "-", reason, client or "-", thread or "-",
        thread_source or "-", prompt_chars, prompt_tokens, duration_ms, status,
        f" note={note}" if note else "",
        f" decision={decision!r}" if decision else "",
    )
