"""Thread identity and affinity: AS-3, FA-6 (fingerprint, prefix similarity, header)."""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from urllib.parse import quote

from ripple.config import AffinityConfig
from ripple.registry import (BLOCK_CHARS, CHARS_PER_TOKEN, LABEL_CHARS, MIN_SHARED_BLOCKS,
                             Thread, ThreadRegistry, block_chain, fingerprint,
                             match_ratio, prefill_estimate, prompt_text, shared_blocks)

from tests.support import (bearer, chat_body, host_config, keyed_config, make_config,
                           prompt_for, stub_model_loaded)

LONG_OPENING = ("Placement is decided per thread, not per request. " * 120)[:5000]


def _two_hosts(stub_factory, *, weight_a: float = 3.0, weight_b: float = 2.0):
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    return [
        host_config("A", a.url, node_id=a.state.node_id, weight=weight_a),
        host_config("B", b.url, node_id=b.state.node_id, weight=weight_b),
    ]


async def test_as3_five_turns_of_one_conversation_stay_on_one_host(
        stub_factory, ripple_factory):
    """The binding wins even though the other host is completely empty."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))
    prompt = prompt_for(0)

    first = await harness.ok(chat_body("m1", prompt, turns=2))
    bound = first.headers["x-ripple-host"]
    assert bound == "A"  # the deficit rule picks A for the very first thread

    seen = []
    for turn in range(3, 7):  # four more turns of the same conversation
        resp = await harness.ok(chat_body("m1", prompt, turns=turn))
        assert resp.headers["x-ripple-reason"] == "thread"
        assert resp.headers["x-ripple-host"] == bound
        seen.append(resp.headers["x-ripple-host"])

    assert Counter(seen) == {bound: 4}
    # B never saw a single request
    other = b if bound == "A" else a
    assert other.state.chat_calls == 0
    # and the conversation occupies exactly one slot
    assert await harness.slots() == {"A": 1, "B": 0}


async def test_fa6_compressed_conversation_keeps_its_binding(stub_factory,
                                                            ripple_factory):
    """A compression rewrites the body: the beginning still matches, so the
    prefix similarity keeps the thread on its host."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    first = await harness.ok(chat_body("m1", LONG_OPENING, turns=2))
    bound = first.headers["x-ripple-host"]
    assert bound == "A"

    # Same opening (4000 of the stored 4096 prefix characters), rewritten tail.
    compressed = LONG_OPENING[:4000] + " [compressed] the rest of the history was " \
                                       "summarised by the client"
    second = await harness.ok(chat_body("m1", compressed, turns=1))

    assert second.headers["x-ripple-host"] == bound
    assert second.headers["x-ripple-reason"] == "thread"
    assert second.headers["x-ripple-thread"] == first.headers["x-ripple-thread"]
    assert (await harness.stats())["prefix_matches"] >= 1
    # no second thread was opened for the compressed body
    assert (await harness.healthz())["threads"]["active"] == 1
    assert b.state.chat_calls == 0


async def test_fa7_short_single_message_holds_no_slot_but_a_long_one_does(
        stub_factory, ripple_factory):
    """FA-7: one short user message is ephemeral, a long one opens a thread."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))

    short = await harness.ok(chat_body("m1", prompt_for(0), turns=1))
    assert short.headers["x-ripple-reason"] == "ephemeral"
    assert await harness.slots() == {"A": 0, "B": 0}

    # 33600 characters ≈ 8400 estimated tokens, above min_prompt_tokens (8192)
    long_prompt = "context " * 4200
    long_turn = await harness.ok(chat_body("m1", long_prompt, turns=1))
    assert long_turn.headers["x-ripple-reason"] == "new_thread"
    assert sum((await harness.slots()).values()) == 1
    assert len((await harness.stats())["threads"]) == 1


async def test_fa6_unrelated_prompt_opens_a_new_thread(stub_factory,
                                                      ripple_factory):
    """The similarity match must not glue unrelated conversations together."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))

    first = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    other = await harness.ok(chat_body("m1", prompt_for(1), turns=2))

    assert other.headers["x-ripple-reason"] == "new_thread"
    assert other.headers["x-ripple-thread"] != first.headers["x-ripple-thread"]
    assert (await harness.healthz())["threads"]["active"] == 2


