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

import logging
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
         "policy": None, "dispatch": True, "routable": True,
         # Nothing is loaded beside DeepSeek, so there is no forecast to make:
         # `fits: true` and no sentence (issue #126).
         "fits": True, "fit_reason": None},
        # Listed but cold: dispatchable (a cold load is a legitimate dispatch),
        # not routable — residency decides the availability column (#116).
        {"host": "B", "offered": True, "loaded": False, "lock": False,
         "policy": None, "dispatch": True, "routable": False,
         "fits": True, "fit_reason": None},
    ]
    # No host could forecast here (both answered `([], None)`): "nothing to
    # say", not a warning — and since every host reports `fits: true`, the
    # model-level verdict is `true`, not an unknown (issue #126).
    assert row["feasible"] is True
    assert row["feasible_reason"] is None and row["feasible_host"] is None

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
# Lockability is a property of the model, not of its routability (issue #118)
# ---------------------------------------------------------------------------
# #28 refused every write whose model no host dispatches: a lock there could
# never refuse a request, so the write was answered with `409
# lock_not_dispatchable`. That asked the wrong question. A lock says what
# happens *when* the model is resident — so it is settable on a cold model, on
# one the allow-list keeps out of routing, and on one no node lists yet (the
# operator locking ahead of a start). The one thing that cannot be locked is an
# oMLX builtin, because it always is: derived from the node's `engine_type`,
# never stored, never releasable.
UNLISTED = "Qwen3.5-122B-A10B-4bit"


async def test_a_cold_model_can_be_locked(stub_factory, ripple_factory, tmp_path):
    """A model a host lists and does not hold is lockable — the lock protects
    the memory it *would* take. `loaded_on` stays empty and `lock_reason` is
    None: a lock is not a claim that a request is being refused right now."""
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    assert a.state.model(QWEN).loaded is False and b.state.model(QWEN).loaded is False

    resp = await _lock(harness, QWEN, note="hold the slot")

    assert resp.status_code == 200, resp.text
    assert harness.ripple.policy.locked(QWEN) is True
    # the write is in the store, and the answer of the write already says so
    answered = next(r for r in resp.json()["models"] if r["id"] == QWEN)
    assert answered["lock"] is True and answered["loaded_on"] == []

    row = await _model_row(harness, QWEN)
    assert row["lock"] is True
    assert row["builtin"] is False and row["lockable"] is True
    assert row["loaded_on"] == []
    assert row["lock_reason"] is None
    assert row["since"] is not None and row["note"] == "hold the slot"
    assert [x["loaded"] for x in row["hosts"]] == [False, False]
    assert QWEN in (await _admin_models(harness))["locks"]


async def test_a_model_no_host_lists_yet_can_be_locked(stub_factory, ripple_factory,
                                                       tmp_path):
    """The case #28 refused with 409: the operator locks ahead of a start, so
    the model is not in any node's list yet. The write is stored, and
    `GET /admin/models` reports it with `offered: false` — a lock nobody can see
    is a lock nobody can release."""
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    assert all(UNLISTED not in h.models for h in harness.ripple.fleet.hosts)

    resp = await _lock(harness, UNLISTED)

    assert resp.status_code == 200, resp.text
    assert harness.ripple.policy.locked(UNLISTED) is True

    row = await _model_row(harness, UNLISTED)
    assert row["lock"] is True
    assert row["loaded_on"] == [] and row["lock_reason"] is None
    assert row["listed"] is False
    assert [x["offered"] for x in row["hosts"]] == [False, False]
    # No host offers it, so there is nothing to forecast: `null`, not a
    # warning — locking ahead of an oMLX start is a legitimate state (#126).
    assert row["feasible"] is None
    assert row["feasible_reason"] is None and row["feasible_host"] is None
    assert [x["fits"] for x in row["hosts"]] == [None, None]
    assert UNLISTED in (await _admin_models(harness))["locks"]


async def test_a_cold_model_can_be_unlocked(stub_factory, ripple_factory, tmp_path):
    """The release works on a cold model too — it is the same attribute, and a
    lock that could only be set would be a trap."""
    a, b = _fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    await _lock(harness, QWEN)

    resp = await _unlock(harness, QWEN)

    assert resp.status_code == 200, resp.text
    assert harness.ripple.policy.locked(QWEN) is False
    row = await _model_row(harness, QWEN)
    assert row["lock"] is False and row["since"] is None
    assert QWEN not in (await _admin_models(harness))["locks"]


