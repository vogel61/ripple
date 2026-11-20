"""The model lock: protect a model's residency from eviction (issue #21).

Three unload paths exist and each of them is tested here for the same promise:

* a **dispatch** that would make oMLX evict the locked model — the request is
  offered to the other host first and only refused when no host is left, with
  `503 model_locked` naming the model and the host it protects;
* a **calibration** on that node — 409, before anything is loaded or unloaded;
* a **replication** to that node — 409, before anything is probed or copied,
  because the reload at the end of the job empties the node.

The lock is operator state: it survives a restart, needs the admin key and lives
in the same file as the model policy (`~/.ripple/models.yaml`), because it is the
same kind of decision — and because one file is what the operator has to look at.

Everything runs against stub oMLX instances; nothing here loads a real model.
"""

from __future__ import annotations

import stat
import uuid

from ripple.config import (LoadingConfig, PlacementConfig, ReplicationConfig,
                           ReplicationHostConfig)
from ripple.model_policy import ModelPolicy, load, save

from tests.support import (CEILING, DEEPSEEK_SIZE, QWEN_SIZE, StubModel, bearer,
                           chat_body, host_config, keyed_config, prompt_for)

ADMIN = bearer("test-admin-key")
# The M5's two models (FA-18), the pair the eviction forecast was written for.
DEEPSEEK = "DeepSeek-V4.1-Flash-1017-oQ4e-mtp"
QWEN = "Qwen3.8-Flash-Next-oQ4e-mtp"
# oMLX infrastructure: loaded on every node, 0 GB, in no allow-list. Resident
# everywhere, dispatched nowhere — the model a lock can never protect.
INFRA = "MarkItDown"
SOURCE_ROOT = "/Users/operator/.omlx/models"
TARGET_ROOT = "/Volumes/models"


# ---------------------------------------------------------------------------
# Fixtures: a two-node fleet with DeepSeek resident on A
# ---------------------------------------------------------------------------
def _cfg(tmp_path, hosts, *, samples: int = 2):
    """Admin key on, every operator store under `tmp_path`.

    A test must never read `~/.ripple/*`: the live lock would refuse a request the
    test expects to be served, and a test that writes there would refuse the
    orchestrator's own requests.
    """
    cfg = keyed_config(hosts, str(tmp_path / "keys.yaml"))
    cfg.placement = PlacementConfig(
        model_policy_file=str(tmp_path / "models.yaml"),
        maintenance_file=str(tmp_path / "maintenance.yaml"),
        loading=LoadingConfig(calibration_file=str(tmp_path / "calibration.yaml"),
                              samples=samples, max_seconds=30.0))
    return cfg


def _deepseek(*, loaded: bool, last_access: float = 1_000.0) -> StubModel:
    return StubModel(id=DEEPSEEK, loaded=loaded, estimated_size=DEEPSEEK_SIZE,
                     last_access=last_access)


def _qwen() -> StubModel:
    return StubModel(id=QWEN, loaded=False, estimated_size=QWEN_SIZE)


def _fleet(stub_factory):
    """A: DeepSeek resident (422.5 GB of a 487.7 GB ceiling). B: both cold.

    A request for Qwen (103.9 GB) would push A to 526.4 GB and make oMLX evict
    DeepSeek; B can take it cold. That is exactly the situation the lock is for.
    """
    a = stub_factory(models=[_deepseek(loaded=True), _qwen()])
    b = stub_factory(models=[_deepseek(loaded=False), _qwen()])
    return a, b


def _hosts(a, b):
    return [host_config("A", a.url, node_id=a.state.node_id, weight=3),
            host_config("B", b.url, node_id=b.state.node_id, weight=2)]


def _client(harness) -> dict[str, str]:
    """A client key: the config has auth on, so a chat needs one (FA-23)."""
    return bearer(harness.ripple.keys.create(f"client-{uuid.uuid4().hex[:8]}",
                                             "interactive").key)


async def _lock(harness, model: str = DEEPSEEK, **body):
    return await harness.client.post(f"/admin/models/{model}/lock", headers=ADMIN,
                                     json=body)


async def _unlock(harness, model: str = DEEPSEEK):
    return await harness.client.delete(f"/admin/models/{model}/lock", headers=ADMIN)


