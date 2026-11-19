"""The per-host ledger (issue #33): idle/prefill/decode time since the start.

The operator asked three questions and every one of them has exactly one honest
source, which is what this file measures:

- **time per node** — sampled from the node itself (`/admin/api/activity`, one
  call per probe tick). The five states are exclusive and add up to the sampled
  window; a tick the node did not answer belongs to no state at all.
- **prefill and decode seconds** — oMLX's own `usage.prompt_eval_duration` and
  `usage.generation_duration`, summed. A response without them is counted in
  `requests_without_usage`, never estimated.
- **the client's wait** — RIPPLE's own wall clock, kept apart from the node's.

Plus the two properties the operator will actually rely on: the ledger survives a
restart, and `POST /admin/stats/reset` zeroes it *and* the file before it
answers.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

import pytest

from ripple.config import (AuthConfig, DiscoveryConfig, ReplicationConfig, StatsConfig)
from ripple.host_stats import (HostStats, cross_from_usage, state_from_activity)

from tests.support import (StubModel, bearer, chat_body, host_config, make_config,
                           wait_until)

ADMIN = bearer("test-admin-key")
MODEL = "m1"

# The usage object oMLX really sends (measured 16.11.2026) — the two durations are
# the whole point of this issue.
OMLX_USAGE = {
    "prompt_tokens": 46, "completion_tokens": 16, "time_to_first_token": 0.39,
    "total_time": 0.68, "prompt_eval_duration": 0.39, "generation_duration": 0.3,
    "prompt_tokens_per_second": 119.35, "generation_tokens_per_second": 53.68,
}


# ---------------------------------------------------------------------------
# The state of a tick
# ---------------------------------------------------------------------------
def _activity(prefilling: int = 0, generating: int = 0, loading: bool = False,
              models: int = 1) -> dict:
    return {"active_models": {"models": [
        {"id": f"m{i}", "is_loading": loading, "active_requests": prefilling + generating,
         "prefilling": [{"id": "p"}] * prefilling,
         "generating": [{"id": "g"}] * generating, "idle_seconds": 0.2}
        for i in range(models)],
        "total_active_requests": prefilling + generating}}


def test_each_tick_maps_to_exactly_one_state():
    """The five states are exclusive by construction, not by convention."""
    assert state_from_activity(_activity(prefilling=1)) == "prefill"
    assert state_from_activity(_activity(generating=1)) == "decode"
    assert state_from_activity(_activity(prefilling=1, generating=1)) == "mixed"
    assert state_from_activity(_activity(loading=True)) == "loading"
    assert state_from_activity(_activity()) == "idle"


def test_a_loading_model_is_not_idle():
    """A 453 GB cold load takes about two minutes and is neither idle nor decode.
    The load flag has to win over `idle`, otherwise the operator's "why is the
    node busy at 3 a.m." has no answer in the block."""
    assert state_from_activity(_activity(loading=True, models=2)) == "loading"
    assert state_from_activity(_activity(loading=False, models=2)) == "idle"


def test_an_unreadable_answer_is_not_a_state():
    """A missing `active_models` is not a statement about the node: `None`, so
    the tick contributes to no state instead of to a guessed `idle`."""
    assert state_from_activity(None) is None
    assert state_from_activity({}) is None
    assert state_from_activity({"active_models": "nope"}) is None
    assert state_from_activity({"active_models": {"models": "nope"}}) is None
    assert state_from_activity({"active_models": {"models": []}}) == "idle"


def test_the_five_states_add_up_to_the_sampled_window():
    """The claim the block makes: the columns are a partition of the window.

    A tick is charged to the state that was observed at the *previous* tick — what
    was seen then is what held in between — so the first tick only records an
    observation and contributes no time.
    """
    ledger = HostStats(["A"])
    ledger.sample({"A": "decode"}, now=1000.0)     # observation only
    ledger.sample({"A": "idle"}, now=1010.0)       # 10 s of decode
    ledger.sample({"A": "prefill"}, now=1025.0)    # 15 s of idle
    ledger.sample({"A": "loading"}, now=1035.0)    # 10 s of prefill
    row = ledger.snapshot(now=1035.0)["hosts"][0]
    assert row["decode"] == 10.0
    assert row["idle"] == 15.0
    assert row["prefill"] == 10.0
    assert row["loading"] == 0.0 and row["mixed"] == 0.0
    assert ledger.sampled_seconds == 35.0
    assert sum(row[state] for state in ("idle", "loading", "prefill", "decode",
                                        "mixed")) == pytest.approx(ledger.sampled_seconds)
    assert row["sampled_seconds"] == pytest.approx(ledger.sampled_seconds)