def _infra_stub(stub_factory):
    """oMLX infrastructure as the fleet sees it: a virtual engine, resident
    without weights. `engine_type: markitdown` is oMLX's own word for it and the
    only source of the builtin flag — no id list lives in this repository."""
    return stub_factory(models=[_deepseek(loaded=True),
                                StubModel(id=INFRA, loaded=True, estimated_size=0,
                                          actual_size=0, engine_type="markitdown")])


def _infra_hosts(stub):
    """One node that lists the infrastructure model and dispatches only DeepSeek."""
    return [host_config("A", stub.url, node_id=stub.state.node_id,
                        models=[DEEPSEEK])]


async def test_an_omlx_builtin_reads_as_locked_without_a_stored_entry(
        stub_factory, ripple_factory, tmp_path):
    """MarkItDown is always locked and never an operator decision: the state is
    derived from `engine_type` on every read, so the policy file stays empty —
    a second source of truth for a state nobody can change would be a lie."""
    stub = _infra_stub(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _infra_hosts(stub)))

    # The premise: resident, and not dispatchable on any node.
    health = await harness.healthz()
    row = _health_row(health, INFRA)
    assert [h["loaded"] for h in row["hosts"]] == [True]
    assert [h["dispatch"] for h in row["hosts"]] == [False]

    # Locked in both views, and in the flat list — with no entry behind it.
    assert harness.ripple.policy.locked_models() == []
    assert row["lock"] is True and row["builtin"] is True
    assert row["lock_reason"] is None       # nothing can be refused for it
    assert INFRA in health["locks"]

    admin = await _model_row(harness, INFRA)
    assert admin["lock"] is True and admin["builtin"] is True
    assert admin["lockable"] is False
    assert admin["since"] is None and admin["by"] is None and admin["note"] == ""
    assert admin["loaded_on"] == ["A"] and admin["lock_reason"] is None
    assert INFRA in (await _admin_models(harness))["locks"]

    store = tmp_path / "models.yaml"
    assert not store.exists() or "locks:" not in store.read_text(encoding="utf-8")


async def test_an_omlx_builtin_cannot_be_locked_or_unlocked(stub_factory,
                                                            ripple_factory, tmp_path):
    """Both writes are refused with `409 lock_builtin` — not with the old
    `lock_not_dispatchable`: the refusal is about what the model *is*, and it
    says so. Nothing is written in either direction."""
    stub = _infra_stub(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _infra_hosts(stub)))

    posted = await _lock(harness, INFRA)
    assert posted.status_code == 409, posted.text
    assert posted.json()["error"]["code"] == "lock_builtin"
    assert INFRA in posted.json()["error"]["message"]
    assert "builtin" in posted.json()["error"]["message"]

    deleted = await _unlock(harness, INFRA)
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["error"]["code"] == "lock_builtin"
    assert INFRA in deleted.json()["error"]["message"]

    assert harness.ripple.policy.locked(INFRA) is False
    assert harness.ripple.policy.locked_models() == []
    store = tmp_path / "models.yaml"
    assert not store.exists() or "locks:" not in store.read_text(encoding="utf-8")

    # The refusal is about the builtin, not about the endpoint: the model the
    # same node dispatches is still lockable.
    assert (await _lock(harness, DEEPSEEK)).status_code == 200
    assert harness.ripple.policy.locked_models() == [DEEPSEEK]


async def test_a_stale_stored_entry_for_a_builtin_stays_inert(stub_factory,
                                                              ripple_factory, tmp_path):
    """An entry written before #118 is left in the file (it is operator state)
    but changes nothing: the derived state wins on every read, the entry cannot
    be released, and it claims no refusal."""
    (tmp_path / "models.yaml").write_text(
        f"version: 1\nmodels: {{}}\nlocks:\n  {INFRA}: true\n", encoding="utf-8")
    stub = _infra_stub(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _infra_hosts(stub)))

    row = _health_row(await harness.healthz(), INFRA)
    assert row["lock"] is True and row["builtin"] is True
    assert row["lock_reason"] is None      # resident, and still no 503
    assert [h["loaded"] for h in row["hosts"]] == [True]

    assert (await _unlock(harness, INFRA)).status_code == 409
    assert harness.ripple.policy.locked(INFRA) is True      # untouched


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


