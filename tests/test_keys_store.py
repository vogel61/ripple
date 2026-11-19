"""The client-key store: round-trips, rejections, rotation, and the file itself.

Everything here runs against a temporary file (`tmp_path`); nothing touches the
running service or a real key store. The tests that matter most are the boring
ones — a wrong token must be `None`, a disabled key must be `None`, and the file
must be `0600` — because those are the properties the proxy's auth decision will
be built on.
"""

from __future__ import annotations

import stat

import pytest
import yaml

from ripple.keys import KINDS, ClientKey, KeyStore, KeyStoreError

ADMIN = "rip-admin-" + "a" * 32


def _store(tmp_path, admin: str = "") -> KeyStore:
    return KeyStore(tmp_path / "keys.yaml", admin_key=admin)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# -- the happy path ---------------------------------------------------------
def test_create_and_check_round_trip(tmp_path):
    store = _store(tmp_path)
    created = store.create("alice", kind="batch", models=["m2", "m1"],
                           created_by="richard", note="nightly eval")

    assert isinstance(created, ClientKey)
    assert created.key.startswith("rip-")
    # "rip-" + token_urlsafe(24) is 4 + 32 characters
    assert len(created.key) == 36
    assert created.kind == "batch"
    assert created.models == ["m1", "m2"]      # sorted
    assert created.enabled is True
    assert created.created_at                  # ISO 8601, non-empty
    assert created.created_by == "richard"
    assert created.note == "nightly eval"

    found = store.check(created.key)
    assert found is not None
    assert found.name == "alice"
    assert found.kind == "batch"
    assert found.models == ["m1", "m2"]
    assert found.model_allowed("m1")
    assert not found.model_allowed("m9")


def test_each_created_key_is_different(tmp_path):
    store = _store(tmp_path)
    keys = {store.create(f"k{i}").key for i in range(5)}
    assert len(keys) == 5
    assert all(k.startswith("rip-") for k in keys)


def test_kind_defaults_to_interactive(tmp_path):
    store = _store(tmp_path)
    assert store.create("default").kind == "interactive"
    assert set(KINDS) == {"interactive", "batch", "one-shot"}


def test_models_empty_means_all_models(tmp_path):
    store = _store(tmp_path)
    key = store.create("any")
    assert key.models == []
    assert key.model_allowed("whatever-model")
    # explicit None and empty list both normalise to "no restriction"
    assert store.create("any2", models=None).models == []
    assert store.create("any3", models=[]).models == []


# -- rejections -------------------------------------------------------------
def test_wrong_token_is_unknown(tmp_path):
    store = _store(tmp_path)
    real = store.create("alice")
    assert store.check("rip-not-a-real-key") is None
    assert store.check(real.key[:-1] + ("A" if real.key[-1] != "A" else "B")) is None
    assert store.check(real.key + "x") is None
    assert store.check("") is None
    assert store.check(None) is None


def test_disabled_key_is_not_accepted(tmp_path):
    store = _store(tmp_path)
    key = store.create("paused")

    assert store.set_enabled("paused", False) is True
    assert store.check(key.key) is None
    # the row is still there, only switched off
    assert store.get("paused").enabled is False

    assert store.set_enabled("paused", True) is True
    assert store.check(key.key) is not None


def test_set_enabled_on_an_unknown_name_returns_false(tmp_path):
    assert _store(tmp_path).set_enabled("nobody", False) is False


def test_unknown_name_in_get_raises(tmp_path):
    with pytest.raises(KeyStoreError):
        _store(tmp_path).get("nobody")


def test_duplicate_name_raises(tmp_path):
    store = _store(tmp_path)
    store.create("alice")
    with pytest.raises(KeyStoreError):
        store.create("alice")
    # the original key is untouched and still works
    assert len(store.list()) == 1
    assert store.get("alice").kind == "interactive"


def test_unknown_kind_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(KeyStoreError):
        store.create("alice", kind="streaming")
    assert store.list() == []
    assert not (tmp_path / "keys.yaml").exists()


def test_empty_name_raises(tmp_path):
    with pytest.raises(KeyStoreError):
        _store(tmp_path).create("   ")


# -- admin ------------------------------------------------------------------
def test_admin_key_is_recognised_and_not_a_client_key(tmp_path):
    store = _store(tmp_path, admin=ADMIN)
    client = store.create("alice")

    assert store.is_admin(ADMIN) is True
    assert store.is_admin(client.key) is False
    assert store.is_admin(None) is False
    assert store.is_admin("") is False
    assert store.is_admin(ADMIN[:-1]) is False
    # the admin key is not a client key: no row, no kind, no models
    assert store.check(ADMIN) is None