async def test_fa6_session_header_beats_the_fingerprint(stub_factory,
                                                       ripple_factory):
    """An explicit X-Session-Id wins over model + first user message."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))
    prompt = prompt_for(0)

    fingerprint_thread = await harness.ok(chat_body("m1", prompt, turns=2))
    assert fingerprint_thread.headers["x-ripple-reason"] == "new_thread"

    # same first user message, but an explicit thread id: this is a new thread
    header_thread = await harness.ok(chat_body("m1", prompt, turns=2),
                                     headers={"X-Session-Id": "sess-42"})
    assert header_thread.headers["x-ripple-thread"] == "sid:sess-42"
    assert header_thread.headers["x-ripple-reason"] == "new_thread"
    assert header_thread.headers["x-ripple-thread"] != \
        fingerprint_thread.headers["x-ripple-thread"]

    # a later turn with the same header stays on the header's thread
    again = await harness.ok(chat_body("m1", prompt, turns=3),
                             headers={"X-Session-Id": "sess-42"})
    assert again.headers["x-ripple-reason"] == "thread"
    assert again.headers["x-ripple-thread"] == "sid:sess-42"
    assert again.headers["x-ripple-host"] == header_thread.headers["x-ripple-host"]

    health = await harness.healthz()
    assert health["threads"]["active"] == 2
    sources = {row["key"]: row["source"] for row in health["threads"]["registry"]}
    assert sources["sid:sess-42"] == "header"


# ---------------------------------------------------------------------------
# Attribution: which client key holds a thread (issue #32)
# ---------------------------------------------------------------------------
def _registry() -> ThreadRegistry:
    return ThreadRegistry(AffinityConfig())


def test_a_running_request_never_expires_and_is_not_idle() -> None:
    """A generation longer than the TTL must not drop the thread out from under
    itself. `last_seen` moves when a request starts and when it ends, so a
    30-minute answer would otherwise look expired at minute 30 — measured on the
    live ledger, where threads showed 300 s idle while their agent was streaming.
    """
    reg = _registry()
    thread = reg.register(key="sid:long", model="m1", host="A", text="hello",
                          holds_slot=True, source="header")
    reg.enter(thread)
    later = time.time() + 5 * 3600            # far past any TTL

    assert reg.expired(thread, now=later) is False
    assert reg.snapshot()[0]["idle_seconds"] == 0.0   # not idle while it works

    reg.leave(thread)                          # the answer is out, the TTL restarts
    assert reg.expired(thread, now=later) is True   # idle counts from here again


def test_the_snapshot_names_the_key_that_opened_the_thread() -> None:
    """`/healthz → threads.registry[].key_name` answers "who holds the slot"."""
    reg = _registry()
    reg.register(key="sid:abc", model="m1", host="A", text="hello", holds_slot=True,
                 source="header", key_name="mustafa")

    assert reg.snapshot()[0]["key_name"] == "mustafa"


def test_a_thread_opened_without_a_key_carries_no_name() -> None:
    """`None` is the fact "this request had no key" (loopback), not "unknown"."""
    reg = _registry()
    reg.register(key="4b17eecc4cf0a8a5", model="m1", host="A", text="hello",
                 holds_slot=True, source="fingerprint")

    assert reg.snapshot()[0]["key_name"] is None


def test_touch_rebind_and_promote_do_not_change_the_name() -> None:
    """Only the opener names a thread: activity and a move are not a new owner."""
    reg = _registry()
    thread = reg.register(key="sid:job", model="m1", host="A", text="hello",
                          holds_slot=True, source="header", kind="batch",
                          ttl_seconds=120, key_name="nightly")

    reg.touch(thread)
    reg.rebind(thread, "B")
    assert reg.promote(thread) is True

    assert thread.key_name == "nightly"
    assert reg.snapshot()[0]["key_name"] == "nightly"


def test_a_thread_adopted_by_prefix_match_keeps_the_openers_name() -> None:
    """The credit belongs to the conversation, not to the request that found it."""
    reg = _registry()
    opener = reg.register(key="sid:job", model="m1", host="A", text=LONG_OPENING,
                          holds_slot=True, source="header", kind="batch",
                          ttl_seconds=120, key_name="nightly")

    found = reg.best_prefix_match("m1", LONG_OPENING[:4000] + "\nplease summarise")
    assert found is not None and found[0] is opener

    reg.touch(opener)
    reg.promote(opener)
    assert opener.key_name == "nightly"
    assert reg.snapshot()[0]["key_name"] == "nightly"


async def test_healthz_names_the_key_that_holds_the_thread(stub_factory,
                                                         ripple_factory, tmp_path):
    """The wire, not only the registry: the dispatch passes the key's name on,
    and an adoption by another key does not take it away."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml"),
        affinity=AffinityConfig(ttl_seconds=1800, batch_ttl_seconds=120)))
    job = harness.ripple.keys.create("nightly", "batch")
    human = harness.ripple.keys.create("hermes", "interactive")

    await harness.ok(chat_body("m1", LONG_OPENING, turns=2), headers=bearer(job.key))
    row = (await harness.healthz())["threads"]["registry"][0]
    assert row["key_name"] == "nightly"

    # a person picks the conversation up by prefix match; the opener keeps it
    await harness.ok(chat_body("m1", LONG_OPENING[:4000] + "\nplease summarise",
                               turns=2),
                     headers=bearer(human.key))
    row = (await harness.healthz())["threads"]["registry"][0]
    assert row["kind"] == "interactive"
    assert row["key_name"] == "nightly"


