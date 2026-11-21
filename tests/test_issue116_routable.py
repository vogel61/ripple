"""A model that is not loaded is not routable — lock or no lock (issue #116).

The report: the model view showed `DeepSeek-V4.1-Flash-1017-oQ4e-mtp` as *not
loaded* on `alpha` and still offered it as routable, on the same screen as an
eviction lock. The suspicion was that the lock was read as "stays resident".

The lock is not in the routability path at all (`ModelPolicy.locked()` is asked
by the unload paths and by the padlock, never by a dispatch decision). The defect
was one step earlier: the availability column was derived from `dispatch`, and
`HostRuntime.model_available()` is `model_permitted() and model in self.models` —
the node's *listing*. A model oMLX lists but does not hold is dispatchable (a
cold load is a legitimate answer to "would RIPPLE send a request here") and was
therefore painted as routable, while the residency column said `loaded: false`.

The fix separates the two facts: `HostRuntime.model_routable()` adds residency
(and a readable list) to the dispatch predicate, `/healthz` and `/admin/models`
carry it as `routable`, and the page paints the availability column from it. The
lock stays what it is — a veto on an unload.

Variant chosen: *not routable* rather than "load on demand". RIPPLE has no load
call in the view path, and starting a several-hundred-GB load from a dashboard
tick is a side effect nobody asked for — the very cost the flag exists to avoid.
"""

from __future__ import annotations

from ripple.config import LoadingConfig, PlacementConfig

from tests.support import (DEEPSEEK_SIZE, StubModel, bearer, host_config,
                           keyed_config)

ADMIN = bearer("test-admin-key")
DEEPSEEK = "DeepSeek-V4.1-Flash-1017-oQ4e-mtp"


def _cfg(tmp_path, hosts):
    """Admin key on, every operator store under `tmp_path` (never `~/.ripple`)."""
    cfg = keyed_config(hosts, str(tmp_path / "keys.yaml"))
    cfg.placement = PlacementConfig(
        model_policy_file=str(tmp_path / "models.yaml"),
        maintenance_file=str(tmp_path / "maintenance.yaml"),
        loading=LoadingConfig(calibration_file=str(tmp_path / "calibration.yaml"),
                              samples=1, max_seconds=10.0))
    return cfg


def _cold(loaded: bool) -> StubModel:
    return StubModel(id=DEEPSEEK, loaded=loaded, estimated_size=DEEPSEEK_SIZE)


def _row(health: dict, model: str) -> dict:
    return next(m for m in health["models"] if m["id"] == model)


def _cell(health: dict, model: str, host: str) -> dict:
    return next(h for h in _row(health, model)["hosts"] if h["host"] == host)


async def _lock(harness, model: str = DEEPSEEK):
    return await harness.client.post(f"/admin/models/{model}/lock", headers=ADMIN)


# ---------------------------------------------------------------------------
# The combination the issue asks for: loaded = false + locked = true
# ---------------------------------------------------------------------------
async def test_a_locked_model_that_is_not_loaded_is_not_routable(
        stub_factory, ripple_factory, tmp_path):
    """`loaded: false` + `locked: true` ⇒ not routable, and the lock is reported.

    Measured before the fix: `dispatch: true`, `routable` did not exist, and the
    page derived the availability column from `dispatch` — so a locked, cold
    entry read as "this node can serve it right now".
    """
    stub = stub_factory(models=[_cold(loaded=False)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("alpha", stub.url, node_id=stub.state.node_id)]))

    # The premise: oMLX lists the model and has nothing loaded — so the entry is
    # dispatchable (a cold load is a legitimate dispatch) ...
    assert harness.host("alpha").model_available(DEEPSEEK) is True
    assert harness.host("alpha").model_routable(DEEPSEEK) is False

    resp = await _lock(harness)
    assert resp.status_code == 200, resp.text
    assert harness.ripple.policy.locked(DEEPSEEK) is True

    health = await harness.healthz()
    cell = _cell(health, DEEPSEEK, "alpha")
    assert cell["loaded"] is False
    assert cell["routable"] is False          # the fix: residency, not the lock
    assert cell["dispatch"] is True           # unchanged fact: RIPPLE would send
    assert cell["lock"] is False              # a padlock marks a *resident* copy
    row = _row(health, DEEPSEEK)
    assert row["lock"] is True                # the model-level lock is reported
    assert row["routable"] is False           # no host holds it
    # A lock on a model nobody loaded refuses nothing — it must not claim a 503.
    assert row["lock_reason"] is None