def test_a_host_that_has_never_answered_contributes_nothing():
    """Two nodes, one of them never answering the activity call: the ledger's
    window keeps running, that node's row stays at zero — and the total row sums
    only the windows that really exist, so no share can exceed 100 %."""
    ledger = HostStats(["A", "B"])
    ledger.sample({"A": "decode"}, now=1000.0)
    ledger.sample({"A": "decode"}, now=1010.0)
    snapshot = ledger.snapshot(now=1010.0)
    rows = {row["name"]: row for row in snapshot["hosts"]}
    assert rows["A"]["decode"] == 10.0
    assert rows["B"]["sampled_seconds"] == 0.0
    assert snapshot["total"]["decode"] == 10.0
    assert snapshot["total"]["sampled_seconds"] == 10.0
    assert snapshot["sampled_seconds"] == 10.0


def test_a_host_that_stops_answering_keeps_its_last_state():
    """The interval is charged to the state that was last seen — otherwise the
    five states would no longer add up to the window, which is the one claim the
    block makes. A node that is silent was last seen doing something."""
    ledger = HostStats(["A", "B"])
    ledger.sample({"A": "decode", "B": "idle"}, now=1000.0)
    ledger.sample({"A": "decode"}, now=1010.0)      # B silent this tick
    rows = {row["name"]: row for row in ledger.snapshot(now=1010.0)["hosts"]}
    assert rows["B"]["idle"] == 10.0
    assert rows["B"]["sampled_seconds"] == 10.0


# ---------------------------------------------------------------------------
# Work counters
# ---------------------------------------------------------------------------
def test_prefill_and_decode_come_from_what_omlx_measured():
    ledger = HostStats(["A"])
    ledger.record("A", usage=OMLX_USAGE, client_seconds=0.68)
    row = ledger.snapshot()["hosts"][0]
    assert row["requests"] == 1
    assert row["prompt_tokens"] == 46
    assert row["completion_tokens"] == 16
    assert row["prefill_seconds"] == 0.39
    assert row["generation_seconds"] == 0.3
    assert row["client_seconds"] == 0.68
    assert row["requests_without_usage"] == 0


def test_a_response_without_usage_is_counted_and_never_split():
    """No usage object (client abort, error, a backend that does not measure) or
    a plain OpenAI usage without the two durations: the tokens may be known, the
    split is not. It is counted and left alone — a real run estimated 25 000
    tokens against 82 510 actual, which is why nothing here is estimated."""
    ledger = HostStats(["A"])
    ledger.record("A", usage=None, client_seconds=2.5)
    ledger.record("A", usage={"prompt_tokens": 12, "completion_tokens": 4},
                  client_seconds=1.5, ok=False)
    row = ledger.snapshot()["hosts"][0]
    assert row["requests"] == 2
    assert row["errors"] == 1
    assert row["prompt_tokens"] == 12
    assert row["completion_tokens"] == 4
    assert row["prefill_seconds"] == 0.0
    assert row["generation_seconds"] == 0.0
    assert row["requests_without_usage"] == 2
    assert row["client_seconds"] == 4.0        # the client's wait is kept


def test_a_request_that_never_reached_the_node_is_an_error_not_a_request():
    ledger = HostStats(["A"])
    ledger.error("A")
    row = ledger.snapshot()["hosts"][0]
    assert row["requests"] == 0 and row["errors"] == 1


def test_the_cross_check_is_read_from_the_nodes_own_totals():
    assert cross_from_usage({"totals": {"requests": 5, "prefill_seconds": 1.5,
                                        "generation_seconds": 2.5}}) == {
        "requests": 5, "prefill_seconds": 1.5, "generation_seconds": 2.5}
    # A field the node does not report stays zero instead of failing the row.
    assert cross_from_usage({"totals": {"requests": 5}})["prefill_seconds"] == 0.0
    assert cross_from_usage(None) is None
    assert cross_from_usage({"no": "totals"}) is None


# ---------------------------------------------------------------------------
# Persistence and reset
# ---------------------------------------------------------------------------
def _ledger(tmp_path: Path, *, name: str = "hs.json", persist: bool = True) -> HostStats:
    return HostStats(["A", "B"], path=tmp_path / name, persist=persist,
                     flush_seconds=0.0)