# ---------------------------------------------------------------------------
# Work-unit granularity: a spawn gets its own key (issue #92, phase 1 part 1)
# ---------------------------------------------------------------------------
# A thread stores the chained block hashes of the prompt that opened it (issue
# #99). A follow-up turn is the same text plus a message, so it shares every
# whole block; a spawned subagent brief shares none.
MAIN_KEY = "sid:gwk_a69118d7a3b01a0f"
SPAWN_ONE = "You are a subagent. Read ripple/registry.py and report the slot logic."
SPAWN_TWO = "You are a subagent. Count the threads in tests/test_affinity.py."
SPAWN_THREE = "You are a subagent. Check docs/requirements.md section 6.3."
SPAWN_FOUR = "You are a subagent. Run the dashboard snapshot by hand."


def _main_thread(reg: ThreadRegistry, host: str = "A") -> Thread:
    return reg.register(key=MAIN_KEY, model="m1", host=host, text=LONG_OPENING,
                        holds_slot=True, source="header")


def test_92_the_first_request_births_the_plain_key_and_the_follow_up_stays() -> None:
    """Rules 3 and 2: the session thread is born under the header key, and a
    grown prompt (every block plus the boundary block) keeps it."""
    reg = _registry()

    key, thread = reg.key_for_family(MAIN_KEY, "m1", LONG_OPENING)
    assert key == MAIN_KEY
    assert thread is None                     # nothing to join yet, and no mutation

    main = _main_thread(reg)

    follow_up = LONG_OPENING + "\nand one more turn of the same conversation"
    assert main.chain is not None
    assert shared_blocks(main.chain, follow_up) >= MIN_SHARED_BLOCKS
    key, thread = reg.key_for_family(MAIN_KEY, "m1", follow_up)
    assert key == MAIN_KEY
    assert thread is main


def test_92_a_spawn_with_a_diverging_prefix_gets_its_own_key() -> None:
    """Rule 4: the header alone is not enough — the prefix has to match too.

    Measured on the M5: a spawned request shares 8 tokens with the main turn
    (ratio ≈ 0.0001), so it may be placed on another node.
    """
    reg = _registry()
    main = _main_thread(reg)

    assert match_ratio(main.prefix, SPAWN_ONE) < 0.01
    key, thread = reg.key_for_family(MAIN_KEY, "m1", SPAWN_ONE)

    assert key == MAIN_KEY + "#" + hashlib.sha256(
        SPAWN_ONE.encode("utf-8")).hexdigest()[:8]
    assert key != MAIN_KEY
    assert thread is None                     # the child thread is not born yet

    child = reg.register(key=key, model="m1", host="B", text=SPAWN_ONE,
                         holds_slot=True, source="header")

    # its own follow-up turn matches the child thread (rule 2) and stays on it
    follow_up = SPAWN_ONE + "\nAlso check the eviction order."
    key2, thread2 = reg.key_for_family(MAIN_KEY, "m1", follow_up)
    assert key2 == key
    assert thread2 is child

    # and the main thread keeps its binding, even with the child in the family
    key3, thread3 = reg.key_for_family(MAIN_KEY, "m1",
                                       LONG_OPENING + "\nturn four")
    assert key3 == MAIN_KEY
    assert thread3 is main


def test_92_different_briefs_yield_different_child_keys() -> None:
    """Four spawns of one session are four threads — that is the whole point."""
    reg = _registry()
    _main_thread(reg)

    keys = [reg.key_for_family(MAIN_KEY, "m1", brief)[0]
            for brief in (SPAWN_ONE, SPAWN_TWO, SPAWN_THREE, SPAWN_FOUR)]

    assert len(set(keys)) == 4
    assert MAIN_KEY not in keys
    assert all(k.startswith(MAIN_KEY + "#") for k in keys)
    assert all(len(k) == len(MAIN_KEY) + 9 for k in keys)

    # deterministic: the same brief always names the same child
    assert reg.key_for_family(MAIN_KEY, "m1", SPAWN_ONE)[0] == keys[0]
    # and a retried request rejoins the child instead of opening a new one
    for key, brief in zip(keys, (SPAWN_ONE, SPAWN_TWO, SPAWN_THREE, SPAWN_FOUR)):
        reg.register(key=key, model="m1", host="B", text=brief,
                     holds_slot=True, source="header")
    again = reg.key_for_family(MAIN_KEY, "m1", SPAWN_TWO + "\nand one more")
    assert again[0] == keys[1]
    assert again[1] is not None


def test_92_identical_briefs_collapse_onto_one_child_key() -> None:
    """The honest limit of a fingerprint, not a bug: same opening, same unit."""
    reg = _registry()
    _main_thread(reg)

    first = reg.key_for_family(MAIN_KEY, "m1", SPAWN_ONE)[0]
    second = reg.key_for_family(MAIN_KEY, "m1", SPAWN_ONE)[0]

    assert first == second