def test_no_admin_key_means_no_admin(tmp_path):
    store = _store(tmp_path)
    store.create("alice")
    assert store.is_admin(ADMIN) is False
    assert store.is_admin("") is False
    assert store.is_admin(None) is False


def test_constructor_admin_key_wins_over_the_file(tmp_path):
    path = tmp_path / "keys.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "admin_key": "rip-from-the-file",
        "keys": [],
    }), encoding="utf-8")

    from_file = KeyStore(path)
    assert from_file.is_admin("rip-from-the-file") is True

    override = KeyStore(path, admin_key=ADMIN)
    assert override.is_admin(ADMIN) is True
    assert override.is_admin("rip-from-the-file") is False


def test_admin_key_from_the_file_is_kept_on_save(tmp_path):
    path = tmp_path / "keys.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "admin_key": ADMIN, "keys": []}),
                    encoding="utf-8")
    store = KeyStore(path)
    store.create("alice")
    reloaded = KeyStore(path)
    assert reloaded.is_admin(ADMIN) is True


# -- revoke / rotate --------------------------------------------------------
def test_revoke_removes_and_reports_whether_it_existed(tmp_path):
    store = _store(tmp_path)
    key = store.create("alice")

    assert store.revoke("alice") is True
    assert store.check(key.key) is None
    assert store.list() == []
    # idempotent: revoking twice is not an error, it is just False
    assert store.revoke("alice") is False
    assert store.revoke("never-existed") is False


def test_revoke_persists(tmp_path):
    store = _store(tmp_path)
    key = store.create("alice")
    store.revoke("alice")
    assert KeyStore(tmp_path / "keys.yaml").check(key.key) is None


def test_rotate_changes_the_secret_but_keeps_identity(tmp_path):
    store = _store(tmp_path)
    old = store.create("alice", kind="one-shot", models=["m1"], note="laptop")

    rotated = store.rotate("alice")

    assert rotated.name == old.name
    assert rotated.kind == old.kind == "one-shot"
    assert rotated.models == old.models == ["m1"]
    assert rotated.note == "laptop"
    assert rotated.key != old.key
    assert rotated.key.startswith("rip-")
    # exactly one row, and the old secret is dead
    assert len(store.list()) == 1
    assert store.check(old.key) is None
    assert store.check(rotated.key) is not None


def test_rotate_on_an_unknown_name_raises(tmp_path):
    with pytest.raises(KeyStoreError):
        _store(tmp_path).rotate("nobody")


# -- persistence ------------------------------------------------------------
def test_store_survives_a_reload_from_disk(tmp_path):
    path = tmp_path / "keys.yaml"
    store = KeyStore(path, admin_key=ADMIN)
    alice = store.create("alice", kind="batch", models=["m2", "m1"],
                         created_by="richard", note="nightly")
    bob = store.create("bob")
    store.set_enabled("bob", False)

    reloaded = KeyStore(path, admin_key=ADMIN)

    assert [k.name for k in reloaded.list()] == ["alice", "bob"]
    again = reloaded.check(alice.key)
    assert again is not None
    assert again.kind == "batch"
    assert again.models == ["m1", "m2"]
    assert again.created_at == alice.created_at
    assert again.created_by == "richard"
    assert again.note == "nightly"
    assert reloaded.check(bob.key) is None          # still disabled
    assert reloaded.get("bob").enabled is False
    assert reloaded.is_admin(ADMIN) is True


def test_file_layout_is_version_1_yaml(tmp_path):
    path = tmp_path / "keys.yaml"
    store = KeyStore(path, admin_key=ADMIN)
    store.create("alice", kind="batch", models=["m1"])

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["admin_key"] == ADMIN
    assert isinstance(raw["keys"], list) and len(raw["keys"]) == 1
    row = raw["keys"][0]
    assert set(row) == {"name", "key", "kind", "models", "enabled",
                        "created_at", "created_by", "note"}
    assert row["name"] == "alice"
    assert row["key"].startswith("rip-")
    assert row["kind"] == "batch"
    assert row["models"] == ["m1"]
    assert row["enabled"] is True


def test_file_mode_is_0600(tmp_path):
    path = tmp_path / "keys.yaml"
    store = KeyStore(path)
    store.create("alice")
    assert _mode(path) == 0o600

    store.create("bob")            # every rewrite must keep the mode
    assert _mode(path) == 0o600
    store.rotate("alice")
    assert _mode(path) == 0o600

    # and a reloaded store writes it back the same way
    KeyStore(path).save()
    assert _mode(path) == 0o600


def test_save_leaves_no_temporary_files_behind(tmp_path):
    store = _store(tmp_path)
    store.create("alice")
    assert [p.name for p in tmp_path.iterdir()] == ["keys.yaml"]