async def _admin_models(harness) -> dict:
    resp = await harness.client.get("/admin/models", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _model_row(harness, model: str) -> dict:
    body = await _admin_models(harness)
    return next(row for row in body["models"] if row["id"] == model)


def _health_row(health: dict, model: str) -> dict:
    return next(row for row in health["models"] if row["id"] == model)


# ---------------------------------------------------------------------------
# The store: one lock per model, in the policy file
# ---------------------------------------------------------------------------
def test_the_lock_round_trips_through_the_policy_file(tmp_path):
    path = tmp_path / "models.yaml"
    store = ModelPolicy(path=path)
    # A lock and a rule live in the same file, at different levels: the rule
    # belongs to a (host, model) pair, the lock to the model alone.
    store.set("A", "m1", "hidden", by="dashboard", note="licence")
    store.set_lock(DEEPSEEK, by="dashboard", note="my working model")
    save(store)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert "locks:" in text and DEEPSEEK in text and "level: hidden" in text

    again = load(path)
    assert again.locked(DEEPSEEK) is True
    assert again.locked_models() == [DEEPSEEK]
    assert again.lock(DEEPSEEK).note == "my working model"
    assert again.level("A", "m1") == "hidden"
    assert len(again) == 1                     # the rule count is unchanged
    assert again.locked(QWEN) is False


def test_unlocking_is_idempotent_and_leaves_the_rules_alone(tmp_path):
    path = tmp_path / "models.yaml"
    store = ModelPolicy(path=path)
    store.set("A", "m1", "disabled")
    store.set_lock("m2")
    save(store)

    assert store.clear_lock("m2") is True
    assert store.clear_lock("m2") is False
    save(store)
    again = load(path)
    assert again.locked_models() == []
    assert again.level("A", "m1") == "disabled"
    # A fleet that never locks keeps the file it always had: no empty `locks:`.
    assert "locks:" not in path.read_text(encoding="utf-8")


def test_a_hand_written_lock_is_read_and_a_missing_file_has_none(tmp_path):
    assert load(tmp_path / "nothing.yaml").locked_models() == []

    # `locks: {m1: true}` is what a hand edit looks like; it must lock, not
    # release — the same rule the policy loader applies to an unknown level.
    path = tmp_path / "hand.yaml"
    path.write_text(f"version: 1\nmodels: {{}}\nlocks:\n  {DEEPSEEK}: true\n",
                    encoding="utf-8")
    assert load(path).locked(DEEPSEEK) is True


def test_a_corrupt_file_does_not_take_the_lock_with_it(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("locks: [this is not a mapping\n", encoding="utf-8")
    # Unreadable is "nothing locked" — the wrong way round would refuse every
    # request for the sake of a file nobody can read.
    assert load(broken).locked_models() == []


# ---------------------------------------------------------------------------
# The guard: refuse instead of evicting
# ---------------------------------------------------------------------------
async def test_a_locked_model_is_not_evicted_and_the_request_is_refused(
        stub_factory, ripple_factory, tmp_path):
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    client = _client(harness)
    assert (await _lock(harness)).status_code == 200

    # B is taken out of the pool, so A is the only host left: the request can
    # only be served by evicting DeepSeek, which is now forbidden.
    harness.host("B").maintenance = {"by": "test", "note": "only A is left"}
    resp = await harness.chat(chat_body(QWEN, prompt_for(0), turns=2), headers=client)

    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["reason"] == "model_locked"
    # The message has to name the model, the host it protects and the way out.
    assert DEEPSEEK in error["message"]
    assert " on A" in error["message"]
    assert f"DELETE /admin/models/{DEEPSEEK}/lock" in error["message"]

    # Nothing was sent to A, so nothing could have been evicted — and the model
    # is still resident there.
    assert a.state.chat_calls == 0
    assert a.state.unload_calls == []
    assert a.state.model(DEEPSEEK).loaded is True
    health = await harness.healthz()
    assert DEEPSEEK in next(h for h in health["hosts"] if h["name"] == "A")["models_loaded"]
    assert (await harness.stats())["requests_model_locked"] == 1


async def test_the_request_is_offered_to_the_other_host_first(
        stub_factory, ripple_factory, tmp_path):
    """A lock is not a refusal — it is a veto on one host. B serves it cold."""
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    client = _client(harness)
    await _lock(harness)

    resp = await harness.ok(chat_body(QWEN, prompt_for(0), turns=2), headers=client)

    assert resp.headers["x-ripple-host"] == "B"
    assert resp.headers["x-ripple-reason"] == "cold_load"
    assert a.state.chat_calls == 0 and b.state.chat_calls == 1
    assert a.state.model(DEEPSEEK).loaded is True
    assert (await harness.stats())["requests_model_locked"] == 0


async def test_unlocked_the_same_request_evicts_as_it_does_today(
        stub_factory, ripple_factory, tmp_path):
    """The other half of the acceptance: without a lock nothing changed."""
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    client = _client(harness)
    # B out of the pool, so A is the only host — the case where evicting is the
    # only way to serve Qwen.
    harness.host("B").maintenance = {"by": "test", "note": "only A is left"}

    resp = await harness.ok(chat_body(QWEN, prompt_for(0), turns=2), headers=client)

    assert resp.headers["x-ripple-host"] == "A"
    assert resp.headers["x-ripple-reason"] == "evict_idle"
    assert a.state.chat_calls == 1
    # An eviction is also a cold load of the model that asked for the memory, so
    # it is counted as one (the load supervision depends on it).
    assert (await harness.stats())["cold_load"] == 1


async def test_a_locked_model_is_still_served(stub_factory, ripple_factory, tmp_path):
    """The lock protects residency; it never stops RIPPLE from routing *to* it."""
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    client = _client(harness)
    await _lock(harness)

    resp = await harness.ok(chat_body(DEEPSEEK, prompt_for(0), turns=2), headers=client)

    assert resp.headers["x-ripple-host"] == "A"
    assert a.state.chat_calls == 1


async def test_a_lock_on_a_model_that_is_not_loaded_refuses_nothing(
        stub_factory, ripple_factory, tmp_path):
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    client = _client(harness)
    await _lock(harness, QWEN)              # locked, but resident nowhere
    harness.host("B").maintenance = {"by": "test", "note": "only A is left"}

    # A lock on a model that is not resident protects nothing: A still evicts
    # DeepSeek (unlocked) to make room for Qwen, exactly as without the lock.
    resp = await harness.ok(chat_body(QWEN, prompt_for(0), turns=2), headers=client)
    assert resp.headers["x-ripple-host"] == "A"

    row = _health_row(await harness.healthz(), QWEN)
    assert row["lock"] is True
    assert row["lock_reason"] is None        # no refusal can be in force


async def test_a_slot_eviction_is_still_allowed_when_it_is_not_the_locked_model(
        stub_factory, ripple_factory, tmp_path):
    """The lock is about models, not about every eviction: threads still yield."""
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    client = _client(harness)
    # DeepSeek is locked and resident on A; A is out of the pool, so every Qwen
    # request has to go to B, which has room for exactly one conversation.
    await _lock(harness, DEEPSEEK)
    harness.host("A").maintenance = {"by": "test", "note": "the locked node"}
    harness.host("B").cfg.max_threads = 1

    first = await harness.ok(chat_body(QWEN, prompt_for(0), turns=2), headers=client)
    assert first.headers["x-ripple-host"] == "B"
    # A finished conversation gives up its slot for the request in hand — a
    # slot is not a model, so the lock has nothing to say about it.
    second = await harness.ok(chat_body(QWEN, prompt_for(1), turns=2), headers=client)
    assert second.status_code == 200
    assert second.headers["x-ripple-reason"] == "slot_evict"
    assert (await harness.stats())["threads_evicted"] == 1


# ---------------------------------------------------------------------------
# The admin surface and /healthz — the contract #23 reads
# ---------------------------------------------------------------------------
async def test_the_endpoints_need_the_admin_key(stub_factory, ripple_factory,
                                               tmp_path):
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))

    # The lock is operator state, so it is refused without the admin key. The
    # issue asks for 403 here; the rest of /admin keeps its 401.
    assert (await harness.client.get("/admin/models")).status_code == 401
    assert (await harness.client.post(f"/admin/models/{DEEPSEEK}/lock")).status_code == 403
    assert (await harness.client.delete(f"/admin/models/{DEEPSEEK}/lock")).status_code == 403
    assert (await harness.client.post(f"/admin/models/{DEEPSEEK}/lock",
                                      headers=bearer("wrong"))).status_code == 403
    assert harness.ripple.policy.locked_models() == []      # nothing was locked


