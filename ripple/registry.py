"""Thread registry: identity, prefix similarity, slots per model (FA-6, FA-7, FA-19).

The OpenAI API is stateless — there is no field that identifies a
conversation. A thread is recognised in this order:

1. explicit header (`X-Session-Id`, configurable) — exact and preferred;
2. fingerprint over model + first user message (the system prompt is excluded:
   it carries timestamps and tool lists and would change every turn);
3. the shared prefix, so a compressed conversation keeps its binding.

Inside a header's family the shared prefix is **counted in blocks of a chained
hash** (issue #99), not measured as a ratio: see `block_chain()`.

Everything here is volatile by design: a persisted slot count would be a lie
after a restart (§5.1, "what a host does not store").
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import AffinityConfig

# How much of a thread key leaves the process: the request log, `/stats`,
# `/healthz` and the `x-ripple-thread` header all carry `Thread.label`. 24 is
# not decoration — a `sid:` key is `sid:` plus a client id, and Hermes sends
# `YYYYMMDD_HHMMSS_<random>`: the whole timestamp has to fit behind the prefix
# or two sessions of the same hour collapse into one string in the log
# (issue #68; 16 gave `sid:20261117_150`, which is every session that hour).
LABEL_CHARS = 24

# The suffix of a work-unit key: `#` plus the 8 hex characters of the spawn
# fingerprint (issue #92). A label that cut it off would show two identical rows
# for two different threads on the Threads tab, because the family part of the
# key is the same for all of them.
LABEL_SUFFIX_CHARS = 9

# The separator between a session key and the fingerprint of a spawned work unit.
UNIT_SEP = "#"

# -- the identity chain (issue #99) -----------------------------------------
#
# vLLM and oMLX decide "how much of this prompt have I already computed" from a
# chained block hash: `h_i = sha256(h_{i-1} || block_i)`, so a block's key
# depends on every block before it and two conversations cannot collide from
# the first differing block onwards. The prompt text is the identity; no
# counter and no similarity is involved.
#
# The block is a *character* block, because a tokeniser here would need the
# chat template (FA-6) — the same reason `estimate_tokens()` divides by four.
# 32 768 characters is the choice that makes one block ≈ one of oMLX's 8192
# token blocks at 4 chars/token: the number we count is then the number oMLX
# caches, and a shared block count can be compared with `cached_tokens` (#98).
BLOCK_CHARS = 32_768

# The chain's seed. The model name is in it because KV blocks are per model:
# two models must never share a chain, however identical their prompts are.
CHAIN_SEED = b"ripple-chain-v1\x00"

# Four characters per token. An **assumption, not a measurement**: it is the
# same rule `estimate_tokens()` applies, it is wrong by ±20 % on code and on
# JSON, and the joint test against oMLX's `cached_tokens` (#98) is what would
# replace it with a real number. Every cost that comes out of it inherits the
# error, so it is a named constant rather than a `/ 4` in the middle of an
# expression.
CHARS_PER_TOKEN = 4

# How many shared blocks make a request "the same thread" inside a header's
# family. One: a block is ~8192 tokens of prefill, which is seconds on this
# fleet — a thread that can reuse it is worth keeping. The ratio this replaces
# needed a threshold because a ratio is dimensionless; a block count is a
# quantity, and 0 is the only honest "nothing in common" (a spawn measured
# 8 tokens = 0 blocks).
MIN_SHARED_BLOCKS = 1


@dataclass(frozen=True)
class BlockChain:
    """The chained block hashes of one prompt, and the text they describe.

    `blocks[i]` is the hash of block *i* **and of every block before it**, so
    two chains that agree on `blocks[i]` agree on the first `(i+1) *
    block_chars` characters. That is the whole point: the comparison is an
    equality walk, and its result is a count, not a score.

    `tail` is what did not fill a whole block (< `block_chars` characters). It
    is kept because a conversation grows: the next turn's text is the last
    turn's text plus a message, so the stored text's last, partial block is
    still a prefix of the new prompt — that is the boundary case `shared_blocks`
    counts as one block, and without it every short prompt would be a new
    thread on every turn.
    """

    model: str
    block_chars: int
    blocks: tuple[str, ...]
    tail: str
    chars: int

    @property
    def full_chars(self) -> int:
        """Characters covered by whole blocks — where `tail` starts."""
        return len(self.blocks) * self.block_chars


def block_chain(text: str, model: str, block_chars: int = BLOCK_CHARS) -> BlockChain:
    """Chained block hashes of *text*: `h_i = sha256(h_{i-1} || block_i)`.

    `h_{-1}` is `CHAIN_SEED + model`, so two models never share a chain.

    Only **whole** blocks enter the chain; the remainder is carried as
    `tail` and compared by content at the boundary (see `shared_blocks`).

    `text` is `prompt_text(body)` — the conversation, **not** the whole
    request. The system prompt is deliberately not in it (existing behaviour):
    it carries a timestamp and a tool list and changes every turn, so hashing
    it would make every session look like every other one. The price is that
    oMLX's `cached_tokens` will exceed what this chain predicts whenever a long
    system prompt is shared — a difference to explain, not to "fix" by hashing
    it in.
    """
    if block_chars <= 0:
        raise ValueError(f"block_chars must be > 0, got {block_chars}")
    digest = hashlib.sha256(CHAIN_SEED + model.encode("utf-8", "replace")).digest()
    blocks: list[str] = []
    full = len(text) - len(text) % block_chars
    for start in range(0, full, block_chars):
        block = text[start:start + block_chars].encode("utf-8", "replace")
        digest = hashlib.sha256(digest + block).digest()
        blocks.append(digest.hex())
    return BlockChain(model=model, block_chars=block_chars, blocks=tuple(blocks),
                      tail=text[full:], chars=len(text))


def shared_blocks(stored: BlockChain | None, text: str,
                  chain: BlockChain | None = None) -> int:
    """How many leading blocks *text* shares with the stored chain.

    A linear equality walk over the two chains — the longest common prefix of
    the hashes, which is the longest common prefix of the blocks.

    The **boundary** is the one place where this is not a plain hash
    comparison: when every whole block of the stored text matched and the
    stored text is a prefix of *text*, the stored text's partial last block
    counts as one more shared block. That is what makes a grown prompt
    (last turn + one more message) match its thread; it is also the source of
    the documented ±1-block inexactness, because character blocks do not align
    with token blocks.

    Incomparable means 0: a chain from another model or another block size
    describes different KV blocks and must never be matched against this one.
    """
    if stored is None or not stored.chars:
        return 0
    if chain is None:
        chain = block_chain(text, stored.model, stored.block_chars)
    if chain.model != stored.model or chain.block_chars != stored.block_chars:
        return 0
    limit = min(len(stored.blocks), len(chain.blocks))
    n = 0
    while n < limit and stored.blocks[n] == chain.blocks[n]:
        n += 1
    if (n == len(stored.blocks) and stored.tail
            and len(text) >= stored.chars
            and text[stored.full_chars:stored.chars] == stored.tail):
        n += 1
    return n


@dataclass(frozen=True)
class PrefillEstimate:
    """What a request still has to prefill, from a shared block count.

    `prefill_tokens` is the number #95 wants: a move costs one prefill of what
    is *not* cached on the destination.
    """

    shared_blocks: int
    shared_chars: int
    shared_tokens: int
    prefill_tokens: int


def prefill_estimate(prompt_tokens: int, shared: int, *,
                     block_chars: int = BLOCK_CHARS,
                     cap_chars: int | None = None) -> PrefillEstimate:
    """`(shared_chars, shared_tokens, prefill_tokens)` from a block count.

    `shared_chars = shared_blocks * block_chars`, capped at `cap_chars` (the
    shorter of the two prompts) so a counted boundary block cannot claim more
    characters than either side actually has.

    The division by `CHARS_PER_TOKEN` is an assumption; the block count is not.
    Character blocks do not align with token blocks, so this is exact in blocks
    and ±1 block at the boundary.
    """
    shared = max(0, int(shared))
    chars = shared * block_chars
    if cap_chars is not None:
        chars = min(chars, max(0, int(cap_chars)))
    shared_tokens = chars // CHARS_PER_TOKEN
    return PrefillEstimate(shared_blocks=shared, shared_chars=chars,
                           shared_tokens=shared_tokens,
                           prefill_tokens=max(0, int(prompt_tokens) - shared_tokens))


@dataclass
class Thread:
    key: str
    model: str
    host: str
    prefix: str
    source: str            # header | fingerprint | prefix_match
    created: float
    last_seen: float
    # The chained block hashes of the text that opened the thread (issue #99).
    # `prefix` above stays what it was — the first `cfg.prefix_chars`
    # characters, which the fleet-wide `best_prefix_match` still compares as a
    # ratio — but the header path no longer looks at it: it counts shared
    # blocks. None means "no chain" (a `Thread` built by hand in a test, or one
    # that predates the chain); such a thread shares 0 blocks, it is not a
    # candidate. Nothing is persisted, so this is a clean cut: the registry
    # dies with the process and every thread of a restarted RIPPLE is born with
    # its chain.
    chain: BlockChain | None = None
    turns: int = 1
    holds_slot: bool = True
    # Requests currently running on this thread. Only a thread with zero is ever
    # allowed to give up its slot (see ThreadRegistry.evictable).
    active: int = 0
    # When this thread was last moved to another host (rebind cost model).
    last_rebind: float = 0.0
    # --- the arriving-turn decision (issue #95, §6.4) ---------------------
    # Where this thread came from, and when. A move *back* to that host inside
    # `placement.rebind.per_thread_cooldown_seconds` has to clear a higher bar
    # (`placement.migrate.return_factor`): the band is symmetric, so direction
    # is the one thing it cannot see, and a reversal is exactly the oscillation
    # that would undo a move for two prefills. `moved_at` also opens the move
    # window the pin counts in.
    moved_from: str | None = None
    moved_at: float = 0.0
    # Moves this thread has taken inside that window, and the pin they earn:
    # `max_moves_per_thread` moves pin the conversation until `pinned_until`,
    # because the fleet-wide `rebind.max_per_hour` bounds the fleet and one long
    # conversation can spend it. Visible in `/healthz → threads.registry[]`.
    move_count: int = 0
    pinned_until: float = 0.0
    # Prompt size of the **last** request on this thread, in tokens — the price
    # of moving it, because a move costs exactly one prefill on the destination
    # (`prompt_tokens / pp_tps`, see `placement.plan_rebind`). Overwritten on
    # every request, never summed: the number of turns says nothing about the
    # price. 200 turns after a compression are cheap; a single Hermes turn can
    # carry 83 192 prompt tokens. 0 means **unknown**, and an unknown size is
    # never moved on a guessed price.
    last_prompt_tokens: int = 0
    # The traffic class of the key that opened the thread. Kept because the
    # eviction order reads it (an interactive request evicts a batch thread
    # first) and because rebind is disabled for batch threads, not for
    # enforcement. A batch thread that an interactive request adopts by prefix
    # match is promoted once (ThreadRegistry.promote) — there is no way back.
    kind: str = "interactive"
    # Own TTL in seconds, or None for the global `affinity.ttl_seconds`. A batch
    # thread carries its shorter TTL itself: the expiry check runs per thread, so
    # two classes of thread can age differently in one registry.
    ttl_seconds: float | None = None
    # The client key that opened the thread, by name. None means the request
    # arrived without a key (loopback under `auth.allow_loopback`) — it is not
    # "unknown": an anonymous thread is a fact the dashboard can state.
    key_name: str | None = None

    @property
    def label(self) -> str:
        """The first `LABEL_CHARS` characters — but never at the cost of the
        work-unit suffix (issue #92).

        A key of a spawned unit is `<session>#<fp8>`; its family part alone is
        already long enough that a plain `key[:24]` cuts exactly the `#<fp8>`
        off, and two spawns of one session would render as two identical rows.
        So for a hash key the family part gives way instead: the label stays 24
        characters and ends with the `#` plus its 8 hex. Keys without a `#` are
        untouched, byte for byte (issue #68).
        """
        if UNIT_SEP not in self.key:
            return self.key[:LABEL_CHARS]
        if len(self.key) <= LABEL_CHARS:
            return self.key
        return (self.key[: LABEL_CHARS - LABEL_SUFFIX_CHARS]
                + self.key[-LABEL_SUFFIX_CHARS:])

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created


def _content_text(content: Any) -> str:
    """Flatten a message content (string or list of typed parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif p.get("type") == "image_url":
                    parts.append("[image]")
        return "".join(parts)
    return ""


def user_messages(body: dict[str, Any]) -> list[str]:
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return []
    out = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            out.append(_content_text(m.get("content")))
    return out


def prompt_text(body: dict[str, Any]) -> str:
    """Everything the user contributed, in order — system prompts excluded.
    This is the text the prefix comparison runs on."""
    users = user_messages(body)
    if users:
        return "\n".join(users)
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        return "\n".join(str(p) for p in prompt)
    return ""


def prompt_size_text(body: dict[str, Any]) -> str:
    """Everything that ends up in the prompt — the text the *size* is read from.

    `prompt_text()` answers a different question: "what did the user write",
    because the prefix match must not see a system prompt that carries
    timestamps and a tool list and changes every turn. The size of a request is
    not that. oMLX prefills the system prompt, the tool schema and the whole
    history as well, so sizing from `prompt_text()` understates a real agent
    turn by three orders of magnitude: a Hermes turn of 83 192 prompt tokens
    with a 101-character user message looked like **27 tokens** and was
    therefore "ephemeral" — it held no slot, never bound, and the prefill bias
    saw a prompt below `min_prompt_tokens` (issue #14).

    Counted here: the content of every message whatever its role (system,
    user, assistant, tool), the tool calls an assistant message carries, the
    `tools` schema, and `prompt`/`input` for the completion-style endpoints.
    Still a character estimate — `estimate_tokens()` applies the 4 chars/token
    rule to it — because a real tokeniser would need the chat template (FA-6).
    """
    parts: list[str] = []
    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            parts.append(_content_text(m.get("content")))
            # A tool call is prompt too: its name and its arguments are
            # re-rendered into the next prompt by the chat template.
            calls = m.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    fn = call.get("function") if isinstance(call, dict) else None
                    if isinstance(fn, dict):
                        parts.append(str(fn.get("name") or ""))
                        parts.append(str(fn.get("arguments") or ""))
            if isinstance(m.get("name"), str):
                parts.append(m["name"])
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        parts.append(prompt)
    elif isinstance(prompt, list):
        parts.extend(str(p) for p in prompt)
    for key in ("input", "instructions"):
        value = body.get(key)
        if isinstance(value, str):
            parts.append(value)
    tools = body.get("tools")
    if tools:
        # The schema is part of the prompt oMLX prefills, and on an agent turn
        # it is not small. Sorted so the estimate is stable across requests.
        parts.append(json.dumps(tools, sort_keys=True, default=str))
    return "\n".join(p for p in parts if p)


def estimate_tokens(text: str) -> int:
    """Cheap character-based estimate (4 chars/token). Tokenising in the router
    would need the chat template and buy nothing (FA-6)."""
    return max(1, len(text) // 4)


def fingerprint(model: str, body: dict[str, Any]) -> str | None:
    users = user_messages(body)
    if not users:
        return None
    h = hashlib.sha256()
    h.update(model.encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(users[0].encode("utf-8", "replace"))
    return h.hexdigest()[:32]


def unit_fingerprint(text: str, prefix_chars: int) -> str:
    """8 hex characters naming one spawned work unit (issue #92).

    sha256 over the same slice of the prompt the prefix comparison stores
    (`text[:cfg.prefix_chars]`), so the name is a fact about the request and
    nothing else: no counter, no clock, no state. Two requests with the same
    opening get the same name — which is the honest limit of a fingerprint, see
    `ThreadRegistry.key_for_family`.

    Since issue #99 the chain (`block_chain`) decides *whether* a request
    belongs to a thread; this still decides what a new thread is *called*. The
    shape is part of the wire contract — `sid:<header>#<8 lowercase hex>`, 9
    characters of suffix that `dashboard.js` encodes and `proxy.py` resolves —
    so it is not derived from the chain tail: that would be a different name
    for the same request, and three tests (and every log line) spell the old
    one.
    """
    return hashlib.sha256(text[:prefix_chars].encode("utf-8", "replace")).hexdigest()[:8]


def common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def match_ratio(stored_prefix: str, text: str) -> float:
    """match_ratio = common characters / characters compared.

    The fleet-wide, no-header path (`best_prefix_match`) still uses this: it
    has no family to restrict it and no chain to compare, and its callers'
    contract (a request adopted by similarity, `stats.prefix_matches`) did not
    move in issue #99. The **header** path does not: `key_for_family` counts
    shared blocks (`shared_blocks`) instead, because a ratio over a 4096-char
    slice cannot say where a long prompt diverges.

    The spec (§FA-6) divides by the length of the incoming request. Taken
    literally that makes the ratio collapse for long conversations: a 4096-char
    stored prefix against a 200k-char request can never exceed 0.02 and the
    threshold 0.5 would never be reachable. The denominator is therefore the
    shorter of the two sides, which is what the comparison can actually see.
    """
    if not stored_prefix or not text:
        return 0.0
    denom = min(len(stored_prefix), len(text))
    if denom <= 0:
        return 0.0
    return common_prefix_len(stored_prefix, text) / denom


@dataclass
class ThreadRegistry:
    cfg: AffinityConfig
    threads: dict[str, Thread] = field(default_factory=dict)
    _ephemeral: dict[tuple[str, str], deque[float]] = field(default_factory=dict)
    # Timestamps of the last moves — the hourly budget that stops a thread from
    # paying its prefill over and over (flapping).
    _rebinds: deque[float] = field(default_factory=deque)
    # The block size of the identity chain (issue #99). `BLOCK_CHARS` is the
    # documented default; it is a field so a test can count blocks on a 5000
    # character prompt instead of building a 200 kB one.
    block_chars: int = BLOCK_CHARS

    # -- identity ----------------------------------------------------------
    def key_from_headers(self, headers: Iterable[tuple[str, str]]) -> str | None:
        wanted = self.cfg.header.lower()
        for k, v in headers:
            if k.lower() == wanted and v:
                return f"sid:{v.strip()}"
        return None

    def ttl_of(self, thread: Thread) -> float:
        """The TTL in force for **this** thread, not for the registry.

        A thread opened by a batch key carries its own, shorter TTL; a thread
        without one keeps the global `affinity.ttl_seconds`, so everything that
        existed before behaves exactly as it did. Expiry is therefore decided per
        thread — one global number cannot describe two traffic classes at once.
        """
        return thread.ttl_seconds if thread.ttl_seconds else self.cfg.ttl_seconds

    def expired(self, thread: Thread, now: float | None = None) -> bool:
        """True once the thread has been idle longer than *its* TTL.

        A thread with a request in flight never expires: `last_seen` moves when a
        request starts and when it ends, so a generation that runs longer than the
        TTL would otherwise drop the thread — and its slot — out from under a
        running request. The TTL counts idle time, and a running request is the
        opposite of idle.
        """
        if thread.active > 0:
            return False
        now = now if now is not None else time.time()
        return now - thread.last_seen > self.ttl_of(thread)

    def lookup(self, key: str, model: str) -> Thread | None:
        t = self.threads.get(key)
        if t is None:
            return None
        if t.model != model:
            return None
        if self.expired(t):
            self.remove(t)
            return None
        return t

    def best_prefix_match(self, model: str, text: str) -> tuple[Thread, float] | None:
        best: tuple[Thread, float] | None = None
        for t in list(self.threads.values()):
            if t.model != model or not t.holds_slot:
                continue
            if self.expired(t):
                self.remove(t)
                continue
            r = match_ratio(t.prefix, text)
            if best is None or r > best[1]:
                best = (t, r)
        if best and best[1] >= self.cfg.match_threshold:
            return best
        return None

    def chain_for(self, text: str, model: str) -> BlockChain:
        """The identity chain of one request, at this registry's block size."""
        return block_chain(text, model, self.block_chars)

    def best_family_match(self, header_key: str, model: str,
                          text: str,
                          chain: BlockChain | None = None) -> tuple[Thread, int] | None:
        """`best_prefix_match`, restricted to one session's family (issue #92).

        The family of *header_key* is every thread of *model* whose key is
        exactly `header_key` or `header_key + "#" + <fp8>` — the session's main
        thread and the work units spawned out of it. `best_prefix_match` keeps
        its own fleet-wide semantics (other callers rely on them); this is the
        narrower question the header path has to ask, because a session must
        never be glued onto a *different* session's prefix.

        Since issue #99 the answer is a **block count** (`shared_blocks`), not
        a ratio: the thread with the longest common prefix of chained block
        hashes wins, and the count is returned so the caller can price what is
        still to prefill. Below `MIN_SHARED_BLOCKS` the answer is `None`, not a
        weak match. Expired threads are dropped as they are seen, and a thread
        that holds no slot is not a candidate — it is not bound anywhere.

        Ties keep the first thread in registry order, exactly as the ratio did.
        """
        if not header_key:
            return None
        if chain is None:
            chain = self.chain_for(text, model)
        family_prefix = header_key + UNIT_SEP
        best: tuple[Thread, int] | None = None
        for t in list(self.threads.values()):
            if t.model != model or not t.holds_slot:
                continue
            if t.key != header_key and not t.key.startswith(family_prefix):
                continue
            if self.expired(t):
                self.remove(t)
                continue
            n = shared_blocks(t.chain, text, chain)
            if best is None or n > best[1]:
                best = (t, n)
        if best and best[1] >= MIN_SHARED_BLOCKS:
            return best
        return None

    def key_for_family(self, header_key: str, model: str,
                       text: str) -> tuple[str, Thread | None]:
        """(key, thread) for a request that carries a session header (issue #92).

        The session is no longer the unit of placement. Parallel work — the
        subagents an agent session spawns — inherits the session header but
        shares almost nothing with the main turn (measured: 8 tokens, 0 blocks
        of 8192), so it may live on another node. The key therefore has two
        levels:

        1. **family**: every thread of *model* whose key is `header_key` or
           `header_key + "#" + <fp8>`; the longest shared prefix of chained
           block hashes over that family decides (issue #99).
        2. at or above `MIN_SHARED_BLOCKS` (one shared block, ~32k characters
           ≈ 8192 tokens of prefill) → that thread's key and the thread
           itself. The main conversation's follow-up turn lands here — its
           text is the last turn's text plus a message, so every whole block
           plus the boundary block is shared — and so does a spawned unit's own
           follow-up, because its child thread stored *its* opening.
        3. no match, and no thread under `header_key` at all → `header_key`
           with `None`: the first request of the session births the main thread
           under the plain header key.
        4. no match, but a thread under `header_key` exists → a spawn:
           `header_key + "#" + fp8`, where fp8 is the first 8 hex of
           `sha256(text[:cfg.prefix_chars])`, together with whatever thread
           already carries that child key (`lookup`, so `None` until it is
           registered). Four spawns of four different briefs are four threads
           and can be placed on four different nodes.

        Nothing here mutates the registry and nothing counts: the same text
        always yields the same fp8, so a retry or a duplicate request rejoins
        its own child thread instead of opening another one.

        Why a block count and not a ratio (issue #99): a ratio is one number
        for the whole prefix and it is computed over the first
        `cfg.prefix_chars` characters, so it cannot see where a long prompt
        diverges — 60 % of a 200k-character prompt and 100 % of it look the
        same to a 4096-character slice. A block count says *where*, and a count
        can be subtracted from a cost. The child that shares 60 % of a long
        prefix now has three shared blocks and is visibly the same thread,
        instead of landing on whichever side of `match_threshold` its slice
        happened to fall.

        Honest limits, all deliberate:

        - **Identical briefs collapse.** Two spawns whose first
          `cfg.prefix_chars` characters are equal produce the same fp8 and share
          one child thread. That is what a content fingerprint means; telling
          them apart would need a per-request id from the client, which is
          exactly the client change this design avoids.
        - **Character blocks do not align with token blocks.** The count is
          exact in blocks and ±1 block at the boundary, where a partial block
          is compared by content (`shared_blocks`) rather than by hash. A text
          shorter than one block has no whole block at all, so two *different*
          short prompts that share their opening share 0 blocks even if they
          share most characters — the chain does not do fuzzy matching, on
          purpose.
        - **The system prompt is not in the chain** (`prompt_text` excludes
          it), so this answers "same conversation", not "same request".
        - **Ordering.** A spawn that arrives *before* the session's first
          request finds an empty family and gets the plain key (rule 3) — the
          first arrival names the session thread, whoever it is. The next
          request that does not match it then opens a child of its own. In the
          normal order (main turn first, spawns after) this cannot happen; a
          spawn that arrives first costs one thread that is not the main one,
          never a stolen binding, because rule 2 always wins over rule 3.
        """
        chain = self.chain_for(text, model)
        found = self.best_family_match(header_key, model, text, chain)
        if found is not None:
            return found[0].key, found[0]
        if self.lookup(header_key, model) is None:
            return header_key, None
        child = f"{header_key}{UNIT_SEP}{unit_fingerprint(text, self.cfg.prefix_chars)}"
        return child, self.lookup(child, model)

    # -- mutation ----------------------------------------------------------
    def register(self, *, key: str, model: str, host: str, text: str,
                 holds_slot: bool, source: str, kind: str = "interactive",
                 ttl_seconds: float | None = None,
                 key_name: str | None = None,
                 prompt_tokens: int = 0) -> Thread:
        now = time.time()
        t = Thread(key=key, model=model, host=host,
                   prefix=text[: self.cfg.prefix_chars],
                   chain=self.chain_for(text, model),
                   source=source, created=now, last_seen=now, holds_slot=holds_slot,
                   kind=kind, ttl_seconds=ttl_seconds, key_name=key_name,
                   last_prompt_tokens=max(0, int(prompt_tokens)))
        if holds_slot:
            self.threads[key] = t
        return t

    def touch(self, thread: Thread, prompt_tokens: int | None = None) -> None:
        """A request of this thread is admitted: refresh it.

        `prompt_tokens` is the size of *this* request, so it **replaces** the
        previous value instead of adding to it — the price of a move is one
        prefill of the next prompt, and the next prompt is the last one seen.
        `None` (the caller does not know) leaves the stored size alone rather
        than resetting a known price to "unknown".
        """
        thread.last_seen = time.time()
        thread.turns += 1
        if prompt_tokens is not None:
            thread.last_prompt_tokens = max(0, int(prompt_tokens))

    def promote(self, thread: Thread) -> bool:
        """Raise a batch thread to interactive. True only for the real change.

        A person is now waiting on this conversation, so the 120 s batch TTL
        would pull the prefix cache out from under it: the thread drops its own
        TTL (it ages by `affinity.ttl_seconds` again) and takes part in eviction
        in the normal order. One-way on purpose — a conversation does not become
        less urgent because a batch job happens to reuse its prefix — and the
        return value is the "exactly once" guard: a thread already promoted is
        not counted or logged again.
        """
        if thread.kind != "batch":
            return False
        thread.kind = "interactive"
        thread.ttl_seconds = None
        return True

    def rebind(self, thread: Thread, host: str) -> None:
        thread.host = host
        thread.last_seen = time.time()

    def note_rebind(self, thread: Thread, host: str, now: float | None = None) -> None:
        """A deliberate move (not a fallback): count it against the budget."""
        now = now if now is not None else time.time()
        thread.host = host
        thread.last_seen = now
        thread.last_rebind = now
        self._rebinds.append(now)

    def rebinds_last_hour(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        while self._rebinds and now - self._rebinds[0] > 3600:
            self._rebinds.popleft()
        return len(self._rebinds)

    def idle_threads(self, host: str, now: float | None = None, *,
                     cooldown_seconds: float = 0.0) -> list[Thread]:
        """The threads on *host* a move may take, cheapest price first (#80).

        A candidate has to be movable *and* cheap, in that order:

        - `active == 0` — a thread with a request in flight is work, and moving
          it throws that work away;
        - not `batch` — a batch thread is throughput without anyone waiting for
          its prefix cache, and rebind is off for it everywhere else too;
        - not expired — a dead thread has no slot left to move;
        - `last_rebind` outside the cooldown — a thread that just paid its
          prefill must not pay it again (flapping).

        Sorted ascending by the price of the move. At a fixed `pp_tps` that
        price is `last_prompt_tokens / pp_tps`, so the prompt size alone orders
        them; `turns` and `created` are the stable tie-break, so the list does
        not wobble between two scans that see the same fleet. No division by
        `pp_tps` happens here — the price in seconds belongs to `placement.py`,
        which knows the rates; this method only says which threads are cheap.

        A thread whose size is unknown (0) sorts **last**, not first: a price of
        0 would look free, and the decision layer refuses to price it at all.
        It stays in the list so the caller sees the thread and can say why it
        did not move, rather than wondering where it went.

        `cooldown_seconds` is passed in because the registry only holds
        `AffinityConfig`; the number lives in `placement.rebind`.
        """
        now = now if now is not None else time.time()
        out: list[Thread] = []
        for t in self._live(now):
            if t.host != host or not t.holds_slot:
                continue
            if t.active > 0 or t.kind == "batch":
                continue
            if t.last_rebind and now - t.last_rebind < cooldown_seconds:
                continue
            out.append(t)
        return sorted(out, key=lambda t: (t.last_prompt_tokens <= 0,
                                          t.last_prompt_tokens, t.turns, t.created))

    def enter(self, thread: Thread) -> None:
        """A request starts on this thread."""
        thread.active += 1
        thread.last_seen = time.time()

    def leave(self, thread: Thread) -> None:
        """That request is done with the upstream (FA-19 slot stays)."""
        thread.active = max(0, thread.active - 1)
        thread.last_seen = time.time()

    def evictable(self, host: str, model: str | None = None, *,
                  prefer_kind: str | None = None) -> Thread | None:
        """The thread that may give up its slot: the one idle the longest.

        A thread with a request in flight is never a candidate — pulling the slot
        from under a running request is exactly what the reservation exists to
        prevent. A conversation that finished minutes ago is the opposite: nobody
        is waiting for it, and oMLX's prefix cache for it is most likely gone
        already, so the only cost of dropping it is a re-prefill if it ever
        returns.

        `prefer_kind` orders by class before age: an interactive request (a
        person waiting, FA-18) takes a finished **batch** thread's slot first,
        even when an interactive thread has been idle longer. Batch traffic has
        no one waiting for its cache and will not come back to it; a person's
        conversation may. Without a candidate of that class the rule falls back
        to pure age, so a fleet of interactive threads behaves exactly as before.

        Without this, a host that has seen `max_threads` conversations within the
        TTL refuses every new one with 503 `no_slot` while sitting completely
        idle (measured: 24 finished threads, `inflight 0`, both hosts 12/12).
        """
        now = time.time()
        cands = [t for t in self._live(now)
                 if t.host == host and t.holds_slot and t.active <= 0
                 and (model is None or t.model == model)]
        if prefer_kind is not None:
            preferred = [t for t in cands if t.kind == prefer_kind]
            if preferred:
                cands = preferred
        return min(cands, key=lambda t: t.last_seen) if cands else None

    def remove(self, thread: Thread) -> None:
        self.threads.pop(thread.key, None)

    def forget_host(self, host: str) -> int:
        """Drop every thread bound to a node that left the fleet (issue #70).

        A bound thread *is* a slot on its host, and the slot counts are the
        fleet's — a removed node that kept them would reserve capacity for a
        machine that no longer exists, and the share the dashboard shows would
        be wrong for ever. The ephemeral window of that host goes with it: it
        answers "how many one-shot requests hit this (node, model) in the last
        minute", which is a statement about a node that is no longer there.

        A thread with a request still in flight is left alone. The drain already
        waited for it; pulling it out from under a request is exactly what the
        reservation exists to prevent.
        """
        gone = [t for t in self.threads.values() if t.host == host and t.active <= 0]
        for thread in gone:
            self.threads.pop(thread.key, None)
        for key in [k for k in self._ephemeral if k[0] == host]:
            self._ephemeral.pop(key, None)
        return len(gone)

    def purge(self) -> int:
        dead = [t for t in self.threads.values() if self.expired(t)]
        for t in dead:
            self.threads.pop(t.key, None)
        return len(dead)

    # -- ephemeral requests (FA-7) -----------------------------------------
    def note_ephemeral(self, host: str, model: str) -> None:
        dq = self._ephemeral.setdefault((host, model), deque())
        dq.append(time.time())

    def _prune_ephemeral(self, now: float | None = None) -> None:
        now = now or time.time()
        window = self.cfg.ephemeral_window_seconds
        for key, dq in list(self._ephemeral.items()):
            while dq and now - dq[0] > window:
                dq.popleft()
            if not dq:
                self._ephemeral.pop(key, None)

    def ephemeral_count(self, host: str, model: str) -> int:
        self._prune_ephemeral()
        return len(self._ephemeral.get((host, model), ()))

    # -- counting ----------------------------------------------------------
    # Counting must ignore expired threads: the purge loop runs every 30 s, and
    # in between a thread whose TTL has passed must not occupy a slot any more
    # (AS-4b) — otherwise the deficit rule keeps routing around an idle thread.
    def _live(self, now: float | None = None):
        now = now or time.time()
        for t in self.threads.values():
            if self.expired(t, now):
                continue
            yield t

    def slots(self, host: str, model: str) -> int:
        return sum(1 for t in self._live()
                   if t.host == host and t.model == model and t.holds_slot)

    def slots_on_host(self, host: str) -> int:
        return sum(1 for t in self._live() if t.host == host and t.holds_slot)

    def weighted_slots(self, host: str, model: str, decay_seconds: float,
                       now: float | None = None) -> float:
        """Slot count for **binding** only (FA-2), not for the hard slot limit.

        A thread counts 1.0 while a request of it is in flight and fades
        **quadratically** to 0 over `decay_seconds` of idleness:
        `(1 − idle/decay)²`. Reason (measured 14.11.2026): the entry host
        permanently carries the operator's own session threads. Counted at full
        weight for the whole 30 min TTL they pushed every new conversation onto
        the weaker node although nothing was running anywhere.

        Quadratic on purpose: a thread idle for a tenth of the window still
        weighs 0.81, half the window 0.25 — the first minutes after an answer
        keep their pull, the tail does not. A thread with a request in flight
        always weighs 1.0, so work actually running on a "silent" thread is
        never invisible. The binding itself is untouched; the thread stays
        assigned and counts fully again on its next request.
        `slots_on_host` stays raw — the hard slot limit is real.
        """
        now = now if now is not None else time.time()
        total = 0.0
        for t in self._live(now):
            if t.host != host or t.model != model or not t.holds_slot:
                continue
            if t.active > 0 or decay_seconds <= 0:
                total += 1.0
                continue
            idle = now - t.last_seen
            total += 1.0 if idle <= 0 else max(0.0, 1.0 - idle / decay_seconds) ** 2
        return total

    def threads_for_model(self, model: str) -> list[Thread]:
        return [t for t in self._live() if t.model == model and t.holds_slot]

    def active_total(self) -> int:
        return sum(1 for t in self._live() if t.holds_slot)

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {"key": t.label, "model": t.model, "host": t.host, "source": t.source,
             "kind": t.kind, "key_name": t.key_name, "turns": t.turns,
             "ttl_seconds": self.ttl_of(t),
             # `last_seen` bewegt sich beim Start und am Ende einer Anfrage, nicht
             # waehrend sie laeuft. Eine laufende Anfrage ist nicht Leerlauf, also
             # steht hier 0 — sonst zaehlt die Seite einer arbeitenden Bindung
             # Minuten Stillstand an und die Restzeit schrumpft mit ihr. `active`
             # selbst steht nur im /healthz-Payload (`_thread_rows`), damit
             # `/stats → threads` seine Form behaelt (issue #35).
             "idle_seconds": 0.0 if t.active else round(now - t.last_seen, 1)}
            for t in sorted(self.threads.values(), key=lambda x: x.last_seen, reverse=True)
        ]