async def test_the_admin_view_reports_the_same_pair(
        stub_factory, ripple_factory, tmp_path):
    """`GET /admin/models` carries `routable` per host too — one predicate, one
    answer, so the page cannot read a different truth from the other endpoint."""
    stub = stub_factory(models=[_cold(loaded=False)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("alpha", stub.url, node_id=stub.state.node_id)]))
    assert (await _lock(harness)).status_code == 200

    body = (await harness.client.get("/admin/models", headers=ADMIN)).json()
    row = next(r for r in body["models"] if r["id"] == DEEPSEEK)
    cell = next(h for h in row["hosts"] if h["host"] == "alpha")
    assert row["lock"] is True and row["lock_reason"] is None
    assert cell == {"host": "alpha", "offered": True, "loaded": False,
                    "lock": False, "policy": None, "dispatch": True,
                    "routable": False, "fits": True, "fit_reason": None}


# ---------------------------------------------------------------------------
# The lock must not break what works: resident + locked stays routable
# ---------------------------------------------------------------------------
async def test_a_locked_model_that_is_loaded_stays_routable(
        stub_factory, ripple_factory, tmp_path):
    """`loaded: true` + `locked: true` ⇒ still routable, and still served.

    The control: the fix must not turn the lock into a reason to stop routing.
    """
    stub = stub_factory(models=[_cold(loaded=True)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("alpha", stub.url, node_id=stub.state.node_id)]))
    assert (await _lock(harness)).status_code == 200

    health = await harness.healthz()
    cell = _cell(health, DEEPSEEK, "alpha")
    assert cell["loaded"] is True and cell["routable"] is True
    assert cell["dispatch"] is True and cell["lock"] is True
    row = _row(health, DEEPSEEK)
    assert row["lock"] is True and row["routable"] is True
    assert row["lock_reason"] is not None and "alpha" in row["lock_reason"]


# ---------------------------------------------------------------------------
# The third gate: an unreadable list says nothing about residency
# ---------------------------------------------------------------------------
async def test_an_unreadable_model_list_is_not_routable(
        stub_factory, ripple_factory, tmp_path):
    """"Unknown" is not "yes" (issue #53): the dispatch predicate already says no
    for an unreadable list, and the residency predicate must not resurrect it."""
    stub = stub_factory(models=[_cold(loaded=True)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("alpha", stub.url, node_id=stub.state.node_id)]))
    host = harness.host("alpha")
    assert host.model_routable(DEEPSEEK) is True

    # The next read fails: the list it left behind is stale, not evidence.
    host.models_error = "connect refused"
    assert host.model_available(DEEPSEEK) is True     # stale list, unchanged rule
    assert host.model_routable(DEEPSEEK) is False

    cell = _cell(await harness.healthz(), DEEPSEEK, "alpha")
    assert cell["routable"] is False and cell["dispatch"] is True


async def test_a_model_the_node_does_not_list_is_not_routable(
        stub_factory, ripple_factory, tmp_path):
    """The negative of the negative: nothing to serve ⇒ both answers are no."""
    stub = stub_factory(models=[])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("alpha", stub.url, node_id=stub.state.node_id)]))
    host = harness.host("alpha")
    assert host.model_available(DEEPSEEK) is False
    assert host.model_routable(DEEPSEEK) is False