def test_a_test_config_never_points_at_the_operator_ledger():
    """Same rule as the maintenance windows and the model policy: a test must not
    read or write `~/.ripple/host_stats.json`. The live file is the operator's
    measured history — a test would inherit a fleet that is not its own."""
    cfg = make_config([host_config("M5-alpha", "http://192.0.2.11:8000")])
    assert not str(cfg.stats.file).startswith(str(Path.home()))
    assert str(cfg.stats.file).startswith(tempfile.gettempdir())
    # …and nothing is written at all unless a test asks for the file.
    assert cfg.stats.persist is False


def test_the_ledger_survives_a_restart(tmp_path):
    first = _ledger(tmp_path)
    first.record("A", usage=OMLX_USAGE, client_seconds=0.7)
    first.sample({"A": "decode", "B": "idle"}, now=1000.0)
    first.sample({"A": "decode", "B": "idle"}, now=1010.0)
    first.flush(force=True)

    second = _ledger(tmp_path)
    second.load()
    row = second.snapshot(now=1010.0)["hosts"][0]
    assert row["requests"] == 1
    assert row["prefill_seconds"] == 0.39
    assert row["decode"] == 10.0
    assert second.sampled_seconds == 10.0


def test_a_missing_or_broken_file_means_zeros_and_one_warning(tmp_path, caplog):
    """A first start has no file and a half-written one must not stop the
    service — zeros and exactly one warning, per start, not per host."""
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ripple"):
        missing = _ledger(tmp_path)
        missing.load()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert missing.snapshot()["hosts"][0]["requests"] == 0

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ripple"):
        ledger = _ledger(tmp_path, name="broken.json")
        ledger.load()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert ledger.snapshot()["hosts"][0]["requests"] == 0


