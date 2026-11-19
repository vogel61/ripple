"""Client identity: who sent a request and where its thread id comes from (issue #3).

`log_request()` said model, host, reason, thread, size, duration and status —
nothing about the caller, and nothing about whether the thread id was the
client's own claim (`X-Session-Id`, `sid:…`) or a guess from the prompt
(fingerprint, `fp`). Both are now on the request line and in the `/stats`
decision ring buffer, so a request can be attributed to an origin.

`X-Forwarded-For` is believed **only** from a configured trusted peer
(`proxy.trusted_proxies`, default empty = never): the header is written by
whoever sends it, so from an untrusted peer it is a claim about itself, not an
origin. From a trusted peer it is read from the right — the right-most entry is
the one that peer appended, the left-most is what the client wrote.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from ripple.config import ConfigError, ProxyConfig, load_config

from tests.support import (chat_body, host_config, make_config, prompt_for,
                           stub_model_loaded)


def peer_client(harness, peer: str) -> httpx.AsyncClient:
    """A client for the same app that presents `peer` as its transport address.

    ASGITransport reports 127.0.0.1 by default; a test that has to tell a
    trusted peer from an untrusted one has to move the peer.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.ripple.app, client=(peer, 40_000)),
        base_url="http://ripple", timeout=30.0)


def request_lines(caplog) -> list[str]:
    """The rendered `ripple.request` lines, in order."""
    return [r.getMessage() for r in caplog.records if r.name == "ripple.request"]


def _one_host(stub_factory, **proxy):
    a = stub_factory(models=[stub_model_loaded("m1")])
    hosts = [host_config("M5", a.url, node_id=a.state.node_id)]
    return make_config(hosts, proxy=ProxyConfig(**proxy) if proxy else None)


# ---------------------------------------------------------------------------
# The client address
# ---------------------------------------------------------------------------
async def test_client_address_is_logged_and_in_the_decision_ring(
        stub_factory, ripple_factory, caplog):
    """A request from 127.0.0.1 logs `client=127.0.0.1`."""
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory))

    await harness.ok(chat_body("m1", prompt_for(0), turns=2))

    assert "client=127.0.0.1" in request_lines(caplog)[-1]
    decision = (await harness.stats())["decisions"][-1]
    assert decision["client"] == "127.0.0.1"


async def test_spoofed_forwarded_for_from_an_untrusted_peer_is_ignored(
        stub_factory, ripple_factory, caplog):
    """No trusted peer configured: the header is a claim, not an origin."""
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory))

    await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                     headers={"X-Forwarded-For": "1.2.3.4"})

    assert "client=127.0.0.1" in request_lines(caplog)[-1]
    assert "1.2.3.4" not in caplog.text
    assert (await harness.stats())["decisions"][-1]["client"] == "127.0.0.1"


async def test_a_spoofed_header_is_ignored_over_a_real_socket(stub_factory,
                                                              live_proxy_factory,
                                                              caplog):
    """uvicorn must not rewrite the peer from the header behind RIPPLE's back.

    uvicorn's own `ProxyHeadersMiddleware` is on by default and believes
    `X-Forwarded-For` from 127.0.0.1 — i.e. from every process on this Mac,
    which is exactly the case that needs no key. Observed live before the fix:
    a local request claiming `1.2.3.4` was logged as `from=1.2.3.4` and refused
    with 401. The peer is therefore read with `proxy_headers=False`, and this
    test goes over a real socket rather than ASGITransport, which has no
    middleware at all.
    """
    caplog.set_level(logging.INFO)
    url = live_proxy_factory(_one_host(stub_factory))

    async with httpx.AsyncClient(base_url=url, timeout=30.0) as client:
        resp = await client.post("/v1/chat/completions",
                                 json=chat_body("m1", prompt_for(0), turns=2),
                                 headers={"X-Forwarded-For": "1.2.3.4"})
    assert resp.status_code == 200, resp.text

    assert "client=127.0.0.1" in request_lines(caplog)[-1]
    assert "1.2.3.4" not in caplog.text


async def test_forwarded_for_is_used_from_a_configured_trusted_peer(
        stub_factory, ripple_factory, caplog):
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory, trusted_proxies=["127.0.0.1"]))

    await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                     headers={"X-Forwarded-For": "1.2.3.4"})

    assert "client=1.2.3.4" in request_lines(caplog)[-1]
    assert (await harness.stats())["decisions"][-1]["client"] == "1.2.3.4"
    # the effective configuration shows the list, it is not a secret
    config = (await harness.client.get("/config")).json()
    assert config["proxy"]["trusted_proxies"] == ["127.0.0.1"]