# -- loading edge cases -----------------------------------------------------
def test_missing_file_loads_as_an_empty_store(tmp_path):
    store = KeyStore(tmp_path / "does-not-exist.yaml")
    assert store.list() == []
    assert store.check("rip-anything") is None
    assert store.is_admin(None) is False


def test_empty_file_loads_cleanly(tmp_path):
    path = tmp_path / "keys.yaml"
    for content in ("", "   \n", "# only a comment\n", "---\n", "version: 1\n"):
        path.write_text(content, encoding="utf-8")
        store = KeyStore(path)
        assert store.list() == []
        assert store.check(None) is None
    # an empty store can still be written to
    store.create("alice")
    assert len(KeyStore(path).list()) == 1


def test_corrupt_file_raises_with_the_path(tmp_path):
    path = tmp_path / "keys.yaml"
    path.write_text("keys: [ {name: alice, key: rip-x\n", encoding="utf-8")
    with pytest.raises(KeyStoreError) as exc:
        KeyStore(path)
    assert str(path) in str(exc.value)


def test_corrupt_structure_raises(tmp_path):
    path = tmp_path / "keys.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "keys": "not-a-list"}),
                    encoding="utf-8")
    with pytest.raises(KeyStoreError) as exc:
        KeyStore(path)
    assert str(path) in str(exc.value)


def test_duplicate_names_in_the_file_raise(tmp_path):
    path = tmp_path / "keys.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "keys": [{"name": "alice", "key": "rip-one"},
                 {"name": "alice", "key": "rip-two"}],
    }), encoding="utf-8")
    with pytest.raises(KeyStoreError):
        KeyStore(path)


def test_unknown_kind_in_the_file_raises(tmp_path):
    path = tmp_path / "keys.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "keys": [{"name": "alice", "key": "rip-one", "kind": "turbo"}],
    }), encoding="utf-8")
    with pytest.raises(KeyStoreError):
        KeyStore(path)


# -- views ------------------------------------------------------------------
def test_list_keeps_the_secrets_and_masked_hides_them(tmp_path):
    store = _store(tmp_path)
    key = store.create("alice", kind="batch", models=["m1"], note="laptop")

    listed = store.list()
    assert [k.key for k in listed] == [key.key]

    masked = store.masked()
    assert len(masked) == 1
    row = masked[0]
    assert row["name"] == "alice"
    assert row["kind"] == "batch"
    assert row["models"] == ["m1"]
    assert row["enabled"] is True
    assert row["note"] == "laptop"
    assert row["key"] == f"{key.key[:6]}…{key.key[-4:]}"
    assert key.key not in row["key"]
    assert "…" in row["key"]


def test_masked_never_contains_the_whole_key(tmp_path):
    store = _store(tmp_path, admin=ADMIN)
    created = [store.create(f"k{i}") for i in range(3)]
    dump = repr(store.masked())
    for k in created:
        assert k.key not in dump
    assert ADMIN not in dump


def test_masked_of_a_short_key_does_not_reveal_it(tmp_path):
    path = tmp_path / "keys.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "keys": [{"name": "tiny", "key": "rip-abc"}],
    }), encoding="utf-8")
    store = KeyStore(path)
    assert store.masked()[0]["key"] == "…"
    assert store.check("rip-abc") is not None


def test_list_returns_copies(tmp_path):
    store = _store(tmp_path)
    store.create("alice")
    row = store.list()[0]
    row.enabled = False
    row.models.append("m9")
    assert store.get("alice").enabled is True
    assert store.get("alice").models == []


# -- hostile input ----------------------------------------------------------
def test_non_ascii_token_is_rejected_without_crashing(tmp_path):
    store = _store(tmp_path, admin=ADMIN)
    store.create("alice")
    # hmac.compare_digest refuses non-ASCII str; a 500 here would be reachable
    # without a key, so the store compares bytes instead
    assert store.check("rip-kéy-ünicode") is None
    assert store.is_admin("rip-kéy-ünicode") is False
    assert store.check("🔑") is None


def test_a_token_that_is_not_a_string_is_rejected(tmp_path):
    store = _store(tmp_path, admin=ADMIN)
    store.create("alice")
    assert store.check(12345) is None          # type: ignore[arg-type]
    assert store.check(b"rip-bytes") is None   # type: ignore[arg-type]
    assert store.is_admin(12345) is False      # type: ignore[arg-type]


def test_keys_are_not_in_error_messages(tmp_path):
    store = _store(tmp_path)
    key = store.create("alice")
    with pytest.raises(KeyStoreError) as exc:
        store.create("alice")
    assert key.key not in str(exc.value)
    assert "alice" in str(exc.value)