def test_99_the_split_is_one_shared_block_not_a_ratio() -> None:
    """Rules 2 and 4 on either side of `MIN_SHARED_BLOCKS`, computed exactly.

    This replaces `test_92_the_threshold_is_where_the_two_branches_split`,
    which asserted the ratio split this issue removes: the header path no
    longer compares `match_ratio` with `match_threshold`, it counts shared
    blocks of a chained hash.

    The two texts sit on either side of one block — and the first one is
    deliberately on the *wrong* side of `match_threshold`, because that is what
    the change buys: 1000 shared characters are 0.24 of the stored 4096-char
    prefix (the ratio would call it a spawn) and one whole block of prefill
    (the chain calls it the same thread).
    """
    reg = ThreadRegistry(AffinityConfig(), block_chars=1000)
    main = _main_thread(reg)                      # 5000 characters = 5 blocks
    assert main.chain is not None
    assert len(main.chain.blocks) == 5
    assert MIN_SHARED_BLOCKS == 1

    one_block = LONG_OPENING[:1000] + "Z" + " tail that differs from here on." * 60
    assert shared_blocks(main.chain, one_block) == 1
    assert match_ratio(main.prefix, one_block) < reg.cfg.match_threshold   # premise
    key, thread = reg.key_for_family(MAIN_KEY, "m1", one_block)
    assert key == MAIN_KEY
    assert thread is main

    no_block = LONG_OPENING[:999] + "Z" + " tail that differs from here on." * 60
    assert shared_blocks(main.chain, no_block) == 0
    key2, thread2 = reg.key_for_family(MAIN_KEY, "m1", no_block)
    assert key2 == MAIN_KEY + "#" + hashlib.sha256(
        no_block[:reg.cfg.prefix_chars].encode("utf-8")).hexdigest()[:8]
    assert key2 != MAIN_KEY
    assert thread2 is None


# ---------------------------------------------------------------------------
# Identity as a chained block hash (issue #99)
# ---------------------------------------------------------------------------
def test_99_a_follow_up_sharing_ninety_percent_of_its_prefix_matches() -> None:
    """The acceptance case: 90 % of the stored prefix, one thread.

    4500 of 5000 characters are identical, so four of the five 1000-character
    blocks hash the same and the fifth differs at its 500th character — four
    blocks, not a 0.9.
    """
    reg = ThreadRegistry(AffinityConfig(), block_chars=1000)
    main = _main_thread(reg)
    follow_up = LONG_OPENING[:4500] + "Z" + " and then the conversation went on." * 20

    assert shared_blocks(main.chain, follow_up) == 4
    key, thread = reg.key_for_family(MAIN_KEY, "m1", follow_up)
    assert key == MAIN_KEY
    assert thread is main


def test_99_a_spawn_sharing_no_block_does_not_match() -> None:
    """The measured spawn: 8 tokens in common = 0 blocks of 8192.

    At the default 32 768-character block a 5000-character prompt has no whole
    block at all, and the boundary does not save it either: the spawn diverges
    at its first character, so it is not a prefix of the stored text.
    """
    reg = _registry()
    main = _main_thread(reg)

    assert shared_blocks(main.chain, SPAWN_ONE) == 0
    key, thread = reg.key_for_family(MAIN_KEY, "m1", SPAWN_ONE)
    assert key == MAIN_KEY + "#" + hashlib.sha256(
        SPAWN_ONE.encode("utf-8")).hexdigest()[:8]
    assert thread is None


def test_99_a_child_sharing_sixty_percent_of_a_long_prefix_matches() -> None:
    """The case the change exists for: 60 % of a 20 000-character prefix.

    12 of 20 blocks are identical, so the child is visibly the thread it was
    born from — 12 is a number the operator can read, and it is far above
    `MIN_SHARED_BLOCKS`. The ratio over the stored 4096-character slice is 1.0
    for this text and for a 100 %-shared one alike: it cannot tell them apart,
    which is why it is no longer asked.
    """
    reg = ThreadRegistry(AffinityConfig(), block_chars=1000)
    stored = LONG_OPENING * 4                     # 20 000 characters
    main = reg.register(key=MAIN_KEY, model="m1", host="A", text=stored,
                        holds_slot=True, source="header")
    child = stored[:12_000] + "Z" + " the subagent's own reading of it." * 40

    assert main.chain is not None
    assert len(main.chain.blocks) == 20
    assert shared_blocks(main.chain, child) == 12
    # the ratio this replaces is blind to the difference: it only sees 4096 chars
    assert match_ratio(main.prefix, child) == 1.0

    key, thread = reg.key_for_family(MAIN_KEY, "m1", child)
    assert key == MAIN_KEY
    assert thread is main