async def test_the_admin_view_reports_the_lock_per_model(stub_factory,
                                                        ripple_factory, tmp_path):
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    await _lock(harness, note="the operator's session")

    body = await _admin_models(harness)
    assert body["locks"] == [DEEPSEEK]
    row = next(r for r in body["models"] if r["id"] == DEEPSEEK)
    assert row["lock"] is True
    assert row["by"] == "dashboard" and row["note"] == "the operator's session"
    assert row["loaded_on"] == ["A"]
    assert row["lock_reason"] is not None and "A" in row["lock_reason"]
    assert row["hosts"] == [
        {"host": "A", "offered": True, "loaded": True, "lock": True,
         "policy": None, "dispatch": True},
        {"host": "B", "offered": True, "loaded": False, "lock": False,
         "policy": None, "dispatch": True},
    ]

    unlocked = next(r for r in body["models"] if r["id"] == QWEN)
    assert unlocked["lock"] is False and unlocked["since"] is None
    assert unlocked["lock_reason"] is None


async def test_healthz_carries_the_lock_and_the_reason(stub_factory, ripple_factory,
                                                       tmp_path):
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    await _lock(harness)

    health = await harness.healthz()
    assert health["locks"] == [DEEPSEEK]
    row = _health_row(health, DEEPSEEK)
    assert row["lock"] is True
    assert "locked" in row["lock_reason"] and DEEPSEEK in row["lock_reason"]
    # Per host: the padlock marks the node whose copy is protected.
    per_host = {r["host"]: r["lock"] for r in row["hosts"]}
    assert per_host == {"A": True, "B": False}
    assert _health_row(health, QWEN)["lock"] is False

    # Unlocking removes both the flag and the reason.
    await _unlock(harness)
    health = await harness.healthz()
    assert health["locks"] == []
    assert _health_row(health, DEEPSEEK)["lock"] is False
    assert _health_row(health, DEEPSEEK)["lock_reason"] is None


