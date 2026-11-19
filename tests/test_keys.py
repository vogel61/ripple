"""Key handling: FA-15 / AS-10 — keys are used, never logged, never exposed.

Plus the three traffic classes a key can carry (`interactive` / `batch` /
`one-shot`) where they change behaviour: the TTL of the thread a key opens.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from ripple.config import AffinityConfig, ConfigError, ProxyConfig, load_config
from ripple.keys import AdminRateLimiter

from tests.support import (bearer, chat_body, host_config, keyed_config,
                           make_config, prompt_for, stub_model_loaded)

UPSTREAM_KEY = "sk-upstream-secret-8f3a91"
CLIENT_KEY = "sk-client-secret-2b7c40"


def _yaml(url: str, node_id: str) -> str:
    return f"""
listen_host: 127.0.0.1
listen_port: 18010
log_level: info
defaults:
  max_inflight: 8
  max_threads: 20
  spill_threshold: 3
  hard_threshold: 0.95
hosts:
  - name: A
    node_id: {node_id}
    weight: 3
    api_key: ${{RIPPLE_TEST_UPSTREAM_KEY}}
    addresses:
      - {url}
affinity:
  header: X-Session-Id
  prefix_chars: 4096
  match_threshold: 0.5
  min_prompt_tokens: 8192
  ephemeral_window_seconds: 60
placement:
  binding_strategy: deficit
discovery:
  interval_seconds: 3600
  timeout_seconds: 2