def test_99_shared_blocks_is_the_hand_computed_number() -> None:
    """The count, block by block, against an expectation written out by hand.

    Four 10-character blocks `A B C D`; the stored text is `A B C` plus a
    5-character tail, so the cases below cover: a divergence inside a block, a
    divergence at a block boundary, a grown prompt (the boundary block), and a
    text that ends exactly on a block boundary.
    """
    a = "A" * 10 + "B" * 10 + "C" * 10 + "D" * 10          # 4 blocks
    stored_text = a[:35]                                    # 3 blocks + "DDDDD"
    stored = block_chain(stored_text, "m1", 10)

    assert len(stored.blocks) == 3
    assert stored.tail == "DDDDD"
    assert stored.full_chars == 30 and stored.chars == 35

    # every block equal, the stored text is a prefix: 3 blocks + the boundary
    assert shared_blocks(stored, stored_text + "and one more turn") == 4
    # identical text is identical text
    assert shared_blocks(stored, stored_text) == 4
    # the tail differs at its first character: 3 blocks, no boundary
    assert shared_blocks(stored, a[:30] + "XDDDD and more") == 3
    # divergence inside block 2: two blocks
    assert shared_blocks(stored, a[:25] + "ZZZZZZZZZZ") == 2
    # divergence inside block 0: none
    assert shared_blocks(stored, "Z" + a[1:]) == 0
    # a stored text that ends on a block boundary has no partial block to count
    exact = block_chain(a, "m1", 10)
    assert shared_blocks(exact, a + "E" * 10) == 4
    # a shorter request is compared over its own length, never over the tail
    assert shared_blocks(stored, a[:12]) == 1
    # nothing stored, nothing shared
    assert shared_blocks(None, a) == 0


def test_99_two_models_never_share_a_chain() -> None:
    """The seed carries the model: identical text, different KV blocks."""
    text = LONG_OPENING * 4
    m1 = block_chain(text, "m1", 1000)
    m2 = block_chain(text, "m2", 1000)

    assert m1.blocks and m2.blocks
    assert m1.blocks[0] != m2.blocks[0]
    assert shared_blocks(m1, text, m2) == 0


def test_99_a_thread_without_a_chain_is_not_a_candidate() -> None:
    """The clean cut, stated: the registry is in memory and dies with the
    process, so no thread survives a restart without a chain.

    A `Thread` built without one (an object from before this change) shares 0
    blocks and is therefore never adopted — it is not silently matched by the
    old ratio, because the old ratio is not consulted in the header path any
    more. The next request under that header opens a child of its own.
    """
    reg = _registry()
    legacy = Thread(key=MAIN_KEY, model="m1", host="A", prefix=LONG_OPENING,
                    source="header", created=time.time(), last_seen=time.time())
    reg.threads[MAIN_KEY] = legacy

    assert shared_blocks(legacy.chain, LONG_OPENING + "\nturn two") == 0
    key, thread = reg.key_for_family(MAIN_KEY, "m1", LONG_OPENING + "\nturn two")
    assert key != MAIN_KEY
    assert thread is None


def test_99_the_cost_estimate_comes_out_of_the_block_count() -> None:
    """`shared_chars = blocks × block_chars`, `/4` tokens, the rest is prefill."""
    estimate = prefill_estimate(25_000, 2)
    assert estimate.shared_blocks == 2
    assert estimate.shared_chars == 2 * BLOCK_CHARS
    assert estimate.shared_tokens == 2 * BLOCK_CHARS // CHARS_PER_TOKEN
    assert estimate.prefill_tokens == 25_000 - estimate.shared_tokens

    # a counted boundary block never claims more characters than exist
    capped = prefill_estimate(25_000, 1, cap_chars=5_000)
    assert capped.shared_chars == 5_000
    assert capped.shared_tokens == 5_000 // CHARS_PER_TOKEN
    assert capped.prefill_tokens == 25_000 - 1_250

    # nothing shared is the whole prompt, and never a negative price
    assert prefill_estimate(25_000, 0).prefill_tokens == 25_000
    assert prefill_estimate(100, 2).prefill_tokens == 0