async def test_a_model_id_with_a_slash_is_addressable(stub_factory, ripple_factory,
                                                      tmp_path):
    a = stub_factory(models=[StubModel(id="org/m1", loaded=True,
                                       estimated_size=10_000_000_000)])
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url,
                                                              node_id=a.state.node_id)]))
    resp = await harness.client.post("/admin/models/org/m1/lock", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert harness.ripple.policy.locked("org/m1") is True
    assert (await _unlock(harness, "org/m1")).status_code == 200
    assert harness.ripple.policy.locked("org/m1") is False


# ---------------------------------------------------------------------------
# Models RIPPLE never routes to: no lock, no claim
# ---------------------------------------------------------------------------
def _infra_stub(stub_factory):
    """oMLX infrastructure as the fleet sees it: resident, 0 GB, never routed.

    MarkItDown is loaded on every node and in no allow-list, so `allowed: false`
    and `dispatch: false` everywhere — the model no request is ever aimed at.
    """
    return stub_factory(models=[_deepseek(loaded=True),
                                StubModel(id=INFRA, loaded=True, estimated_size=0,
                                          actual_size=0)])


def _infra_hosts(stub):
    """One node that lists the infrastructure model and dispatches only DeepSeek."""
    return [host_config("A", stub.url, node_id=stub.state.node_id,
                        models=[DEEPSEEK])]


async def test_a_lock_on_a_model_no_host_dispatches_is_refused(
        stub_factory, ripple_factory, tmp_path):
    """Measured before the fix: `POST /admin/models/MarkItDown/lock` → 200, and
    the store carried a lock whose `lock_reason` promised a 503 for a model that
    no request is ever routed to. The write is refused instead — and the store
    is not touched, so there is nothing to unlock afterwards."""
    stub = _infra_stub(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _infra_hosts(stub)))

    # The premise: resident, and not dispatchable on any node.
    health = await harness.healthz()
    row = _health_row(health, INFRA)
    assert [h["loaded"] for h in row["hosts"]] == [True]
    assert [h["dispatch"] for h in row["hosts"]] == [False]

    resp = await _lock(harness, INFRA)

    assert resp.status_code == 409, resp.text
    message = resp.json()["error"]["message"]
    assert INFRA in message
    assert "dispatchable on no host" in message
    assert resp.json()["error"]["code"] == "lock_not_dispatchable"
    # Nothing in memory, nothing in the file, nothing in the views.
    assert harness.ripple.policy.locked(INFRA) is False
    assert harness.ripple.policy.locked_models() == []
    assert (await harness.healthz())["locks"] == []
    store = tmp_path / "models.yaml"
    assert not store.exists() or "locks:" not in store.read_text(encoding="utf-8")

    # A model that *is* routed can still be locked — the refusal is about the
    # model, not about the endpoint.
    assert (await _lock(harness, DEEPSEEK)).status_code == 200
    assert harness.ripple.policy.locked_models() == [DEEPSEEK]