"""


async def test_fa15_as10_keys_never_appear_in_stats_healthz_or_logs(
        stub_factory, ripple_factory, monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("RIPPLE_TEST_UPSTREAM_KEY", UPSTREAM_KEY)

    a = stub_factory(models=[stub_model_loaded("m1")])
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_yaml(a.url, a.state.node_id), encoding="utf-8")

    cfg = load_config(config_path)
    # FA-15: ${VAR} is expanded from the environment
    assert cfg.hosts[0].api_key == UPSTREAM_KEY
    assert not cfg.warnings

    harness = await ripple_factory(cfg)
    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                            headers={"Authorization": f"Bearer {CLIENT_KEY}"})

    # the configured upstream key wins over the client key
    assert a.state.auth_headers == [f"Bearer {UPSTREAM_KEY}"]
    assert resp.status_code == 200

    stats_text = (await harness.client.get("/stats")).text
    health_text = (await harness.client.get("/healthz")).text
    config_text = (await harness.client.get("/config")).text
    for secret in (UPSTREAM_KEY, CLIENT_KEY):
        assert secret not in stats_text
        assert secret not in health_text
        assert secret not in config_text
        assert secret not in caplog.text

    # /config shows the effective values but only whether a key is set
    config_json = (await harness.client.get("/config")).json()
    assert config_json["hosts"][0]["api_key"] == "set"
    assert config_json["hosts"][0]["weight"] == 3
    assert "PUT_YOUR" not in config_text

    # the request log did run, so the check above is meaningful
    assert "model=m1" in caplog.text
    # with a single host offering the model, placement reports model_pinned
    assert "reason=" in caplog.text


async def test_fa15_client_key_is_passed_through_without_an_upstream_key(
        stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
    ]))

    await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                     headers={"Authorization": f"Bearer {CLIENT_KEY}"})

    assert a.state.auth_headers == [f"Bearer {CLIENT_KEY}"]


# ---------------------------------------------------------------------------
# Traffic classes: the TTL a key's threads get (FA-19, kind)
# ---------------------------------------------------------------------------
def _affinity_yaml(extra: str = "") -> str:
    """The config above with one extra line in the `affinity:` block."""
    return _yaml("http://127.0.0.1:1", "node-1").replace(
        "  prefix_chars:", f"{extra}  prefix_chars:")


def test_affinity_batch_ttl_defaults_to_120_and_rejects_zero(tmp_path):
    """`affinity.batch_ttl_seconds` is configurable with a floor of 1.

    A TTL of 0 would expire every batch thread before its second turn, i.e.
    silently turn `batch` into `one-shot` — that is a configuration error, not a
    tuning choice.
    """
    path = tmp_path / "config.yaml"
    path.write_text(_affinity_yaml(), encoding="utf-8")
    assert load_config(path).affinity.batch_ttl_seconds == 120

    path.write_text(_affinity_yaml("  batch_ttl_seconds: 30\n"), encoding="utf-8")
    assert load_config(path).affinity.batch_ttl_seconds == 30

    path.write_text(_affinity_yaml("  batch_ttl_seconds: 0\n"), encoding="utf-8")
    with pytest.raises(ConfigError, match="batch_ttl_seconds"):
        load_config(path)


async def test_kind_batch_shortens_the_thread_ttl_interactive_keeps_it(
        stub_factory, ripple_factory, tmp_path):
    """A thread opened by a `batch` key ages by `batch_ttl_seconds`, one opened
    by an `interactive` key by `affinity.ttl_seconds`.

    Both live in the same registry at the same time, so the TTL has to be
    carried by the thread — one global number cannot describe two traffic
    classes. The batch TTL is 1 s here instead of 120 s: same code path, and a
    test that waits two minutes is a test nobody runs.
    """
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml"),
        affinity=AffinityConfig(ttl_seconds=1800, batch_ttl_seconds=1)))
    job = harness.ripple.keys.create("nightly", "batch")
    human = harness.ripple.keys.create("hermes", "interactive")

    first = await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                             headers={**bearer(job.key), "X-Session-Id": "job"})
    assert first.headers["x-ripple-thread"] == "sid:job"
    row = (await harness.stats())["threads"][0]
    assert row["kind"] == "batch"
    assert row["ttl_seconds"] == 1

    second = await harness.ok(chat_body("m1", prompt_for(1), turns=2),
                              headers={**bearer(human.key), "X-Session-Id": "human"})
    assert second.headers["x-ripple-thread"] == "sid:human"
    assert await harness.slots() == {"A": 2}

    await asyncio.sleep(1.2)          # past the batch TTL, far inside the other

    registry = harness.ripple.registry
    # the purge loop expires exactly one of the two — the batch thread
    assert registry.purge() == 1
    assert registry.lookup("sid:job", "m1") is None
    assert registry.lookup("sid:human", "m1") is not None
    # the batch slot is free again, the conversation still holds its own
    assert await harness.slots() == {"A": 1}
    assert [r["key"] for r in (await harness.stats())["threads"]] == ["sid:human"]

    # the expired thread is not resurrected: the same header opens a new thread
    again = await harness.ok(chat_body("m1", prompt_for(0), turns=3),
                             headers={**bearer(job.key), "X-Session-Id": "job"})
    assert again.headers["x-ripple-thread"] == "sid:job"
    assert harness.ripple.registry.lookup("sid:job", "m1").turns == 1
    # … while the interactive one is still bound to its host
    third = await harness.ok(chat_body("m1", prompt_for(1), turns=3),
                             headers={**bearer(human.key), "X-Session-Id": "human"})
    assert third.headers["x-ripple-reason"] == "thread"
    assert third.headers["x-ripple-thread"] == "sid:human"


# ---------------------------------------------------------------------------
# Admin-key attempt limit (issue #6): a four-digit key behind an open door
# ---------------------------------------------------------------------------
ADMIN = "test-admin-key"      # what `keyed_config` puts in `auth.admin_key`


def _admin_harness(stub, tmp_path, **cfg_changes):
    """A keyed config with client auth on and `ADMIN` as the admin key."""
    cfg = keyed_config([host_config("A", stub.url, node_id=stub.state.node_id)],
                       str(tmp_path / "keys.yaml"))
    for name, value in cfg_changes.items():
        setattr(cfg, name, value)
    return cfg


async def test_admin_sixth_wrong_key_within_a_minute_is_429_not_401(
        stub_factory, ripple_factory, tmp_path, caplog):
    """Five 401s, then a 429 with `Retry-After`; the block is one log line and
    one counter, not five.

    The probe is `GET /admin/keys` because that is exactly what the `/keys`
    page sends with the admin key from `sessionStorage` — a wrong key typed
    there has to hit the same limiter as one from `curl`.
    """
    caplog.set_level(logging.INFO)
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_admin_harness(stub, tmp_path))

    for _ in range(5):
        resp = await harness.client.get("/admin/keys", headers=bearer("0000"))
        assert resp.status_code == 401
        assert resp.json()["error"]["type"] == "invalid_request_error"

    sixth = await harness.client.get("/admin/keys", headers=bearer("0000"))
    assert sixth.status_code == 429, sixth.text
    retry_after = int(sixth.headers["retry-after"])
    assert 1 <= retry_after <= 60
    error = sixth.json()["error"]
    assert error["code"] == "admin_rate_limited"
    assert error["type"] == "rate_limit_error"
    assert "retry in" in error["message"]

    # The limiter runs *before* the comparison, so a correct key is refused too
    # while the window is open. The alternative — compare first, block only the
    # wrong answers — would keep guessing at full speed and only relabel it.
    assert (await harness.client.get("/admin/keys",
                                    headers=bearer(ADMIN))).status_code == 429

    stats = await harness.stats()
    assert stats["admin_key_blocks"] == 2      # the two 429s
    assert stats["auth_failures"] == 5         # the 401s that did compare

    # One line per block, not per attempt: the refused requests log nothing.
    assert caplog.text.count("admin_blocked") == 1
    assert caplog.text.count("admin_denied") == 5
    # … and the admin key never appears in the log (FA-15, AS-10)
    assert "0000" not in caplog.text and ADMIN not in caplog.text


async def test_admin_correct_key_is_accepted_again_once_the_window_has_passed(
        stub_factory, ripple_factory, tmp_path):
    """The block rolls with its window — no lockout that needs a restart.

    One-second window instead of sixty: same code path, and a test that waits a
    minute is a test nobody runs.
    """
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_admin_harness(stub, tmp_path))
    harness.ripple.admin_limits = AdminRateLimiter(limit=2, window_seconds=1.0)

    assert (await harness.client.get("/admin/keys", headers=bearer("x"))).status_code == 401
    assert (await harness.client.get("/admin/keys", headers=bearer("x"))).status_code == 401
    assert (await harness.client.get("/admin/keys", headers=bearer(ADMIN))).status_code == 429

    await asyncio.sleep(1.05)                  # past the window, nothing else

    ok = await harness.client.get("/admin/keys", headers=bearer(ADMIN))
    assert ok.status_code == 200, ok.text
    assert [k["name"] for k in ok.json()["keys"]] == []
    # the success cleared the counter: one typo does not immediately block again
    assert (await harness.client.get("/admin/keys", headers=bearer("x"))).status_code == 401
    assert (await harness.stats())["admin_key_blocks"] == 1


async def test_admin_block_is_per_client_address(stub_factory, ripple_factory, tmp_path):
    """One address burning its budget does not lock out another.

    Two in-process clients with distinct transport peers: `127.0.0.1` (the
    default) and `192.0.2.60`. The second one must keep its admin access while
    the first is blocked — a per-process limiter would have taken the operator
    down with the guesser.
    """
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_admin_harness(stub, tmp_path))

    other = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.ripple.app,
                                      client=("192.0.2.60", 41234)),
        base_url="http://ripple", timeout=30.0)
    try:
        for _ in range(5):
            assert (await harness.client.get("/admin/keys",
                                             headers=bearer("0000"))).status_code == 401
        assert (await harness.client.get("/admin/keys", headers=bearer(ADMIN))).status_code == 429
        # the other address still gets in with the correct key
        assert (await other.get("/admin/keys", headers=bearer(ADMIN))).status_code == 200
        # … and has its own budget, not the blocked one's
        assert (await other.get("/admin/keys", headers=bearer("0000"))).status_code == 401
    finally:
        await other.aclose()


async def test_admin_block_uses_the_trusted_forwarded_origin(stub_factory, ripple_factory,
                                                             tmp_path):
    """Behind a trusted proxy the block is per *origin*, not per proxy hop.

    `client_address()` is the only place that decides what the address is, so
    two origins arriving through the same trusted peer get two budgets, and the
    same origin cannot escape by adding `X-Forwarded-For` entries of its own
    (the trusted peer's entry is the right-most one and wins).
    """
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_admin_harness(
        stub, tmp_path, proxy=ProxyConfig(trusted_proxies=["127.0.0.1/32"])))

    for _ in range(5):
        assert (await harness.client.get(
            "/admin/keys",
            headers={**bearer("0000"), "X-Forwarded-For": "192.0.2.70"})).status_code == 401
    blocked = await harness.client.get(
        "/admin/keys", headers={**bearer(ADMIN), "X-Forwarded-For": "192.0.2.70"})
    assert blocked.status_code == 429
    # a forged extra hop does not buy a fresh budget: the origin is still .70
    assert (await harness.client.get(
        "/admin/keys",
        headers={**bearer(ADMIN), "X-Forwarded-For": "192.0.2.71, 192.0.2.70"})).status_code == 429
    # a different origin through the same peer is unaffected
    assert (await harness.client.get(
        "/admin/keys",
        headers={**bearer(ADMIN), "X-Forwarded-For": "192.0.2.99"})).status_code == 200


def test_admin_limiter_prunes_expired_addresses_and_counts_down():
    """Per-address state disappears with its window — no leak, no reset.

    Driven with explicit timestamps: the pruning is what keeps the limiter from
    growing a bucket per address that ever guessed once, and `Retry-After` has
    to count down as the oldest failure ages out rather than stay at 60.
    """
    limiter = AdminRateLimiter(limit=2, window_seconds=10.0)
    assert limiter.retry_after("a", now=100.0) is None
    assert limiter.note_failure("a", now=100.0) is None        # 1 of 2
    assert limiter.note_failure("b", now=100.0) is None        # 1 of 2, own budget
    assert limiter.retry_after("a", now=100.0) is None         # still under the limit
    assert limiter.note_failure("a", now=101.0) == 9           # 2 of 2 — the block starts
    assert limiter.retry_after("a", now=101.0) == 9            # 10 s window, 1 s used
    assert limiter.retry_after("b", now=101.0) is None         # "b" was not affected
    assert limiter.retry_after("a", now=109.0) == 1            # counts down, no fixed 60
    assert limiter.retry_after("a", now=110.0) is None         # the oldest one aged out
    assert limiter.tracked() == 1                              # … "a" still holds one stamp
    assert limiter.retry_after("a", now=111.0) is None
    assert limiter.tracked() == 0                              # nothing left behind
