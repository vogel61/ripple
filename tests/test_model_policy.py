"""Model policy: switching a (host, model) pair off (issue #11).

Two wishes hide behind "make a model unavailable": *do not route it here any
more* (`disabled`) and *do not offer it to clients at all* (`hidden`). Both are
RIPPLE state only — nothing is loaded, unloaded or patched — and both have to
survive the restart that a deploy causes, because a model switched off for a
reason must not come back on its own.
"""

from __future__ import annotations

import stat

import pytest

from ripple.config import PlacementConfig
from ripple.model_policy import LEVELS, ModelPolicy, load, save
from ripple.placement import admit

from tests.support import (StubModel, bearer, chat_body, host_config, keyed_config)

ADMIN = bearer("test-admin-key")
MODEL = "m1"


def _cfg(tmp_path, hosts):
    cfg = keyed_config(hosts, str(tmp_path / "keys.yaml"))
    cfg.placement = PlacementConfig(maintenance_file=str(tmp_path / "maintenance.yaml"),
                                    model_policy_file=str(tmp_path / "models.yaml"))
    return cfg


def _models(*ids: str) -> list[StubModel]:
    return [StubModel(id=mid, estimated_size=100_000_000_000) for mid in ids]


async def _set(harness, model: str = MODEL, node: str = "A",
               level: str = "disabled", **params):
    return await harness.client.post(f"/admin/nodes/{node}/models/{model}",
                                     headers=ADMIN, params={"level": level, **params})


async def _clear(harness, model: str = MODEL, node: str = "A"):
    return await harness.client.delete(f"/admin/nodes/{node}/models/{model}",
                                       headers=ADMIN)


async def _listed(harness, headers: dict[str, str] | None = None) -> list[str]:
    resp = await harness.client.get("/v1/models", headers=headers or {})
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()["data"]]


async def _row(harness, node: str, model: str = MODEL) -> dict:
    resp = await harness.client.get(f"/admin/nodes/{node}/models", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return next(row for row in resp.json()["models"] if row["id"] == model)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
def test_the_store_round_trips_through_the_file(tmp_path):
    path = tmp_path / "models.yaml"
    store = ModelPolicy(path=path)
    store.set("A", "org/m1", "hidden", by="dashboard", note="licence")
    store.set("B", "m2", "disabled", by="dashboard")
    save(store)

    # Operator data next to a running proxy: 0600, like the maintenance store.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert "org/m1" in text and "level: hidden" in text

    again = load(path)
    assert len(again) == 2
    assert again.level("A", "org/m1") == "hidden"
    assert again.rule("A", "org/m1").note == "licence"
    assert again.level("B", "m2") == "disabled"
    assert again.level("A", "m2") is None


def test_both_levels_block_the_dispatch_but_only_hidden_hides(tmp_path):
    store = ModelPolicy(path=tmp_path / "models.yaml")
    store.set("A", "m1", "disabled")
    store.set("B", "m1", "hidden")

    assert store.blocks("A", "m1") and not store.hides("A", "m1")
    assert store.blocks("B", "m1") and store.hides("B", "m1")
    assert not store.blocks("C", "m1")
    assert store.clear("A", "m1") is True
    assert store.clear("A", "m1") is False          # idempotent
    assert not store.blocks("A", "m1")


def test_the_levels_are_the_two_the_issue_names():
    assert LEVELS == ("disabled", "hidden")
    with pytest.raises(ValueError):
        ModelPolicy().set("A", "m1", "off")


def test_a_missing_or_corrupt_file_means_nothing_is_switched_off(tmp_path):
    assert len(load(tmp_path / "nothing.yaml")) == 0

    broken = tmp_path / "broken.yaml"
    broken.write_text("models: [this is not a mapping\n", encoding="utf-8")
    # A corrupt file must never take the whole model list down: starting with
    # every model switched off would be the expensive way round.
    assert len(load(broken)) == 0

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text("version: 1\nmodels:\n  A:\n    m1:\n      level: paused\n",
                       encoding="utf-8")
    assert len(load(unknown)) == 0


# ---------------------------------------------------------------------------
# disabled — no dispatch, the model stays in /v1/models
# ---------------------------------------------------------------------------
async def test_disabled_stops_the_dispatch_but_keeps_the_model_listed(
        stub_factory, ripple_factory, tmp_path):
    a, b = stub_factory(models=_models(MODEL)), stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)

    resp = await _set(harness, node="A", level="disabled")
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["models"] if r["id"] == MODEL)
    assert row["level"] == "disabled"
    assert row["listed"] is True          # clients keep seeing it
    assert row["dispatch"] is False       # RIPPLE stops sending it there

    for i in range(3):
        answer = await harness.ok(chat_body(MODEL, f"fresh question {i} " * 20, turns=2),
                                  headers=client)
        assert answer.headers["x-ripple-host"] == "B"
    assert a.state.chat_calls == 0
    assert b.state.chat_calls == 3
    assert await _listed(harness, client) == [MODEL]