def test_99_hashing_a_100k_character_prompt_is_negligible() -> None:
    """A 100 000-character prompt is three blocks of sha256 — measured, not
    assumed. The bound is loose on purpose (a loaded machine must not make the
    suite flaky); the measured median is three orders of magnitude below it."""
    text = ("The same conversation, one more turn. " * 3000)[:100_000]
    assert len(text) == 100_000
    assert len(block_chain(text, "m1").blocks) == 100_000 // BLOCK_CHARS == 3

    timings = []
    for _ in range(20):
        started = time.perf_counter()
        block_chain(text, "m1")
        timings.append(time.perf_counter() - started)

    timings.sort()
    median = timings[len(timings) // 2]
    assert median < 0.02, f"100k characters took {median * 1000:.1f} ms"


def test_92_the_family_does_not_reach_across_sessions() -> None:
    """A session's header is matched against its own family only — and
    `best_prefix_match` keeps its fleet-wide meaning for everyone else."""
    reg = _registry()
    _main_thread(reg)
    other = reg.register(key="sid:another-session", model="m1", host="B",
                         text=LONG_OPENING, holds_slot=True, source="header")

    # the identical prefix lives in another family: not a candidate here
    key, thread = reg.key_for_family(MAIN_KEY, "m1", LONG_OPENING + "\nturn two")
    assert thread is not other
    assert key == MAIN_KEY

    # the fleet-wide helper still sees both, unchanged
    found = reg.best_prefix_match("m1", LONG_OPENING)
    assert found is not None and found[0].key in {MAIN_KEY, other.key}


def test_92_a_spawn_before_the_first_request_cannot_steal_the_plain_key() -> None:
    """The documented ordering caveat: rule 2 beats rule 3, but an empty family
    hands the plain key to whoever arrives first."""
    reg = _registry()

    # empty family: the spawn *is* the first arrival and names the thread
    key, thread = reg.key_for_family(MAIN_KEY, "m1", SPAWN_ONE)
    assert key == MAIN_KEY and thread is None

    # once a child exists, a request that matches it stays on the child, and a
    # non-matching one opens a child of its own rather than taking the family key
    reg = _registry()
    child_key = MAIN_KEY + "#" + hashlib.sha256(
        SPAWN_ONE.encode("utf-8")).hexdigest()[:8]
    child = reg.register(key=child_key, model="m1", host="B", text=SPAWN_ONE,
                         holds_slot=True, source="header")
    key, thread = reg.key_for_family(MAIN_KEY, "m1", SPAWN_ONE + "\nmore")
    assert key == child_key and thread is child

    key, thread = reg.key_for_family(MAIN_KEY, "m1", LONG_OPENING)
    assert key == MAIN_KEY and thread is None


def test_92_child_threads_are_ordinary_threads_for_slots_and_eviction() -> None:
    """Nothing about a child key is special: it holds a slot on its host and is
    evictable like any other idle thread, so Part 2 needs no new rules."""
    reg = _registry()
    _main_thread(reg, host="A")

    children = []
    for brief in (SPAWN_ONE, SPAWN_TWO, SPAWN_THREE, SPAWN_FOUR):
        key, _ = reg.key_for_family(MAIN_KEY, "m1", brief)
        children.append(reg.register(key=key, model="m1", host="B", text=brief,
                                     holds_slot=True, source="header"))

    assert len({t.key for t in children}) == 4
    assert reg.slots("B", "m1") == 4
    assert reg.slots_on_host("B") == 4
    assert reg.slots("A", "m1") == 1
    assert reg.slots_on_host("A") == 1

    reg.enter(children[0])                    # a running child is not evictable
    assert reg.evictable("B") in children[1:]
    reg.leave(children[0])
    victim = reg.evictable("B")
    assert victim is not None and victim.key.startswith(MAIN_KEY + "#")


def test_92_the_label_keeps_the_work_unit_suffix_visible() -> None:
    """24 characters, as before — but a hash key keeps its `#` and 8 hex, or two
    spawns of one session would render as two identical rows (issue #68/#92)."""
    plain_key = "sid:20261119_101112_abcdef"
    plain = Thread(key=plain_key, model="m1", host="A", prefix="",
                   source="header", created=0.0, last_seen=0.0)
    assert plain.label == plain_key[:LABEL_CHARS]      # byte-identical to today
    assert plain.label == "sid:20261119_101112_abcd"
    assert len(plain.label) == LABEL_CHARS

    spawn = Thread(key=MAIN_KEY + "#3f2a1b9c", model="m1", host="A", prefix="",
                   source="header", created=0.0, last_seen=0.0)
    assert len(spawn.label) == LABEL_CHARS
    assert spawn.label.endswith("#3f2a1b9c")
    assert spawn.label == "sid:gwk_a69118d#3f2a1b9c"
    assert spawn.label != plain.label

    # two spawns of one session stay distinguishable in the Threads tab
    other = Thread(key=MAIN_KEY + "#9c8b7a6f", model="m1", host="A", prefix="",
                   source="header", created=0.0, last_seen=0.0)
    assert other.label != spawn.label

    # a key shorter than the label keeps its whole self, no overlap
    short = Thread(key="sid:a#12345678", model="m1", host="A", prefix="",
                   source="header", created=0.0, last_seen=0.0)
    assert short.label == "sid:a#12345678"


# ---------------------------------------------------------------------------
# Work-unit granularity on the wire: `_identify()` asks for the family key
# (issue #92, phase 2 part 2)
# ---------------------------------------------------------------------------
# The rules above are the registry's; these tests prove the *wiring*, i.e. that
# the request path actually asks `key_for_family()` — with the text that
# `register()` stores — and that the level it names is the level that gets
# registered. Nothing here re-derives a key: every expectation is computed from
# `prompt_text()` / `fingerprint()`, the same functions the proxy calls.
SPAWN_BRIEF = ("You are a subagent. Read ripple/registry.py and report exactly how a "
               "slot is reserved, then stop.")


def _header_sid() -> dict[str, str]:
    """The affinity header naming MAIN_KEY's session."""
    return {"X-Session-Id": MAIN_KEY.removeprefix("sid:")}


def _threads(harness) -> dict[str, Thread]:
    """The registry's live threads, by full key (what `/healthz` truncates)."""
    return dict(harness.ripple.registry.threads)


def _child_key(harness, body: dict) -> str:
    """The child key the registry derives for *body*: family + `#` + fp8.

    Computed here from the same `prompt_text()` and the same slice
    (`prefix_chars`) `key_for_family()` uses, so the assertion is arithmetic
    rather than "whatever the code returned".
    """
    text = prompt_text(body)
    fp8 = hashlib.sha256(
        text[:harness.ripple.cfg.affinity.prefix_chars].encode("utf-8")).hexdigest()[:8]
    return f"{MAIN_KEY}#{fp8}"


async def test_92_wire_a_header_follow_up_keeps_the_main_thread(stub_factory,
                                                                ripple_factory):
    """A grown prompt with the same header is the same conversation (ratio ≈ 1.0).

    This is the half of the family rule that must not move: the main thread keeps
    its key, its host and its slot, and no child appears next to it.
    """
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))
    sid = _header_sid()

    first = await harness.ok(chat_body("m1", LONG_OPENING, turns=2), headers=sid)
    assert first.headers["x-ripple-thread"] == MAIN_KEY
    bound = first.headers["x-ripple-host"]

    again = await harness.ok(chat_body("m1", LONG_OPENING, turns=3), headers=sid)
    assert again.headers["x-ripple-reason"] == "thread"
    assert again.headers["x-ripple-thread"] == MAIN_KEY
    assert again.headers["x-ripple-host"] == bound

    threads = _threads(harness)
    assert list(threads) == [MAIN_KEY]
    assert threads[MAIN_KEY].source == "header"
    assert threads[MAIN_KEY].turns == 2