async def test_a_stored_lock_on_an_infrastructure_model_claims_no_refusal(
        stub_factory, ripple_factory, tmp_path):
    """A lock written before the fix stays in the store (it is operator state),
    but it must not go on claiming that something is refused: it protects a
    model nothing is ever sent to. `DELETE` still works, so the stale lock is
    not stuck."""
    (tmp_path / "models.yaml").write_text(
        f"version: 1\nmodels: {{}}\nlocks:\n  {INFRA}: true\n", encoding="utf-8")
    stub = _infra_stub(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _infra_hosts(stub)))

    row = _health_row(await harness.healthz(), INFRA)
    assert row["lock"] is True            # the store says so, and it is reported
    assert row["lock_reason"] is None     # ... but no refusal can be in force
    assert [h["loaded"] for h in row["hosts"]] == [True]   # resident, still no 503

    admin = next(r for r in (await _admin_models(harness))["models"] if r["id"] == INFRA)
    assert admin["lock"] is True and admin["lock_reason"] is None
    assert admin["loaded_on"] == ["A"]    # resident — and that is all it says

    # Releasing it must stay possible: otherwise the stale lock would be stuck.
    assert (await _unlock(harness, INFRA)).status_code == 200
    assert harness.ripple.policy.locked(INFRA) is False


async def test_the_lock_survives_a_restart(stub_factory, ripple_factory, tmp_path):
    a, b = _fleet(stub_factory)
    cfg = _cfg(tmp_path, _hosts(a, b))
    harness = await ripple_factory(cfg)
    await _lock(harness, note="do not evict")

    # A deploy restarts RIPPLE while the model is locked: it stays locked, and
    # the refusal is in force from the first request on — not from the first
    # dashboard click after the restart.
    again = await ripple_factory(cfg)
    client = _client(again)
    assert again.ripple.policy.locked(DEEPSEEK) is True
    assert again.ripple.policy.lock(DEEPSEEK).note == "do not evict"
    assert again.host("A").locked_loaded() == [DEEPSEEK]

    again.host("B").maintenance = {"by": "test", "note": "only A is left"}
    resp = await again.chat(chat_body(QWEN, prompt_for(0), turns=2), headers=client)
    assert resp.status_code == 503
    assert resp.json()["error"]["reason"] == "model_locked"
    assert a.state.model(DEEPSEEK).loaded is True


# ---------------------------------------------------------------------------
# The other two unload paths: calibration and replication
# ---------------------------------------------------------------------------
async def test_a_calibration_refuses_while_a_locked_model_is_loaded(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=[StubModel(id="small", loaded=True,
                                       estimated_size=40_000_000_000),
                             StubModel(id="big", estimated_size=400_000_000_000)])
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url,
                                                              node_id=a.state.node_id)]))
    await _lock(harness, "small")

    resp = await harness.client.post("/admin/nodes/A/calibrate", headers=ADMIN)

    assert resp.status_code == 409, resp.text
    message = resp.json()["error"]["message"]
    assert "small" in message and "A" in message and "locked" in message
    # Nothing was loaded, unloaded or measured: the refusal comes before the job.
    assert a.state.load_calls == [] and a.state.unload_calls == []
    assert (await harness.stats())["calibrations_started"] == 0
    assert a.state.model("small").loaded is True

    # `force=1` is about bound threads, not about a model's residency.
    forced = await harness.client.post("/admin/nodes/A/calibrate?force=1",
                                       headers=ADMIN)
    assert forced.status_code == 409
    assert "force" in forced.json()["error"]["message"]
    assert a.state.load_calls == []

    # Without the lock the same node is calibratable (the plan itself is fine).
    await _unlock(harness, "small")
    allowed = await harness.client.post("/admin/nodes/A/calibrate", headers=ADMIN)
    assert allowed.status_code == 202, allowed.text


