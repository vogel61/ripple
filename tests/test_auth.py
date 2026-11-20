"""Client authentication: FA-23 / AS-14.

Measured 14.11.2026 before this existed: `POST :8010` without any Authorization
header answered 200 while oMLX itself answered 401 — every machine on the LAN
could use the models through the balancer. These tests pin the hole shut.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripple.config import AuthConfig

from tests.support import chat_body, host_config, make_config, stub_model_loaded

ADMIN = "test-admin-key"


def _cfg(hosts, keys_file: Path, *, allow_loopback: bool = False):
    cfg = make_config(hosts)
    cfg.auth = AuthConfig(enabled=True, keys_file=str(keys_file), admin_key=ADMIN,
                          allow_loopback=allow_loopback)
    return cfg


def _auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


async def test_fa23_request_without_key_is_rejected(stub_factory, ripple_factory, tmp_path):
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(
        _cfg([host_config("A", stub.url, node_id=stub.state.node_id)], tmp_path / "keys.yaml"))

    resp = await harness.chat(chat_body("m1", "hi", turns=2))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"
    assert stub.state.chat_bodies == []      # never reached the host
    assert (await harness.stats())["auth_failures"] == 1


async def test_fa23_valid_key_passes(stub_factory, ripple_factory, tmp_path):
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(
        _cfg([host_config("A", stub.url, node_id=stub.state.node_id)], tmp_path / "keys.yaml"))
    key = harness.ripple.keys.create("hermes", "interactive")

    resp = await harness.ok(chat_body("m1", "hi", turns=2), headers=_auth(key.key))

    assert resp.status_code == 200
    assert (await harness.stats())["by_key"] == {"hermes": 1}


async def test_fa23_disabled_key_is_rejected(stub_factory, ripple_factory, tmp_path):
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(
        _cfg([host_config("A", stub.url, node_id=stub.state.node_id)], tmp_path / "keys.yaml"))
    key = harness.ripple.keys.create("alice", "interactive")
    harness.ripple.keys.set_enabled("alice", False)

    resp = await harness.chat(chat_body("m1", "hi", turns=2), headers=_auth(key.key))

    assert resp.status_code == 401


async def test_fa23_model_outside_the_key_scope_is_404(stub_factory, ripple_factory, tmp_path):
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(
        _cfg([host_config("A", stub.url, node_id=stub.state.node_id)], tmp_path / "keys.yaml"))
    key = harness.ripple.keys.create("alice", "interactive", models=["other-model"])

    resp = await harness.chat(chat_body("m1", "hi", turns=2), headers=_auth(key.key))

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_fa23_models_endpoint_needs_a_key_and_filters(stub_factory, ripple_factory, tmp_path):
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(
        _cfg([host_config("A", stub.url, node_id=stub.state.node_id)], tmp_path / "keys.yaml"))

    assert (await harness.client.get("/v1/models")).status_code == 401

    scoped = harness.ripple.keys.create("scoped", "interactive", models=["nope"])
    listing = await harness.client.get("/v1/models", headers=_auth(scoped.key))
    assert listing.status_code == 200
    assert listing.json()["data"] == []

    everything = harness.ripple.keys.create("all", "interactive")
    listing = await harness.client.get("/v1/models", headers=_auth(everything.key))
    assert [m["id"] for m in listing.json()["data"]] == ["m1"]


async def test_fa23_loopback_exemption(stub_factory, ripple_factory, tmp_path):
    """The staged rollout: with allow_loopback the operator's own tools keep
    working while the LAN already needs a key."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(
        _cfg([host_config("A", stub.url, node_id=stub.state.node_id)], tmp_path / "keys.yaml",
             allow_loopback=True))

    resp = await harness.chat(chat_body("m1", "hi", turns=2))

    assert resp.status_code == 200


async def test_fa23_admin_endpoints(stub_factory, ripple_factory, tmp_path):
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(
        _cfg([host_config("A", stub.url, node_id=stub.state.node_id)], tmp_path / "keys.yaml"))

    assert (await harness.client.get("/admin/keys")).status_code == 401
    assert (await harness.client.get("/admin/keys", headers=_auth("wrong"))).status_code == 401

    created = await harness.client.post("/admin/keys", headers=_auth(ADMIN),
                                        json={"name": "alice", "kind": "interactive"})
    assert created.status_code == 200
    body = created.json()
    assert body["key"].startswith("rip-")
    assert body["name"] == "alice"

    listing = await harness.client.get("/admin/keys", headers=_auth(ADMIN))
    names = [k["name"] for k in listing.json()["keys"]]
    assert names == ["alice"]
    assert body["key"] not in str(listing.json())     # only the mask leaves the API

    # the created key works for inference
    resp = await harness.ok(chat_body("m1", "hi", turns=2), headers=_auth(body["key"]))
    assert resp.status_code == 200

    rotated = await harness.client.post("/admin/keys/alice/rotate", headers=_auth(ADMIN))
    assert rotated.status_code == 200
    assert rotated.json()["key"] != body["key"]
    # the old secret is dead, the new one works
    assert (await harness.chat(chat_body("m1", "hi", turns=2),
                               headers=_auth(body["key"]))).status_code == 401
    assert (await harness.chat(chat_body("m1", "hi", turns=2),
                               headers=_auth(rotated.json()["key"]))).status_code == 200

    assert (await harness.client.post("/admin/keys/alice/revoke",
                                      headers=_auth(ADMIN))).json()["revoked"] is True
    assert (await harness.client.get("/admin/keys", headers=_auth(ADMIN))).json()["keys"] == []


async def test_fa23_keys_page_is_served(stub_factory, ripple_factory, tmp_path):
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(
        _cfg([host_config("A", stub.url, node_id=stub.state.node_id)], tmp_path / "keys.yaml"))

    page = await harness.client.get("/keys")
    assert page.status_code == 200
    assert "/admin/keys" in page.text
    assert ADMIN not in page.text        # the admin key is never rendered into the page


async def test_fa23_auth_disabled_changes_nothing(stub_factory, ripple_factory):
    """Default stays off so a fresh checkout behaves as before."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([host_config("A", stub.url,
                                                            node_id=stub.state.node_id)]))
    assert (await harness.chat(chat_body("m1", "hi", turns=2))).status_code == 200