async def test_92_wire_a_spawn_under_the_same_header_becomes_its_own_thread(
        stub_factory, ripple_factory):
    """The measured bug: one header, three parallel tasks, one thread.

    The spawn shares the session's header but almost none of its prompt, so the
    request path must register it under `<header>#<fp8>` — a thread of its own,
    which placement is then free to put on the other host.
    """
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))
    sid = _header_sid()

    main = await harness.ok(chat_body("m1", LONG_OPENING, turns=2), headers=sid)
    assert main.headers["x-ripple-thread"] == MAIN_KEY

    body = chat_body("m1", SPAWN_BRIEF, turns=2)
    spawn = await harness.ok(body, headers=sid)
    expected = _child_key(harness, body)

    assert spawn.headers["x-ripple-reason"] == "new_thread"
    assert spawn.headers["x-ripple-thread"] != main.headers["x-ripple-thread"]
    # the label keeps the `#fp8` visible (issue #68/#92), it is not the whole key
    assert spawn.headers["x-ripple-thread"] == Thread(
        key=expected, model="m1", host="A", prefix="", source="header",
        created=0.0, last_seen=0.0).label
    assert spawn.headers["x-ripple-thread"].endswith(expected[-9:])

    threads = _threads(harness)
    assert set(threads) == {MAIN_KEY, expected}
    child = threads[expected]
    assert child.source == "header"                 # the level lives in the key
    assert child.prefix == prompt_text(body)[:harness.ripple.cfg.affinity.prefix_chars]
    assert child.kind == "interactive"

    # the point of the split: the spawn is bound on its own, so the two threads
    # may sit on different hosts — here the empty node takes the second one.
    assert {threads[MAIN_KEY].host, child.host} == {"A", "B"}
    assert (await harness.healthz())["threads"]["active"] == 2

    # a short one-shot under the same header still holds no slot (FA-7): it is
    # named like a child but never registers, so the registry does not grow
    probe = await harness.ok(chat_body("m1", "what time is it", turns=1), headers=sid)
    assert probe.headers["x-ripple-reason"] == "ephemeral"
    assert set(_threads(harness)) == {MAIN_KEY, expected}


async def test_92_wire_a_child_follow_up_stays_on_the_child(stub_factory,
                                                            ripple_factory):
    """Rule 2 at the child level: its own opening is what the next turn matches.

    The main thread's prefix is in the same family and must not win — the child
    stored the spawn's opening, so the follow-up stays on the child's host.
    """
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))
    sid = _header_sid()

    await harness.ok(chat_body("m1", LONG_OPENING, turns=2), headers=sid)
    body = chat_body("m1", SPAWN_BRIEF, turns=2)
    spawn = await harness.ok(body, headers=sid)

    follow = await harness.ok(chat_body("m1", SPAWN_BRIEF, turns=3), headers=sid)
    assert follow.headers["x-ripple-reason"] == "thread"
    assert follow.headers["x-ripple-thread"] == spawn.headers["x-ripple-thread"]
    assert follow.headers["x-ripple-host"] == spawn.headers["x-ripple-host"]

    threads = _threads(harness)
    assert set(threads) == {MAIN_KEY, _child_key(harness, body)}
    assert threads[_child_key(harness, body)].turns == 2
    assert threads[MAIN_KEY].turns == 1