async def test_a_trusted_peer_is_a_peer_not_a_header(stub_factory, ripple_factory,
                                                    caplog):
    """The same header from another address stays a claim."""
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory, trusted_proxies=["127.0.0.1"]))

    async with peer_client(harness, "192.0.2.50") as client:
        resp = await client.post("/v1/chat/completions",
                                 json=chat_body("m1", prompt_for(0), turns=2),
                                 headers={"X-Forwarded-For": "1.2.3.4"})
    assert resp.status_code == 200

    assert "client=192.0.2.50" in request_lines(caplog)[-1]
    assert "1.2.3.4" not in caplog.text


async def test_a_trusted_network_covers_its_addresses(stub_factory, ripple_factory,
                                                     caplog):
    """`192.0.2.0/24` trusts the LAN without listing every machine."""
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory,
                                             trusted_proxies=["192.0.2.0/24"]))

    async with peer_client(harness, "192.0.2.50") as client:
        await client.post("/v1/chat/completions",
                          json=chat_body("m1", prompt_for(0), turns=2),
                          headers={"X-Forwarded-For": "1.2.3.4"})

    assert "client=1.2.3.4" in request_lines(caplog)[-1]


async def test_a_chain_is_read_from_the_right(stub_factory, ripple_factory, caplog):
    """The left-most entry is what the client wrote — it must never win.

    A client that sends `1.2.3.4` gets its own text appended to by the trusted
    proxy, so the right-most entry is the one the proxy vouched for.
    """
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory, trusted_proxies=["127.0.0.1"]))

    await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                     headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})

    assert "client=5.6.7.8" in request_lines(caplog)[-1]
    assert "1.2.3.4" not in caplog.text


async def test_a_chain_of_trusted_proxies_is_walked_through(stub_factory,
                                                            ripple_factory, caplog):
    """Two trusted hops: the first untrusted entry from the right is the origin."""
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory,
                                             trusted_proxies=["127.0.0.1", "5.6.7.8"]))

    await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                     headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})

    assert "client=1.2.3.4" in request_lines(caplog)[-1]


async def test_unparseable_forwarded_for_falls_back_to_the_peer(stub_factory,
                                                               ripple_factory, caplog):
    """A hostname, a `host:port` or rubbish never becomes an origin."""
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory, trusted_proxies=["127.0.0.1"]))

    await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                     headers={"X-Forwarded-For": "not-an-address"})

    assert "client=127.0.0.1" in request_lines(caplog)[-1]
    assert "not-an-address" not in caplog.text


async def test_the_error_line_carries_the_client_too(stub_factory, ripple_factory,
                                                     caplog):
    """A refused request is exactly the one an operator has to attribute."""
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory))

    resp = await harness.chat(chat_body("ghost", prompt_for(0), turns=2))
    assert resp.status_code == 404, resp.text

    line = request_lines(caplog)[-1]
    assert "client=127.0.0.1" in line
    assert "thread_source=-" in line        # no thread, no source
    decision = (await harness.stats())["decisions"][-1]
    assert decision["client"] == "127.0.0.1"
    assert decision["thread_source"] is None


# ---------------------------------------------------------------------------
# Where the thread id comes from
# ---------------------------------------------------------------------------
async def test_thread_source_is_sid_when_the_client_names_its_thread(
        stub_factory, ripple_factory, caplog):
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory))

    await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                     headers={"X-Session-Id": "sess-7"})

    line = request_lines(caplog)[-1]
    assert "thread=sid:sess-7" in line
    assert "thread_source=sid" in line
    decision = (await harness.stats())["decisions"][-1]
    assert decision["thread"] == "sid:sess-7"
    assert decision["thread_source"] == "sid"


async def test_thread_source_is_fp_when_the_thread_is_guessed(stub_factory,
                                                             ripple_factory, caplog):
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory))

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))

    line = request_lines(caplog)[-1]
    assert f"thread={resp.headers['x-ripple-thread']}" in line
    assert "thread_source=fp" in line
    decision = (await harness.stats())["decisions"][-1]
    assert decision["thread"] == resp.headers["x-ripple-thread"]
    assert decision["thread_source"] == "fp"