# ---------------------------------------------------------------------------
# The lock's arithmetic: can the promise be kept anywhere? (issue #126)
# ---------------------------------------------------------------------------
# A lock is a promise about residency, and on the M5 the numbers cannot keep
# every promise: DeepSeek is 422.5 GB resident against a 487.7 GB ceiling and
# GLM-5.2-oQ4 needs 418.1 GB, so a lock on GLM was a promise the operator heard
# about only at the next healthy transition (`preload_refused`). #126 keeps the
# write (200) and moves the verdict into the view: `feasible` per model,
# `fits` per host, computed by `preload_forecast` and nothing else.
GLM = "GLM-5.2-oQ4"
GLM_SIZE = 418_100_000_000
SMALL = "Small-10B-oQ4"
SMALL_SIZE = 10_000_000_000
IDLE = "Idle-50B-oQ4"
IDLE_SIZE = 50_000_000_000


def _crowded_fleet(stub_factory):
    """Both hosts hold DeepSeek and are asked to hold GLM as well.

    M5's arithmetic on two nodes: 422.5 GB resident against a 487.7 GB ceiling
    (target 463.3 GB at `hard_threshold: 0.95`), and GLM needs another 418.1 GB.
    Neither node can take it while the locked DeepSeek stays — the case #126 is
    about.
    """
    a = stub_factory(models=[_deepseek(loaded=True),
                             StubModel(id=GLM, estimated_size=GLM_SIZE)])
    b = stub_factory(models=[_deepseek(loaded=True),
                             StubModel(id=GLM, estimated_size=GLM_SIZE)])
    return a, b


async def test_the_view_says_per_host_whether_the_lock_can_be_delivered(
        stub_factory, ripple_factory, tmp_path):
    """`fits`/`fit_reason` per host, `feasible`/`_reason`/`_host` per model.

    The sentence is the preload's own, read from `preload_forecast` — one
    arithmetic, so the view and the failed preload cannot disagree.
    """
    a, b = _crowded_fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    # DeepSeek eats the ceiling and is the model a lock protects first; GLM is
    # the model that then fits nowhere.
    assert (await _lock(harness, DEEPSEEK)).status_code == 200
    assert (await _lock(harness, GLM)).status_code == 200

    row = await _model_row(harness, GLM)
    assert row["feasible"] is False
    assert row["feasible_host"] == "A"          # first blocking host, sorted
    assert row["feasible_reason"] == harness.host("A").preload_forecast(GLM)[1]
    assert row["feasible_reason"].startswith("does not fit: 418.1 GB needed")
    assert " GB free" in row["feasible_reason"]
    assert f"{DEEPSEEK} is locked and cannot be evicted" in row["feasible_reason"]

    cells = {c["host"]: c for c in row["hosts"]}
    assert [cells[name]["fits"] for name in ("A", "B")] == [False, False]
    for name in ("A", "B"):
        assert cells[name]["fit_reason"] == harness.host(name).preload_forecast(GLM)[1]
    # The verdict is about memory only: the lock is set and stored.
    assert row["lock"] is True and row["loaded_on"] == []


async def test_a_lock_that_fits_nowhere_is_stored_and_warns(
        stub_factory, ripple_factory, tmp_path, caplog):
    """The operator's decision is a decision: 200, a line in the log, no evict.

    #126 refuses the *warning*, not the write. Nothing is unloaded or unlocked
    to make room — the prognosis reads.
    """
    a, b = _crowded_fleet(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _hosts(a, b)))
    await _lock(harness, DEEPSEEK)

    with caplog.at_level(logging.WARNING, logger="ripple"):
        resp = await _lock(harness, GLM, note="the second big one")

    assert resp.status_code == 200, resp.text
    assert harness.ripple.policy.locked(GLM) is True
    answered = next(r for r in resp.json()["models"] if r["id"] == GLM)
    # The write answers with the whole view, so the page learns the warning
    # state from the click itself (rule 4 of #126).
    assert answered["feasible"] is False
    assert answered["feasible_host"] == "A"
    assert answered["note"] == "the second big one"
    assert "does not fit" in answered["feasible_reason"]
    assert (tmp_path / "models.yaml").read_text(encoding="utf-8").count(GLM) >= 1
    # A line the operator can find later, and no action on the nodes.
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("model_lock_infeasible")]
    assert lines and GLM in lines[0] and "does not fit" in lines[0], lines
    assert a.state.unload_calls == [] and b.state.unload_calls == []
    assert a.state.model(DEEPSEEK).loaded is True