async def test_disabled_on_every_host_is_a_503_with_a_reason(
        stub_factory, ripple_factory, tmp_path):
    a, b = stub_factory(models=_models(MODEL)), stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    await _set(harness, node="A")
    await _set(harness, node="B")

    resp = await harness.chat(chat_body(MODEL, "hello", turns=2), headers=client)

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["reason"] == "model_disabled"
    assert "switched off" in error["message"]
    assert "A=disabled" in error["message"] and "B=disabled" in error["message"]
    # The message has to say how to undo it, not only that it is off.
    assert "DELETE /admin/nodes/" in error["message"]
    assert a.state.chat_bodies == [] and b.state.chat_bodies == []
    # A model that no host would serve is not advertised either: the list must
    # not promise what every request answers with a 503. `disabled` keeps the
    # model listed only as long as another host offers it.
    assert await _listed(harness, client) == []


async def test_disabled_on_the_only_host_that_offers_it_is_not_advertised(
        stub_factory, ripple_factory, tmp_path):
    """`disabled` keeps a model listed only while *another* host offers it."""
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    await _set(harness, node="A", level="disabled")

    assert await _listed(harness, client) == []
    resp = await harness.chat(chat_body(MODEL, "hello", turns=2), headers=client)
    assert resp.status_code == 503
    assert resp.json()["error"]["reason"] == "model_disabled"
    assert a.state.chat_bodies == []


# ---------------------------------------------------------------------------
# hidden — gone from /v1/models once every host hides it
# ---------------------------------------------------------------------------
async def test_hidden_on_one_host_still_lists_and_serves_from_the_other(
        stub_factory, ripple_factory, tmp_path):
    a, b = stub_factory(models=_models(MODEL)), stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    await _set(harness, node="A", level="hidden")

    assert await _listed(harness, client) == [MODEL]     # B still offers it
    answer = await harness.ok(chat_body(MODEL, "hello there " * 20, turns=2),
                              headers=client)
    assert answer.headers["x-ripple-host"] == "B"
    assert a.state.chat_calls == 0


async def test_hidden_on_every_host_removes_it_and_answers_503(
        stub_factory, ripple_factory, tmp_path):
    a, b = stub_factory(models=_models(MODEL)), stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    await _set(harness, node="A", level="hidden")
    await _set(harness, node="B", level="hidden")

    # A 404 from /v1/models' point of view: the model is simply not there.
    assert await _listed(harness, client) == []

    # …and a 503 with a reason a human can act on when it is called anyway.
    resp = await harness.chat(chat_body(MODEL, "hello", turns=2), headers=client)
    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["reason"] == "model_hidden"
    assert "hidden" in error["message"]
    assert "A=hidden" in error["message"] and "B=hidden" in error["message"]
    assert a.state.chat_bodies == [] and b.state.chat_bodies == []
    assert (await harness.stats())["http_503_total"] >= 1