async def test_a_replication_refuses_while_a_locked_model_is_loaded_on_the_target(
        stub_factory, ripple_factory, tmp_path):
    a, b = _fleet(stub_factory)
    hosts = [
        host_config("A", a.url, node_id=a.state.node_id,
                    replication=ReplicationHostConfig(address="192.0.2.11",
                                                      ssh_user="vogel",
                                                      model_roots=[SOURCE_ROOT])),
        host_config("B", b.url, node_id=b.state.node_id,
                    replication=ReplicationHostConfig(address="203.0.113.2",
                                                      ssh_user="vogel",
                                                      model_roots=[TARGET_ROOT])),
    ]
    cfg = _cfg(tmp_path, hosts)
    cfg.replication = ReplicationConfig(enabled=True, admin_key="omlx-key")
    harness = await ripple_factory(cfg)
    touched: list[str] = []

    def factory(_cfg):
        touched.append("built")
        raise AssertionError("the transport must not be built for a refused plan")

    harness.ripple.replication_factory = factory
    # B holds the locked model (it is resident there in this fleet's B stub).
    b.state.model(DEEPSEEK).loaded = True
    harness.host("B").models[DEEPSEEK].loaded = True
    await _lock(harness)

    resp = await harness.client.post("/admin/nodes/B/replicate",
                                     json={"source": "A", "model": QWEN},
                                     headers=ADMIN)

    assert resp.status_code == 409, resp.text
    message = resp.json()["error"]["message"]
    assert DEEPSEEK in message and "B" in message and "locked" in message
    assert touched == []                     # nothing was probed or copied
    assert resp.json()["plan"]["ok"] is False
    assert b.state.model(DEEPSEEK).loaded is True

    # The dry run refuses with the same sentence, so the dialog cannot promise
    # a copy the POST would refuse.
    plan = await harness.client.get(
        f"/admin/replication/plan?source=A&target=B&model={QWEN}", headers=ADMIN)
    assert plan.status_code == 200
    assert any(DEEPSEEK in e for e in plan.json()["errors"])


async def test_a_replication_to_a_node_without_the_locked_model_is_allowed(
        stub_factory, ripple_factory, tmp_path):
    """The lock protects one node's residency, not the whole feature."""
    a, b = _fleet(stub_factory)
    hosts = [
        host_config("A", a.url, node_id=a.state.node_id,
                    replication=ReplicationHostConfig(address="192.0.2.11",
                                                      ssh_user="vogel",
                                                      model_roots=[SOURCE_ROOT])),
        host_config("B", b.url, node_id=b.state.node_id,
                    replication=ReplicationHostConfig(address="203.0.113.2",
                                                      ssh_user="vogel",
                                                      model_roots=[TARGET_ROOT])),
    ]
    cfg = _cfg(tmp_path, hosts)
    cfg.replication = ReplicationConfig(enabled=True, admin_key="omlx-key")
    harness = await ripple_factory(cfg)
    await _lock(harness)                     # DeepSeek is resident on A, not on B

    plan = await harness.client.get(
        f"/admin/replication/plan?source=A&target=B&model={QWEN}", headers=ADMIN)
    errors = " | ".join(plan.json()["errors"])
    assert "locked" not in errors


# ---------------------------------------------------------------------------
# The ceiling the forecast is built on is the one the stub reports
# ---------------------------------------------------------------------------
async def test_the_forecast_uses_the_stub_ceiling(stub_factory, ripple_factory,
                                                  tmp_path):
    """Guard against a test that passes because nothing was forecast at all."""
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    health = await harness.healthz()
    row = next(h for h in health["hosts"] if h["name"] == "A")
    assert row["ceiling_bytes"] == CEILING
    assert row["guard_enabled"] is True
    assert health["capabilities"]["eviction_guard"] is True
    # The forecast is only meaningful with the guard on — and it is on here.
    client = _client(harness)
    await _lock(harness)
    harness.host("B").maintenance = {"by": "test"}
    resp = await harness.chat(chat_body(QWEN, prompt_for(0), turns=2), headers=client)
    assert resp.status_code == 503
    assert resp.json()["error"]["reason"] == "model_locked"