def test_reset_zeroes_everything_and_sets_the_moment(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record("A", usage=OMLX_USAGE, client_seconds=0.7)
    ledger.sample({"A": "decode"}, now=1000.0)
    ledger.sample({"A": "decode"}, now=1010.0)
    at = ledger.reset(now=2000.0)
    snapshot = ledger.snapshot(now=2000.0)
    assert snapshot["reset_at"] == at == 2000.0
    assert snapshot["sampled_seconds"] == 0.0
    for row in snapshot["hosts"] + [snapshot["total"]]:
        assert row["requests"] == 0 and row["decode"] == 0.0
        assert row["prefill_seconds"] == 0.0 and row["client_seconds"] == 0.0
    # The file holds zeros when the call returns — a crash right after the click
    # cannot bring the old numbers back.
    on_disk = json.loads((tmp_path / "hs.json").read_text(encoding="utf-8"))
    assert on_disk["reset_at"] == 2000.0
    assert [row["requests"] for row in on_disk["hosts"]] == [0, 0]


def test_the_file_is_written_atomically(tmp_path):
    """`.tmp` + rename: a reader sees the old file or the new one, never half of
    one — the ledger is read by the next start of the service."""
    ledger = _ledger(tmp_path)
    ledger.record("A", usage=OMLX_USAGE, client_seconds=0.7)
    ledger.flush(force=True)
    assert (tmp_path / "hs.json").is_file()
    assert not (tmp_path / "hs.json.tmp").exists()


# ---------------------------------------------------------------------------
# Through the service
# ---------------------------------------------------------------------------
def _models() -> list[StubModel]:
    return [StubModel(id=MODEL, loaded=True, estimated_size=100_000_000_000)]


def _cfg(tmp_path, hosts, *, stats: StatsConfig | None = None,
         discovery: DiscoveryConfig | None = None):
    # Client auth stays off (the chat calls in these tests carry no key), the
    # admin key is set: the reset endpoint is the thing under test.
    cfg = make_config(hosts)
    cfg.auth = AuthConfig(enabled=False, keys_file=str(tmp_path / "keys.yaml"),
                          admin_key="test-admin-key")
    cfg.replication = ReplicationConfig(admin_key="omlx-admin-key")
    if stats is not None:
        cfg.stats = stats
    if discovery is not None:
        cfg.discovery = discovery
    return cfg


async def test_a_request_lands_in_the_ledger_with_what_omlx_measured(
        tmp_path, stub_factory, ripple_factory):
    """Streamed: the usage chunk oMLX sends carries the two durations, and the
    ledger takes them as they are."""
    stub = stub_factory(models=_models())
    cfg = _cfg(tmp_path, [host_config("A", stub.url, node_id=stub.state.node_id)])
    harness = await ripple_factory(cfg)
    await harness.ok(chat_body(MODEL, stream=True))

    row = (await harness.healthz())["load"]["hosts"][0]
    assert row["name"] == "A"
    assert row["requests"] == 1
    assert row["prefill_seconds"] == pytest.approx(0.02)
    assert row["generation_seconds"] == pytest.approx(0.058)
    assert row["prompt_tokens"] == 12 and row["completion_tokens"] == 3
    # `client_seconds` is RIPPLE's wall clock, rounded to milliseconds in the
    # payload: a stub on loopback answers in well under 0.5 ms, so 0.0 is a
    # correct answer, not a missing measurement. Asserting `> 0` made this test
    # fail whenever the machine was idle (measured 16.11.2026).
    assert row["client_seconds"] >= 0
    assert row["requests_without_usage"] == 0


async def test_a_response_without_the_durations_stays_unsplit(
        tmp_path, stub_factory, ripple_factory):
    """The stub's plain (non-streamed) answer carries a plain OpenAI usage — the
    tokens are counted, the split is not, and the counter says so."""
    stub = stub_factory(models=_models())
    cfg = _cfg(tmp_path, [host_config("A", stub.url, node_id=stub.state.node_id)])
    harness = await ripple_factory(cfg)
    await harness.ok(chat_body(MODEL))

    row = (await harness.healthz())["load"]["hosts"][0]
    assert row["requests"] == 1
    assert row["prompt_tokens"] == 12
    assert row["prefill_seconds"] == 0.0 and row["generation_seconds"] == 0.0
    assert row["requests_without_usage"] == 1
    assert row["client_seconds"] >= 0   # see the note above: a loopback stub can round to 0.0


async def test_the_ledger_is_in_stats_as_well_as_in_healthz(
        tmp_path, stub_factory, ripple_factory):
    """One shape, two endpoints — the page and an operator with curl must not be
    able to disagree."""
    stub = stub_factory(models=_models())
    cfg = _cfg(tmp_path, [host_config("A", stub.url, node_id=stub.state.node_id)])
    harness = await ripple_factory(cfg)
    await harness.ok(chat_body(MODEL))
    health = (await harness.healthz())["load"]
    stats = (await harness.stats())["host_stats"]
    assert stats["hosts"][0]["requests"] == health["hosts"][0]["requests"] == 1
    assert stats["started_at"] == health["started_at"]


async def test_the_sampler_reads_the_node_and_the_cross_check(
        tmp_path, stub_factory, ripple_factory):
    """One tick per `discovery.interval_seconds`: the node's own state, then its
    own SQLite count, both in `/healthz → load`."""
    stub = stub_factory(models=_models(), activity_generating=1,
                        usage_requests=431, usage_prefill_seconds=1400.5,
                        usage_generation_seconds=4700.5)
    cfg = _cfg(tmp_path, [host_config("A", stub.url, node_id=stub.state.node_id)],
               discovery=DiscoveryConfig(interval_seconds=0.5, timeout_seconds=2.0))
    harness = await ripple_factory(cfg)
    harness.ripple.start_host_stats()
    await wait_until(lambda: stub.state.count("activity") >= 3, timeout=10)

    load = (await harness.healthz())["load"]
    assert load["sampled_seconds"] > 0
    row = load["hosts"][0]
    # Two ticks observed "decode", so the interval between them is decode time.
    assert row["decode"] > 0
    assert row["idle"] == 0.0
    # The node's own count, read once and labelled with its range.
    assert load["cross"]["range"] == "today"
    assert stub.state.usage_ranges == ["today"]
    assert load["cross"]["hosts"][0]["requests"] == 431
    assert load["cross"]["hosts"][0]["prefill_seconds"] == pytest.approx(1400.5)
    assert load["cross"]["total"]["generation_seconds"] == pytest.approx(4700.5)
    assert load["cross"]["error"] is None
    # The oMLX admin key, not the client key RIPPLE passes through.
    assert set(stub.state.admin_auth) == {"Bearer omlx-admin-key"}


async def test_a_node_that_refuses_the_activity_call_stays_out(
        tmp_path, stub_factory, ripple_factory):
    """A 401 on the admin API is not `idle`: the tick is dropped, the row's
    window stays at zero — while the cross-check, which the node does answer,
    still arrives."""
    stub = stub_factory(models=_models(), activity_status=401, usage_requests=431)
    cfg = _cfg(tmp_path, [host_config("A", stub.url, node_id=stub.state.node_id)],
               discovery=DiscoveryConfig(interval_seconds=0.5, timeout_seconds=2.0))
    harness = await ripple_factory(cfg)
    harness.ripple.start_host_stats()
    await wait_until(lambda: stub.state.count("activity") >= 2, timeout=10)
    load = (await harness.healthz())["load"]
    row = load["hosts"][0]
    assert row["idle"] == 0.0 and row["sampled_seconds"] == 0.0
    assert load["cross"]["hosts"][0]["requests"] == 431


async def test_without_an_admin_key_the_sampler_does_not_even_ask(
        tmp_path, stub_factory, ripple_factory):
    """The oMLX admin key is what makes the activity call possible at all. Without
    it the sampler stays blind (one warning, not one per tick) and the cross-check
    says why it has no numbers."""
    stub = stub_factory(models=_models())
    cfg = _cfg(tmp_path, [host_config("A", stub.url, node_id=stub.state.node_id)],
               discovery=DiscoveryConfig(interval_seconds=0.5, timeout_seconds=2.0))
    cfg.replication = ReplicationConfig(admin_key="")
    harness = await ripple_factory(cfg)
    harness.ripple.start_host_stats()
    await wait_until(lambda: harness.ripple.host_stats.cross["error"] is not None,
                     timeout=10)
    assert stub.state.count("activity") == 0
    assert stub.state.count("usage") == 0
    load = (await harness.healthz())["load"]
    assert load["cross"]["error"] == "no oMLX admin key configured"
    assert load["cross"]["hosts"][0]["requests"] == 0


async def test_the_reset_endpoint_needs_the_admin_key(
        tmp_path, stub_factory, ripple_factory):
    stub = stub_factory(models=_models())
    cfg = _cfg(tmp_path, [host_config("A", stub.url, node_id=stub.state.node_id)])
    harness = await ripple_factory(cfg)
    await harness.ok(chat_body(MODEL))

    refused = await harness.client.post("/admin/stats/reset")
    assert refused.status_code == 401
    assert (await harness.healthz())["load"]["hosts"][0]["requests"] == 1

    ok = await harness.client.post("/admin/stats/reset", headers=ADMIN)
    assert ok.status_code == 200
    body = ok.json()
    assert body["reset_at"] > 0
    load = (await harness.healthz())["load"]
    assert load["reset_at"] == body["reset_at"]
    assert load["hosts"][0]["requests"] == 0
    assert load["window_seconds"] == pytest.approx(0.0, abs=5.0)


async def test_the_reset_writes_the_file_before_it_answers(
        tmp_path, stub_factory, ripple_factory):
    """The promise of the endpoint: when the call returns, the file on disk
    already holds zeros."""
    path = tmp_path / "ledger.json"
    stub = stub_factory(models=_models())
    cfg = _cfg(tmp_path, [host_config("A", stub.url, node_id=stub.state.node_id)],
               stats=StatsConfig(file=str(path), persist=True, flush_seconds=600.0))
    harness = await ripple_factory(cfg)
    await harness.ok(chat_body(MODEL))
    harness.ripple.host_stats.flush(force=True)
    assert json.loads(path.read_text(encoding="utf-8"))["hosts"][0]["requests"] == 1

    ok = await harness.client.post("/admin/stats/reset", headers=ADMIN)
    assert ok.status_code == 200
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["hosts"][0]["requests"] == 0
    assert on_disk["reset_at"] == pytest.approx(ok.json()["reset_at"])


def test_the_config_reads_the_stats_block(tmp_path):
    """A new block, documented in config.example.yaml — and every default works
    for an existing file, so nothing has to be added to upgrade."""
    from ripple.config import load_config
    path = tmp_path / "config.yaml"
    path.write_text(
        "hosts:\n  - name: A\n    addresses: [http://127.0.0.1:1]\n"
        "stats:\n  file: ~/.ripple/other.json\n  persist: false\n  flush_seconds: 90\n",
        encoding="utf-8")
    cfg = load_config(path)
    assert cfg.stats.file == "~/.ripple/other.json"
    assert cfg.stats.persist is False
    assert cfg.stats.flush_seconds == 90.0
    assert cfg.summary()["stats"]["persist"] is False

    path.write_text("hosts:\n  - name: A\n    addresses: [http://127.0.0.1:1]\n",
                    encoding="utf-8")
    defaults = load_config(path).stats
    assert defaults.file == StatsConfig().file
    assert defaults.persist is True
    assert defaults.flush_seconds == 30.0