async def test_clearing_the_rule_puts_the_model_back(stub_factory, ripple_factory,
                                                    tmp_path):
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    await _set(harness, node="A", level="hidden")
    assert await _listed(harness, client) == []

    resp = await _clear(harness, node="A")
    assert resp.status_code == 200
    row = next(r for r in resp.json()["models"] if r["id"] == MODEL)
    assert row["level"] is None and row["listed"] is True and row["dispatch"] is True
    assert await _listed(harness, client) == [MODEL]
    answer = await harness.ok(chat_body(MODEL, "hello", turns=2), headers=client)
    assert answer.headers["x-ripple-host"] == "A"


# ---------------------------------------------------------------------------
# Persistence — operator state, written only by the endpoints
# ---------------------------------------------------------------------------
async def test_both_levels_survive_a_restart(stub_factory, ripple_factory, tmp_path):
    a, b = stub_factory(models=_models(MODEL)), stub_factory(models=_models(MODEL))
    cfg = _cfg(tmp_path, [host_config("A", a.url), host_config("B", b.url)])
    harness = await ripple_factory(cfg)
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    await _set(harness, node="A", level="disabled", note="broken quant")
    await _set(harness, node="B", level="hidden", note="licence")

    # A deploy restarts RIPPLE while the model is switched off: it stays off.
    again = await ripple_factory(cfg)
    rule_a = again.ripple.policy.rule("A", MODEL)
    assert rule_a is not None
    assert rule_a.level == "disabled" and rule_a.note == "broken quant"
    assert again.ripple.policy.level("B", MODEL) == "hidden"
    assert again.host("A").model_available(MODEL) is False
    assert again.host("B").model_available(MODEL) is False
    # `disabled` still offers it to clients, `hidden` does not — but nothing can
    # serve it any more, so the model is not advertised at all.
    assert await _listed(again, client) == []
    assert (await again.chat(chat_body(MODEL, "hello", turns=2),
                             headers=client)).status_code == 503


async def test_the_balancer_never_writes_the_file(stub_factory, ripple_factory,
                                                  tmp_path):
    a = stub_factory(models=_models(MODEL))
    cfg = _cfg(tmp_path, [host_config("A", a.url)])
    path = tmp_path / "models.yaml"
    harness = await ripple_factory(cfg)
    client = bearer(harness.ripple.keys.create("client", "interactive").key)

    await harness.ok(chat_body(MODEL, "a question " * 20, turns=2), headers=client)
    await _listed(harness, client)
    assert not path.exists()          # placement is a reader, not a writer

    await _set(harness, node="A")
    before = path.read_text(encoding="utf-8")
    await _clear(harness, node="A")
    await _set(harness, node="A")
    after = path.read_text(encoding="utf-8")
    assert before == after            # only the operator endpoint changed it
    assert "level: disabled" in after


# ---------------------------------------------------------------------------
# A bound conversation survives the switch
# ---------------------------------------------------------------------------
async def test_a_bound_thread_falls_back_when_its_host_is_disabled(
        stub_factory, ripple_factory, tmp_path):
    a, b = stub_factory(models=_models(MODEL)), stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    # B is switched off first, so the conversation has to start on A.
    await _set(harness, node="B")
    first = await harness.ok(chat_body(MODEL, "opening question " * 20, turns=2),
                             headers=client)
    assert first.headers["x-ripple-host"] == "A"
    thread = first.headers["x-ripple-thread"]

    # Now the roles swap: A is switched off for this model, B is back.
    await _clear(harness, node="B")
    await _set(harness, node="A")

    second = await harness.ok(chat_body(MODEL, "opening question " * 20, turns=3),
                              headers=client)
    assert second.headers["x-ripple-host"] == "B"
    assert second.headers["x-ripple-thread"] == thread      # same session
    assert second.headers["x-ripple-reason"] == "fallback"


