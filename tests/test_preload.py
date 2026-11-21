"""Preload of locked models when a host becomes healthy (issue #119).

A lock promises residency; before this, nothing delivered it. After an oMLX
restart — or the end of a maintenance window — the node was healthy and empty,
so the first thread paid the cold load (65,8 s measured on the M5, ~2 min on the
M3) although the operator's intent was "this model is always there".

The rules these tests pin, one per test:

* a host that becomes healthy loads its locked models without a request,
* startup stays non-blocking while a 400 GB load runs, and the dashboard says
  `loading` with an eta meanwhile (`source: preload`),
* the preload wins an eviction, and the displaced model is reported in the log
  and in the node's model view,
* a model that does not fit is a recorded failure with a sentence, and it is not
  retried,
* the gates hold: no lock, no load; a builtin is never loaded; the allow-list and
  a maintenance window both say no.

Everything runs against the stub oMLX in `tests/support.py` — no real model is
loaded anywhere, and `fleet.await_preloads()` is the deterministic seam instead
of a `sleep` long enough for a 432 GB load.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time

import pytest

from ripple.config import AuthConfig, LoadingConfig, PlacementConfig

from tests.support import (CEILING, DEEPSEEK_SIZE, QWEN_SIZE, StubModel, bearer,
                           host_config, host_row, make_config, wait_until)

DEEPSEEK = "DeepSeek-V4.1-Flash-1017-oQ4e"
QWEN = "Qwen3.5-122B-A10B-4bit"
BIG = "Big-400B-oQ4"
INFRA = "infra-markitdown"
BIG_SIZE = 400_000_000_000


def _cfg(tmp_path, hosts, *, locked=(), flap_guard: int = 3, max_seconds: float = 30.0):
    """A config whose every operator store points into `tmp_path`.

    A lock has to be in the file *before* the process is built for the start-up
    test: that is the state an operator leaves behind (a lock survives a
    restart), and it is what the preload at start-up has to act on.
    """
    if locked:
        entries = "".join(f"  {model}: true\n" for model in locked)
        (tmp_path / "models.yaml").write_text(
            f"version: 1\nmodels: {{}}\nlocks:\n{entries}", encoding="utf-8")
    cfg = make_config(hosts)
    cfg.placement = PlacementConfig(
        model_policy_file=str(tmp_path / "models.yaml"),
        maintenance_file=str(tmp_path / "maintenance.yaml"),
        flap_guard_failures=flap_guard,
        loading=LoadingConfig(calibration_file=str(tmp_path / "calibration.yaml"),
                              max_seconds=max_seconds))
    return cfg


def _one(stub_factory, **state_kwargs):
    """One stub node, its port known before RIPPLE is built."""
    return stub_factory(**state_kwargs)


# ---------------------------------------------------------------------------
# The trigger: a healthy node loads its locked models by itself
# ---------------------------------------------------------------------------
async def test_a_locked_model_is_loaded_at_startup_without_a_request(
        stub_factory, ripple_factory, tmp_path):
    """No request, no click: the lock is the whole instruction."""
    a = _one(stub_factory, models=[StubModel(id=QWEN, estimated_size=QWEN_SIZE)],
             load_simulates=True)
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)], locked=[QWEN]))

    await harness.ripple.fleet.await_preloads(timeout=10)

    host = harness.host("A")
    assert a.state.load_calls == [QWEN]
    assert host.preloads[QWEN]["state"] == "loaded"
    assert host.preloads[QWEN]["size_gb"] == pytest.approx(QWEN_SIZE / 1e9, abs=0.01)
    assert a.state.model(QWEN).loaded is True
    assert host.loading is None                      # the state ended with the load

    row = host_row(await harness.healthz(), "A")
    assert row["models_loaded"] == [QWEN]
    assert row["preloads"][0]["model"] == QWEN and row["preloads"][0]["state"] == "loaded"

    # A node that stays healthy is not a reason to load again: one attempt per
    # healthy transition, not a loop.
    await host.refresh()
    await harness.ripple.fleet.await_preloads(timeout=10)
    assert a.state.load_calls == [QWEN]


async def test_a_host_that_becomes_healthy_loads_its_locked_models(
        stub_factory, ripple_factory, tmp_path):
    """The transition is the hook: a node that comes back gets its model back."""
    a = _one(stub_factory, models=[StubModel(id=QWEN, estimated_size=QWEN_SIZE)],
             load_simulates=True)
    # flap_guard 1: one unhealthy probe is a verdict, so the test drives the
    # transition instead of waiting for three ticks.
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)],
        flap_guard=1))
    host = harness.host("A")
    assert a.state.load_calls == []                  # not locked yet, nothing to do

    # The operator locks a cold model after the start (issue #118 made that a
    # normal state). Nothing is loaded while the node is healthy and steady —
    # the promise is delivered on the next transition, which is what #119 says.
    harness.ripple.policy.set_lock(QWEN)
    a.state.health_status = "unhealthy"
    await host.refresh()
    assert host.status == "down"

    a.state.health_status = "healthy"
    await host.refresh()
    await harness.ripple.fleet.await_preloads(timeout=10)

    assert a.state.load_calls == [QWEN]
    assert host.preloads[QWEN]["state"] == "loaded"


async def test_the_end_of_a_maintenance_window_preloads(
        stub_factory, ripple_factory, tmp_path):
    """The node was healthy all through the window, so no probe transition fires.

    The window's end is the second trigger: the operator hands the machine back
    and the locked model has to be there, without a request to warm it.
    """
    a = _one(stub_factory, models=[StubModel(id=QWEN, estimated_size=QWEN_SIZE)],
             load_simulates=True)
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    harness.ripple.maintenance.set("A", minutes=30, by="test")
    harness.ripple._apply_windows()
    harness.ripple.policy.set_lock(QWEN)
    await host.refresh()
    await harness.ripple.fleet.await_preloads(timeout=10)
    # A window is the operator's machine: loading 400 GB into it is the opposite
    # of what the window is for.
    assert a.state.load_calls == []

    harness.ripple.maintenance.clear("A")
    harness.ripple._apply_windows()
    await harness.ripple.fleet.await_preloads(timeout=10)

    assert a.state.load_calls == [QWEN]
    assert host.preloads[QWEN]["state"] == "loaded"


# ---------------------------------------------------------------------------
# Non-blocking, and visible while it runs
# ---------------------------------------------------------------------------
async def test_startup_and_the_dashboard_do_not_wait_for_the_load(
        stub_factory, ripple_factory, tmp_path):
    """`#117` was a blocked startup; a 432 GB warm load must not repeat it."""
    a = _one(stub_factory,
             models=[StubModel(id=DEEPSEEK, estimated_size=DEEPSEEK_SIZE)],
             load_simulates=True, load_delay=1.5)
    started = time.monotonic()
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)], locked=[DEEPSEEK]))
    startup = time.monotonic() - started
    assert startup < 3.0, f"startup waited {startup:.2f} s for the preload"

    host = harness.host("A")
    await wait_until(lambda: host.loading is not None, timeout=5)

    # The pass is a named background task, and it is still running: the startup
    # returned without waiting for it.
    pending = harness.ripple.fleet.preload_tasks()
    assert any(name.startswith("ripple-preload-A") for name in pending), pending

    # The page answers while the load runs, and the loading state says what it
    # is and how long it may take.
    page = await harness.client.get("/")
    assert page.status_code == 200
    row = host_row(await harness.healthz(), "A")
    assert row["state"] == "loading" and row["status"] == "loading"
    assert row["in_pool"] is True                    # loading is not a verdict
    assert row["loading"]["model"] == DEEPSEEK
    assert row["loading"]["source"] == "preload"
    assert row["loading"]["eta_seconds"] > 0
    assert row["loading"]["size_gb"] == pytest.approx(DEEPSEEK_SIZE / 1e9, abs=0.01)

    await harness.ripple.fleet.await_preloads(timeout=15)

    assert host.preloads[DEEPSEEK]["state"] == "loaded"
    assert host.loading is None
    assert a.state.model(DEEPSEEK).loaded is True


# ---------------------------------------------------------------------------
# The eviction the preload causes
# ---------------------------------------------------------------------------
async def test_a_preload_displaces_a_model_and_says_which(
        stub_factory, ripple_factory, tmp_path, caplog):
    """The lock wins the eviction — but a model that vanishes is reported."""
    a = _one(stub_factory,
             models=[StubModel(id=DEEPSEEK, loaded=True,
                               estimated_size=DEEPSEEK_SIZE, last_access=1.0),
                     StubModel(id=QWEN, estimated_size=QWEN_SIZE)],
             load_simulates=True, evict_on_load={QWEN: [DEEPSEEK]})
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)], locked=[QWEN]))

    with caplog.at_level(logging.WARNING, logger="ripple.hosts"):
        await harness.ripple.fleet.await_preloads(timeout=10)

    host = harness.host("A")
    assert host.preloads[QWEN]["state"] == "loaded"
    assert host.preloads[QWEN]["evicts"] == [DEEPSEEK]
    assert host.preloads[QWEN]["displaced"] == [DEEPSEEK]
    assert host.displaced[DEEPSEEK]["by"] == QWEN

    evictions = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith("preload_evict")]
    assert evictions and DEEPSEEK in evictions[0] and QWEN in evictions[0], evictions

    health = await harness.healthz()
    row = host_row(health, "A")
    assert row["displaced"][DEEPSEEK]["by"] == QWEN
    deepseek = next(m for m in health["models"] if m["id"] == DEEPSEEK)
    assert deepseek["hosts"][0]["loaded"] is False
    assert deepseek["hosts"][0]["evicted_by"] == QWEN


# ---------------------------------------------------------------------------
# What does not fit fails loudly, once
# ---------------------------------------------------------------------------
async def test_a_model_that_does_not_fit_is_a_recorded_failure(
        stub_factory, ripple_factory, tmp_path):
    """Two 400 GB models on one 512 GB host: the second one cannot be there.

    The failure is a state with a sentence an operator can act on, and the next
    probe tick does not try again — a retry loop against a node that cannot fit
    the model would be a load every interval for ever.
    """
    a = _one(stub_factory,
             models=[StubModel(id=DEEPSEEK, loaded=True, estimated_size=DEEPSEEK_SIZE),
                     StubModel(id=BIG, estimated_size=BIG_SIZE)],
             load_simulates=True)
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)],
        locked=[DEEPSEEK, BIG]))
    host = harness.host("A")

    await harness.ripple.fleet.await_preloads(timeout=10)

    record = host.preloads[BIG]
    assert record["state"] == "failed"
    assert record["reason"].startswith("does not fit: 400.0 GB needed")
    # The number is what is left for the model after everything evictable is
    # gone — read from the forecast, not from a constant in this test.
    assert " GB free" in record["reason"]
    assert f"{DEEPSEEK} is locked and cannot be evicted" in record["reason"]
    assert a.state.load_calls == []                  # no attempt was made
    assert host.loading is None

    await host.refresh()
    await harness.ripple.fleet.await_preloads(timeout=10)
    assert a.state.load_calls == []                  # and none on the next tick

    row = host_row(await harness.healthz(), "A")
    failed = next(p for p in row["preloads"] if p["model"] == BIG)
    assert failed["state"] == "failed" and failed["reason"] == record["reason"]


async def test_a_load_the_node_refuses_is_a_failure_and_not_a_retry(
        stub_factory, ripple_factory, tmp_path):
    """A 409 from oMLX is the answer, not an invitation to ask again."""
    a = _one(stub_factory,
             models=[StubModel(id=QWEN, estimated_size=QWEN_SIZE, load_fails=True)])
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)], locked=[QWEN]))
    host = harness.host("A")

    await harness.ripple.fleet.await_preloads(timeout=10)

    assert a.state.load_calls == [QWEN]
    assert host.preloads[QWEN]["state"] == "failed"
    assert "refused" in host.preloads[QWEN]["reason"]
    assert host.loading is None

    await host.refresh()
    await harness.ripple.fleet.await_preloads(timeout=10)
    assert a.state.load_calls == [QWEN]              # one attempt, one answer


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------
async def test_a_model_that_is_not_locked_is_not_preloaded(
        stub_factory, ripple_factory, tmp_path):
    """Residency is only promised for a locked model — everything else is a
    cold load when a request asks for it, exactly as before."""
    a = _one(stub_factory, models=[StubModel(id=QWEN, estimated_size=QWEN_SIZE)],
             load_simulates=True)
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)]))

    await harness.ripple.fleet.await_preloads(timeout=10)

    assert a.state.load_calls == []
    assert harness.host("A").preloads == {}


async def test_a_builtin_is_never_preloaded(stub_factory, ripple_factory, tmp_path):
    """A builtin has no weights and is resident without a load (issue #118).

    Even a stale entry in the policy file must not turn it into a load call: the
    `engine_type` the node reports is the source of the truth.
    """
    a = _one(stub_factory, models=[StubModel(id=INFRA, estimated_size=0,
                                             actual_size=0, engine_type="markitdown")],
             load_simulates=True)
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)], locked=[INFRA]))

    await harness.ripple.fleet.await_preloads(timeout=10)

    assert a.state.load_calls == []
    assert harness.host("A").preloads == {}


async def test_the_allow_list_gates_the_preload(stub_factory, ripple_factory, tmp_path):
    """A node that is not configured for the model does not load it, locked or not."""
    a = _one(stub_factory,
             models=[StubModel(id=QWEN, estimated_size=QWEN_SIZE),
                     StubModel(id="other", estimated_size=QWEN_SIZE)],
             load_simulates=True)
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id, models=["other"])],
        locked=[QWEN]))

    await harness.ripple.fleet.await_preloads(timeout=10)

    assert a.state.load_calls == []


async def test_a_node_that_is_already_loading_is_left_alone(
        stub_factory, ripple_factory, tmp_path):
    """The state machine already covers an external load; a preload on top of it
    would put a second load on a node that is busy loading one model."""
    a = _one(stub_factory, models=[StubModel(id=QWEN, estimated_size=QWEN_SIZE)],
             load_simulates=True)
    a.state.start_load(QWEN)                         # oMLX is already at it
    harness = await ripple_factory(_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id)], locked=[QWEN]))
    host = harness.host("A")
    await wait_until(lambda: host.loading is not None, timeout=5)

    await harness.ripple.fleet.await_preloads(timeout=10)

    assert a.state.load_calls == []
    assert host.loading["source"] == "omlx"          # the external load, untouched


# ---------------------------------------------------------------------------
# One arithmetic: the lock view and the preload agree (issue #126)
# ---------------------------------------------------------------------------
# The page paints the warning state from `GET /admin/models` *before* the click;
# the preload refuses the load at the next healthy transition. Both have to read
# the same answer, so the view carries `feasible`/`feasible_reason`/`feasible_host`
# (model) and `fits`/`fit_reason` (host) from `preload_forecast` itself. These
# tests re-derive the verdict from the forecast, so a second arithmetic — or a
# paraphrase of the sentence — fails here.
ADMIN = bearer("test-admin-key")
GLM = "GLM-5.2-oQ4"
GLM_SIZE = 418_100_000_000


def _admin_cfg(tmp_path, hosts, *, locked=()):
    """`_cfg` plus the admin key `GET /admin/models` requires."""
    cfg = _cfg(tmp_path, hosts, locked=locked)
    cfg.auth = AuthConfig(enabled=True, keys_file=str(tmp_path / "keys.yaml"),
                          admin_key="test-admin-key")
    return cfg


def _crowded(stub_factory, *, deepseek_loaded: bool):
    """One node holding DeepSeek (or not) and listing GLM, which needs 418.1 GB.

    The M5's pair: 422.5 GB resident against a 487.7 GB ceiling, so GLM cannot
    be there as long as the locked DeepSeek stays.
    """
    return _one(stub_factory,
                models=[StubModel(id=DEEPSEEK, loaded=deepseek_loaded,
                                  estimated_size=DEEPSEEK_SIZE),
                        StubModel(id=GLM, estimated_size=GLM_SIZE)])


async def test_the_lock_view_and_the_preload_read_one_arithmetic(
        stub_factory, ripple_factory, tmp_path):
    """`feasible is False` ⟺ every offering host has a reason — one payload.

    Both nodes hold the locked DeepSeek and are asked for GLM, so both refuse.
    The view's sentence is the preload's own, and the preload's recorded failure
    carries the identical string: the padlock state and the load cannot
    disagree about the same fleet.
    """
    a = _crowded(stub_factory, deepseek_loaded=True)
    b = _crowded(stub_factory, deepseek_loaded=True)
    harness = await ripple_factory(_admin_cfg(
        tmp_path, [host_config("A", a.url, node_id=a.state.node_id),
                   host_config("B", b.url, node_id=b.state.node_id)],
        locked=[DEEPSEEK, GLM]))

    # The preload runs at the healthy transition and records the refusal.
    await harness.ripple.fleet.await_preloads(timeout=10)
    recorded = harness.host("A").preloads[GLM]
    assert recorded["state"] == "failed" and recorded["reason"]

    body = (await harness.client.get("/admin/models", headers=ADMIN)).json()
    row = next(r for r in body["models"] if r["id"] == GLM)
    cells = {c["host"]: c for c in row["hosts"]}
    offering = sorted(name for name, cell in cells.items() if cell["offered"])
    reasons = {name: harness.host(name).preload_forecast(GLM)[1]
               for name in offering}

    # The right-hand side, read from the forecast and nothing else: every host
    # that offers the model refuses it.
    assert offering == ["A", "B"]
    assert all(reason is not None for reason in reasons.values())
    # ... and that is exactly `feasible: false`.
    assert row["feasible"] is False
    first = offering[0]
    assert row["feasible_host"] == first
    assert row["feasible_reason"] == reasons[first]
    # Verbatim: the same string the preload recorded for this model.
    assert row["feasible_reason"] == recorded["reason"]
    assert row["feasible_reason"].startswith("does not fit: 418.1 GB needed")
    # Per host, the same sentence that host would refuse with.
    for name in offering:
        assert cells[name]["fits"] is False
        assert cells[name]["fit_reason"] == reasons[name]

    # The other direction on a second payload: a node with room can hold it (it
    # would evict an unlocked model), so the verdict is `true` and no warning
    # sentence is produced.
    (tmp_path / "roomy").mkdir()
    idle = "Idle-50B-oQ4"
    roomy_stub = _one(stub_factory,
                      models=[StubModel(id=idle, loaded=True,
                                        estimated_size=50_000_000_000),
                              StubModel(id=GLM, estimated_size=GLM_SIZE)])
    roomy = await ripple_factory(_admin_cfg(
        tmp_path / "roomy", [host_config("C", roomy_stub.url,
                                         node_id=roomy_stub.state.node_id)]))
    assert roomy.host("C").preload_forecast(GLM) == ([idle], None)
    row = next(r for r in (await roomy.client.get("/admin/models",
                                                 headers=ADMIN)).json()["models"]
               if r["id"] == GLM)
    offering = sorted(cell["host"] for cell in row["hosts"] if cell["offered"])
    reasons = {name: roomy.host(name).preload_forecast(GLM)[1] for name in offering}
    assert not all(reason is not None for reason in reasons.values())
    assert row["feasible"] is True
    assert row["feasible_reason"] is None and row["feasible_host"] is None
    cell = next(c for c in row["hosts"] if c["host"] == "C")
    assert cell["fits"] is True and cell["fit_reason"] is None


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
def test_the_page_shows_a_failed_preload_and_a_displacement():
    """The failure has to reach the operator, not just `/healthz`.

    String level, like `tests/test_dashboard_loading.py`: the page is
    hand-written JavaScript with no build step, so the honest unit is "does the
    page carry the behaviour" — and the sentences live in the catalogs.
    """
    static = pathlib.Path(__file__).resolve().parent.parent / "ripple" / "static"
    js = (static / "dashboard.js").read_text(encoding="utf-8")
    assert "preloadLine(x)" in js
    assert "x.preloads" in js and "x.evicted_by" in js
    assert "preload-fail" in (static / "dashboard.css").read_text(encoding="utf-8")
    for code in ("en", "de"):
        cat = json.loads((static / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
        assert "{model}" in cat["fleet.preload_failed"]
        assert "{reason}" in cat["fleet.preload_failed"]
        assert "{list}" in cat["fleet.preload_displaced"]
        assert "{model}" in cat["models.evicted_by"]