async def test_92_wire_without_a_header_the_fingerprint_still_names_the_thread(
        stub_factory, ripple_factory):
    """The no-header branch did not move: fingerprint, then fleet-wide prefix.

    A family key needs a header, so a request without one must keep producing
    exactly the key `fingerprint()` names — no `#`, no family lookup.
    """
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))

    body = chat_body("m1", LONG_OPENING, turns=2)
    first = await harness.ok(body)
    expected = fingerprint("m1", body)
    assert expected is not None and "#" not in expected
    assert first.headers["x-ripple-thread"] == expected[:LABEL_CHARS]
    assert list(_threads(harness)) == [expected]

    again = await harness.ok(chat_body("m1", LONG_OPENING, turns=3))
    assert again.headers["x-ripple-reason"] == "thread"
    assert again.headers["x-ripple-thread"] == first.headers["x-ripple-thread"]
    assert again.headers["x-ripple-host"] == first.headers["x-ripple-host"]
    assert (await harness.stats())["prefix_matches"] == 0

    # and the fleet-wide prefix match still adopts a compressed body, unchanged
    compressed = LONG_OPENING[:4000] + " [compressed]"
    third = await harness.ok(chat_body("m1", compressed, turns=1))
    assert third.headers["x-ripple-thread"] == first.headers["x-ripple-thread"]
    assert (await harness.stats())["prefix_matches"] == 1
    assert len(_threads(harness)) == 1


async def test_92_wire_a_child_is_never_promoted(stub_factory, ripple_factory,
                                                 tmp_path):
    """A child carries `source == "header"`, so the promotion guard is blind to it.

    The promotion rule is `source == "prefix_match"`; a spawn is found by its
    header, so a batch session's child is born with the *request's* class and the
    main batch thread keeps its own — nothing is promoted behind the operator's
    back, and `threads_promoted` does not move.
    """
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml"),
        affinity=AffinityConfig(ttl_seconds=1800, batch_ttl_seconds=120)))
    job = harness.ripple.keys.create("nightly", "batch")
    human = harness.ripple.keys.create("hermes", "interactive")
    sid = _header_sid()

    await harness.ok(chat_body("m1", LONG_OPENING, turns=2),
                     headers={**bearer(job.key), **sid})
    body = chat_body("m1", SPAWN_BRIEF, turns=2)
    await harness.ok(body, headers={**bearer(human.key), **sid})

    threads = _threads(harness)
    child = threads[_child_key(harness, body)]
    assert threads[MAIN_KEY].kind == "batch"        # the opener's class, untouched
    assert child.kind == "interactive"              # born from this request
    assert child.source == "header"
    assert harness.ripple.stats.threads_promoted == 0


async def test_92_wire_the_admin_delete_takes_the_child_label(stub_factory,
                                                              ripple_factory,
                                                              tmp_path):
    """A label with `#` is deletable, and two children stay distinguishable.

    `dashboard.js` sends the label through `encodeURIComponent`, so `#` arrives
    as `%23`; the route resolves it against the registry (the full key is
    longer) and must find exactly one thread. Deleting one child leaves the
    other one — the suffix is what keeps the rows apart.
    """
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml")))
    sid = _header_sid()
    human = harness.ripple.keys.create("hermes", "interactive")
    sid = {**bearer(human.key), **sid}

    await harness.ok(chat_body("m1", LONG_OPENING, turns=2), headers=sid)
    first_body = chat_body("m1", SPAWN_ONE, turns=2)
    second_body = chat_body("m1", SPAWN_TWO, turns=2)
    await harness.ok(first_body, headers=sid)
    await harness.ok(second_body, headers=sid)

    threads = _threads(harness)
    assert set(threads) == {MAIN_KEY, _child_key(harness, first_body),
                            _child_key(harness, second_body)}
    labels = [row["key"] for row in (await harness.healthz())["threads"]["registry"]]
    assert len(set(labels)) == 3
    child_labels = [label for label in labels if "#" in label]
    assert len(child_labels) == 2 and all(len(label) == LABEL_CHARS for label in child_labels)

    doomed = threads[_child_key(harness, first_body)].label
    resp = await harness.client.delete("/admin/threads/" + quote(doomed, safe=""),
                                       headers=bearer("test-admin-key"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == doomed

    left = _threads(harness)
    assert set(left) == {MAIN_KEY, _child_key(harness, second_body)}
    assert threads[_child_key(harness, second_body)].label in \
        [row["key"] for row in (await harness.healthz())["threads"]["registry"]]