async def test_a_builtin_is_never_infeasible(stub_factory, ripple_factory, tmp_path):
    """A virtual engine has no weights: `feasible: true`, no sentence — even on
    a node whose ceiling is already full of a locked model."""
    stub = _infra_stub(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, _infra_hosts(stub)))
    await _lock(harness, DEEPSEEK)

    row = await _model_row(harness, INFRA)
    assert row["builtin"] is True and row["lock"] is True
    assert row["feasible"] is True
    assert row["feasible_reason"] is None and row["feasible_host"] is None
    cell = next(c for c in row["hosts"] if c["host"] == "A")
    assert cell["offered"] is True and cell["fits"] is True
    assert cell["fit_reason"] is None


async def test_a_lock_that_is_only_not_routed_here_does_not_warn(
        stub_factory, ripple_factory, tmp_path):
    """Routing is the policy's axis, memory is the lock's (rule 2 of #126).

    `SMALL` is offered by the node and kept out of routing by the allow-list;
    it would fit there, so the lock is deliverable and must not warn — a model
    that is merely not routed somewhere is not a broken promise.
    """
    a = stub_factory(models=[_deepseek(loaded=True),
                             StubModel(id=IDLE, loaded=True, estimated_size=IDLE_SIZE,
                                       last_access=2.0),
                             StubModel(id=SMALL, estimated_size=SMALL_SIZE)])
    hosts = [host_config("A", a.url, node_id=a.state.node_id,
                         models=[DEEPSEEK, IDLE])]
    harness = await ripple_factory(_cfg(tmp_path, hosts))
    await _lock(harness, DEEPSEEK)
    assert (await _lock(harness, SMALL)).status_code == 200

    row = await _model_row(harness, SMALL)
    cell = next(c for c in row["hosts"] if c["host"] == "A")
    assert cell["offered"] is True and cell["dispatch"] is False   # not routed
    assert cell["fits"] is True and cell["fit_reason"] is None
    assert row["feasible"] is True          # a forecast was made, and it fits
    assert row["feasible_reason"] is None and row["feasible_host"] is None


async def test_a_host_without_a_forecast_still_fits(stub_factory, ripple_factory,
                                                   tmp_path):
    """`([], None)` is "no forecast", not "does not fit" (rule 1 of #126).

    Unknown size and a disabled guard both leave the node to decide; the host
    then reports `fits: true`, and the model-level verdict is `true` — no
    warning is invented from silence, and no unknown either.
    """
    a = stub_factory(models=[_deepseek(loaded=True),
                             StubModel(id=GLM, estimated_size=0)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=a.state.node_id)]))
    await _lock(harness, DEEPSEEK)
    await _lock(harness, GLM)

    row = await _model_row(harness, GLM)
    cell = next(c for c in row["hosts"] if c["host"] == "A")
    assert harness.host("A").preload_forecast(GLM) == ([], None)
    assert cell["fits"] is True and cell["fit_reason"] is None
    assert row["feasible"] is True and row["feasible_reason"] is None

    # The same answer with the guard off: the arithmetic is not made, so the
    # host cannot be called unfit.
    off = stub_factory(models=[_deepseek(loaded=True),
                               StubModel(id=GLM, estimated_size=GLM_SIZE)])
    off.state.final_ceiling = 0               # the node reports no ceiling
    guardless = await ripple_factory(_cfg(tmp_path, [
        host_config("A", off.url, node_id=off.state.node_id)]))
    assert guardless.host("A").guard_enabled is False
    assert guardless.host("A").preload_forecast(GLM) == ([], None)
    row = await _model_row(guardless, GLM)
    cell = next(c for c in row["hosts"] if c["host"] == "A")
    assert cell["fits"] is True and cell["fit_reason"] is None
    assert row["feasible"] is True