async def test_admission_refuses_a_disabled_model_and_says_which(
        stub_factory, ripple_factory, tmp_path):
    a, b = stub_factory(models=_models(MODEL)), stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    assert admit(harness.ripple.fleet, harness.host("A"), MODEL) == (True, None)

    await _set(harness, node="A", level="hidden")
    ok, why = admit(harness.ripple.fleet, harness.host("A"), MODEL)
    assert ok is False
    assert why == f"model {MODEL!r} is hidden on A"


# ---------------------------------------------------------------------------
# The admin surface
# ---------------------------------------------------------------------------
async def test_the_endpoints_need_the_admin_key_and_know_their_nodes(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    url = f"/admin/nodes/A/models/{MODEL}"

    assert (await harness.client.get("/admin/nodes/A/models")).status_code == 401
    assert (await harness.client.post(url)).status_code == 401
    assert (await harness.client.delete(url)).status_code == 401
    assert len(harness.ripple.policy) == 0          # a denied call changed nothing

    assert (await harness.client.get("/admin/nodes/A/models",
                                     headers=bearer("wrong"))).status_code == 401
    for verb in ("get", "post", "delete"):
        path = "/admin/nodes/NOPE/models" + ("" if verb == "get" else f"/{MODEL}")
        assert (await getattr(harness.client, verb)(path, headers=ADMIN)).status_code == 404

    bad = await _set(harness, node="A", level="off")
    assert bad.status_code == 400
    assert len(harness.ripple.policy) == 0


async def test_the_admin_view_reports_four_different_answers(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    await _set(harness, node="A", level="hidden", note="licence")

    resp = await harness.client.get("/admin/nodes/A/models", headers=ADMIN)
    body = resp.json()
    assert body["node"] == "A"
    assert body["levels"] == list(LEVELS)
    row = next(r for r in body["models"] if r["id"] == MODEL)
    assert row == {
        "id": MODEL, "level": "hidden", "since": row["since"], "by": "dashboard",
        "note": "licence", "offered": True, "allowed": True, "listed": False,
        "dispatch": False,
    }

    # /healthz carries the same decision, so the dashboard and the soak report
    # see it without a second call.
    health = await harness.healthz()
    host_row = next(h for h in health["models"] if h["id"] == MODEL)["hosts"][0]
    assert host_row["policy"] == "hidden" and host_row["dispatch"] is False


async def test_a_rule_for_a_model_that_is_not_listed_is_still_reported(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    await _set(harness, model="m9", node="A", level="disabled")

    row = await _row(harness, "A", "m9")
    assert row["offered"] is False and row["level"] == "disabled"


async def test_a_model_id_with_a_slash_is_addressable(stub_factory, ripple_factory,
                                                      tmp_path):
    a = stub_factory(models=_models("org/m1"))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)

    resp = await _set(harness, model="org/m1", node="A", level="hidden")
    assert resp.status_code == 200, resp.text
    assert harness.ripple.policy.level("A", "org/m1") == "hidden"
    assert await _listed(harness, client) == []
    assert (await _clear(harness, model="org/m1", node="A")).status_code == 200
    assert await _listed(harness, client) == ["org/m1"]


async def test_the_level_may_arrive_in_the_body(stub_factory, ripple_factory, tmp_path):
    """A dashboard toggle posts JSON; the maintenance routes take query params."""
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    resp = await harness.client.post(f"/admin/nodes/A/models/{MODEL}", headers=ADMIN,
                                     json={"level": "hidden", "note": "from the GUI"})
    assert resp.status_code == 200, resp.text
    assert harness.ripple.policy.level("A", MODEL) == "hidden"
    assert harness.ripple.policy.rule("A", MODEL).note == "from the GUI"


async def test_the_entry_host_fallback_does_not_leak_a_hidden_model(
        stub_factory, ripple_factory, tmp_path):
    """`/v1/models` falls back to the entry host's raw list when nothing is known.

    That list is oMLX's, not RIPPLE's — a model the operator hid must not come
    back through it.
    """
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    await _set(harness, node="A", level="hidden")
    harness.host("A").models.clear()        # nothing discovered yet

    assert await _listed(harness, client) == []