async def test_the_source_describes_the_thread_not_the_request(stub_factory,
                                                               ripple_factory, caplog):
    """A later turn without the header adopts the header's thread — and says how.

    The id is the client's (`sid:`), but this request did not use the header: it
    reached the thread by prompt similarity. Labelling that `sid` would claim a
    source the request never consulted (issue #68), so it is `sid+fp`.
    """
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory))
    prompt = prompt_for(0)

    await harness.ok(chat_body("m1", prompt, turns=2),
                     headers={"X-Session-Id": "sess-7"})
    adopted = await harness.ok(chat_body("m1", prompt, turns=3))

    assert adopted.headers["x-ripple-thread"] == "sid:sess-7"
    assert adopted.headers["x-ripple-reason"] == "thread"
    line = request_lines(caplog)[-1]
    assert "thread=sid:sess-7" in line
    assert "thread_source=sid+fp" in line
    decision = (await harness.stats())["decisions"][-1]
    assert decision["thread"] == "sid:sess-7"
    assert decision["thread_source"] == "sid+fp"


async def test_a_prefix_match_into_a_fingerprint_thread_stays_fp(
        stub_factory, ripple_factory, caplog):
    """The third value is only for sid threads: an fp thread reached by prefix
    match was opened the same way it is found, so `fp` is still the honest
    label (issue #68 adds no value where the source did not change)."""
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory))
    prompt = prompt_for(1)

    await harness.ok(chat_body("m1", prompt, turns=2))
    # A changed opening message means a new fingerprint; the stored prefix still
    # matches, so this is a prefix match into the thread above.
    adopted = await harness.ok(chat_body("m1", prompt + " (more)", turns=2))

    assert adopted.headers["x-ripple-reason"] == "thread"
    assert not adopted.headers["x-ripple-thread"].startswith("sid:")
    assert "thread_source=fp" in request_lines(caplog)[-1]


async def test_two_sessions_of_one_hour_get_two_labels(stub_factory,
                                                       ripple_factory, caplog):
    """Issue #68: `key[:16]` is `sid:` plus 12 characters, and a Hermes id is
    `YYYYMMDD_HHMMSS_<random>` — so every session of one hour logged the same
    thread. The label is 24 characters: prefix and the whole stamp fit."""
    caplog.set_level(logging.INFO)
    harness = await ripple_factory(_one_host(stub_factory))

    first = await harness.ok(chat_body("m1", prompt_for(0), turns=2),
                             headers={"X-Session-Id": "20261117_150500_gwk_66695d98"})
    second = await harness.ok(chat_body("m1", prompt_for(2), turns=2),
                              headers={"X-Session-Id": "20261117_153012_gwk_11112222"})

    one = first.headers["x-ripple-thread"]
    two = second.headers["x-ripple-thread"]
    assert len(one) == len(two) == 24
    assert one == "sid:20261117_150500_gwk_"
    assert two == "sid:20261117_153012_gwk_"
    assert one != two
    lines = request_lines(caplog)
    assert f"thread={one}" in lines[-2]
    assert f"thread={two}" in lines[-1]
    assert "thread_source=sid" in lines[-2] and "thread_source=sid" in lines[-1]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _yaml(trusted: str = "") -> str:
    return f"""
listen_host: 127.0.0.1
listen_port: 18010
defaults:
  max_inflight: 8
  max_threads: 20
  spill_threshold: 3
hosts:
  - name: A
    addresses:
      - http://127.0.0.1:9
proxy:
  request_log: true
{trusted}"""


def test_trusted_proxies_default_to_empty(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_yaml(), encoding="utf-8")

    assert load_config(path).proxy.trusted_proxies == []


def test_trusted_proxies_accept_addresses_and_networks(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_yaml("  trusted_proxies:\n    - 127.0.0.1\n    - 192.0.2.0/24\n"),
                    encoding="utf-8")

    assert load_config(path).proxy.trusted_proxies == ["127.0.0.1", "192.0.2.0/24"]


def test_a_single_trusted_proxy_may_be_written_as_a_string(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_yaml("  trusted_proxies: 127.0.0.1\n"), encoding="utf-8")

    assert load_config(path).proxy.trusted_proxies == ["127.0.0.1"]


def test_an_unparseable_trusted_proxy_is_a_config_error(tmp_path):
    """Trusting the wrong peer forges an origin, so a typo fails loudly."""
    path = tmp_path / "config.yaml"
    path.write_text(_yaml("  trusted_proxies:\n    - localhost\n"), encoding="utf-8")

    with pytest.raises(ConfigError, match="trusted_proxies"):
        load_config(path)
