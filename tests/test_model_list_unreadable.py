"""A node whose model list cannot be read (issue #53).

The distinction the whole file is about:

* the node answered and the model is not in its list → the model does not
  exist for this fleet → 404 `unknown_model`, a permanent answer;
* the node never told us what it carries (a 401 from a missing oMLX admin key,
  a timeout) → the model may well be there → 503 `model_list_unavailable`,
  which a client may retry once the operator fixed the key.

The measured defect: both nodes healthy, `models_available: 0` on both, and a
request for a model the nodes do carry was answered with
`reason=unknown_model status=404 note=model '…' is not offered by any host`.
"""

from __future__ import annotations

from tests.support import (chat_body, host_config, host_row, make_config,
                           prompt_for, stub_model_loaded)


async def test_a_node_that_never_read_its_list_answers_503_not_404(
        stub_factory, ripple_factory):
    """The defect itself: `/health` open, `/v1/models` 401 — no list, no 404."""
    a = stub_factory(models=[stub_model_loaded("m1")], models_status_code=401)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
    ]))

    resp = await harness.chat(chat_body("m1", prompt_for(0), turns=2))

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["reason"] == "model_list_unavailable"
    # The message has to name the node and the cause: the operator's fix is a
    # key in the config, and "not offered by any host" would hide that.
    assert "A's model list is unreadable" in error["message"]
    assert "401 Unauthorized" in error["message"]
    assert "check replication.admin_key" in error["message"]
    assert "not offered by any host" not in error["message"]
    # Nothing was dispatched: the model was never claimed to be absent, and no
    # guess was made either.
    assert a.state.chat_calls == 0


async def test_one_unreadable_node_is_enough_to_keep_the_model_placeable(
        stub_factory, ripple_factory):
    """A readable list that lacks the model is not proof while another node
    could not be asked."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")], models_status_code=401)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
        host_config("B", b.url, node_id=b.state.node_id, weight=1),
    ]))

    resp = await harness.chat(chat_body("m2", prompt_for(0), turns=2))

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["reason"] == "model_list_unavailable"
    assert "B's model list is unreadable" in error["message"]
    assert "A's model list" not in error["message"]
    assert a.state.chat_calls == 0 and b.state.chat_calls == 0


async def test_a_model_a_readable_list_lacks_is_still_404_unknown_model(
        stub_factory, ripple_factory):
    """The old answer keeps its meaning where it is a fact about the model."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    resp = await harness.chat(chat_body("no-such-model", prompt_for(0), turns=2))

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["reason"] == "unknown_model"
    assert error["message"] == "model 'no-such-model' is not offered by any host"
    assert a.state.chat_calls == 0 and b.state.chat_calls == 0


async def test_an_unreadable_list_outside_the_allow_list_changes_nothing(
        stub_factory, ripple_factory):
    """A node that would never carry the model cannot make it unknown either:
    its list is not evidence about this model, readable or not."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("other")], models_status_code=401)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
        host_config("B", b.url, node_id=b.state.node_id, weight=1,
                    models=["other"]),
    ]))

    resp = await harness.chat(chat_body("no-such-model", prompt_for(0), turns=2))

    assert resp.status_code == 404
    assert resp.json()["error"]["reason"] == "unknown_model"


async def test_a_failed_read_records_its_cause_and_a_later_one_clears_it(
        stub_factory, ripple_factory):
    """Host level: the state is per probe, not a verdict for the process."""
    a = stub_factory(models=[stub_model_loaded("m1")], models_status_code=401)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
    ]))
    host = harness.host("A")

    assert host.models_error == "401 Unauthorized"
    assert host.models_read_at is None
    assert host.model_list_unreadable is True
    assert host.models == {}
    assert host.model_available("m1") is False
    # The node itself is fine — a key is missing, not a machine.
    assert host.healthy is True and host.status == "healthy"

    a.state.models_status_code = 200
    await host.refresh()

    assert host.models_error is None
    assert host.models_read_at is not None
    assert host.model_list_unreadable is False
    assert host.model_available("m1") is True


async def test_a_later_failure_makes_the_old_list_unusable_as_evidence(
        stub_factory, ripple_factory):
    """A stale list keeps serving what it knows — it just cannot prove absence."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
    ]))
    host = harness.host("A")
    assert host.model_list_unreadable is False

    a.state.models_status_code = 401
    await host.refresh()

    assert host.model_list_unreadable is True
    assert host.models_error == "401 Unauthorized"
    # The entry is still there and still dispatchable: a resident model must
    # not lose its host because the key stopped working.
    assert host.model_available("m1") is True

    resp = await harness.chat(chat_body("m2", prompt_for(0), turns=2))
    assert resp.status_code == 503
    assert resp.json()["error"]["reason"] == "model_list_unavailable"


async def test_healthz_carries_the_model_list_state_per_host(
        stub_factory, ripple_factory):
    """The dashboard has to be able to tell "0 models" from "not known"."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")], models_status_code=401)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
        host_config("B", b.url, node_id=b.state.node_id, weight=1),
    ]))

    health = await harness.healthz()

    row_a = host_row(health, "A")
    assert row_a["models_available"] == 1
    assert row_a["models_read_at"] is not None
    assert row_a["models_error"] is None

    row_b = host_row(health, "B")
    assert row_b["models_available"] == 0
    assert row_b["models_read_at"] is None
    assert row_b["models_error"] == "401 Unauthorized"

    assert any("host B: model list unreadable (401 Unauthorized)" in w
               for w in health["warnings"])
    assert not any("host A: model list unreadable" in w for w in health["warnings"])
