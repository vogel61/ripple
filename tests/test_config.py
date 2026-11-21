"""`placement.optimizer` (issue #80) and the routing policy the page may set (#78).

The optimizer scans the fleet on a timer, so it needs a schedule and a budget
of its own — separate from `placement.rebind`, which guards every single move a
request happens to trigger. A missing block is not an error: the defaults are
the documented behaviour, and an existing config file keeps loading unchanged.

The one value that is rejected rather than clamped is `interval_seconds: 0`:
"scan continuously" is a busy loop that prices every thread on every pass and
spends the hourly budget on churn, and a config file that asks for it has a
typo, not an intention.

Since #78 the same file's routing policy may also be set at the page, through
one dotted-path grammar and one overlay. The tests at the bottom of this file
are the contract of that: the allow-list, the path grammar, and — the point of
the issue — that a value the page refuses is refused with the loader's own
sentence, because it *is* the loader's own parser.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from ripple.config import (EDITABLE_PATHS, FIELD_HELP, ConfigError, FleetOverlay,
                           apply_overlay, load_config, load_overlay, policy_assign,
                           policy_read, policy_refusal, policy_target, policy_value,
                           save_overlay)

from tests.support import closed_port


def write_config(tmp_path: Path, *, placement: dict[str, Any] | None = None,
                 hosts: list[dict[str, Any]] | None = None,
                 affinity: dict[str, Any] | None = None) -> Path:
    """A minimal, loadable configuration with machine state inside `tmp_path`."""
    plc: dict[str, Any] = {
        "loading": {"calibration_file": str(tmp_path / "calibration.yaml")},
        "maintenance_file": str(tmp_path / "maintenance.yaml"),
        "model_policy_file": str(tmp_path / "models.yaml"),
    }
    plc.update(placement or {})
    data: dict[str, Any] = {
        "listen_host": "127.0.0.1",
        "listen_port": closed_port(),
        "log_level": "warning",
        "hosts": hosts or [{"name": "alpha", "addresses": ["http://127.0.0.1:8000"],
                            "weight": 1}],
        "discovery": {"interval_seconds": 3600.0, "timeout_seconds": 2.0},
        "placement": plc,
        "stats": {"file": str(tmp_path / "host_stats.json"), "persist": False},
        "auth": {"enabled": False, "keys_file": str(tmp_path / "keys.yaml")},
        # The overlay lives beside the config in the test's own directory
        # (issue #78): no test may read or write ~/.ripple/runtime.yaml.
        "fleet": {"file": str(tmp_path / "runtime.yaml")},
    }
    if affinity is not None:
        data["affinity"] = affinity
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def config_with(tmp_path: Path, path: str, value: Any) -> Path:
    """A configuration file that carries `value` at the dotted `path`.

    The counterpart of `policy_value` for the "same message as start-up" test:
    the same value is put where `load_config` reads it and where the endpoint
    would receive it, and the two refusals must be the same sentence.
    """
    parts = path.split(".")
    if parts[0] == "hosts":
        host: dict[str, Any] = {"name": "alpha",
                                "addresses": ["http://127.0.0.1:8000"], "weight": 1}
        host[parts[-1]] = value
        return write_config(tmp_path, hosts=[host])
    if parts[0] == "affinity":
        return write_config(tmp_path, affinity={parts[-1]: value})
    if len(parts) == 3:
        return write_config(tmp_path, placement={parts[1]: {parts[2]: value}})
    return write_config(tmp_path, placement={parts[1]: value})


# ---------------------------------------------------------------------------
# 1. Defaults without the block
# ---------------------------------------------------------------------------
def test_the_block_is_optional_and_has_working_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path))
    opt = cfg.placement.optimizer
    assert opt.enabled is True
    assert opt.interval_seconds == 30.0
    assert opt.max_moves_per_hour == 4


def test_an_empty_block_is_the_same_as_no_block(tmp_path):
    cfg = load_config(write_config(tmp_path, placement={"optimizer": {}}))
    opt = cfg.placement.optimizer
    assert (opt.enabled, opt.interval_seconds, opt.max_moves_per_hour) == (True, 30.0, 4)


def test_rebind_keeps_its_own_block_untouched(tmp_path):
    """The optimizer is a second block, not a rewrite of rebind."""
    cfg = load_config(write_config(tmp_path, placement={
        "rebind": {"enabled": False, "max_per_hour": 2},
        "optimizer": {"max_moves_per_hour": 9},
    }))
    assert cfg.placement.rebind.enabled is False
    assert cfg.placement.rebind.max_per_hour == 2
    assert cfg.placement.optimizer.max_moves_per_hour == 9


# ---------------------------------------------------------------------------
# 2. Set values are read
# ---------------------------------------------------------------------------
def test_set_values_are_read(tmp_path):
    cfg = load_config(write_config(tmp_path, placement={
        "optimizer": {"enabled": False, "interval_seconds": 5.5,
                      "max_moves_per_hour": 0},
    }))
    opt = cfg.placement.optimizer
    assert opt.enabled is False
    assert opt.interval_seconds == 5.5
    # 0 is a legitimate budget: the optimizer is on, but may not move anything
    # (the state an operator wants while watching the reason log).
    assert opt.max_moves_per_hour == 0


def test_an_integer_interval_is_accepted_as_a_number(tmp_path):
    cfg = load_config(write_config(tmp_path, placement={
        "optimizer": {"interval_seconds": 60},
    }))
    assert cfg.placement.optimizer.interval_seconds == 60.0
    assert isinstance(cfg.placement.optimizer.interval_seconds, float)


# ---------------------------------------------------------------------------
# 3. Nonsense is rejected, with a message that names the field
# ---------------------------------------------------------------------------
def test_a_zero_interval_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as err:
        load_config(write_config(tmp_path, placement={
            "optimizer": {"interval_seconds": 0},
        }))
    assert "placement.optimizer.interval_seconds" in str(err.value)
    assert "must be > 0" in str(err.value)


def test_a_negative_interval_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as err:
        load_config(write_config(tmp_path, placement={
            "optimizer": {"interval_seconds": -5},
        }))
    assert "placement.optimizer.interval_seconds" in str(err.value)


def test_a_negative_budget_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as err:
        load_config(write_config(tmp_path, placement={
            "optimizer": {"max_moves_per_hour": -1},
        }))
    assert "placement.optimizer.max_moves_per_hour" in str(err.value)


def test_a_non_numeric_interval_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as err:
        load_config(write_config(tmp_path, placement={
            "optimizer": {"interval_seconds": "soon"},
        }))
    assert "placement.optimizer.interval_seconds" in str(err.value)
    assert "must be a number" in str(err.value)


def test_a_non_mapping_block_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as err:
        load_config(write_config(tmp_path, placement={"optimizer": "on"}))
    assert "placement.optimizer must be a mapping" in str(err.value)


# ---------------------------------------------------------------------------
# 4. The routing policy the page may set (issue #78)
# ---------------------------------------------------------------------------
#: One valid value per editable path. The keys are the allow-list itself, so a
#: path added to `POLICY_EDITABLE` without a value here fails the test below.
VALID_VALUES: dict[str, Any] = {
    "hosts.*.weight": 2.5,
    "hosts.*.spill_threshold": 2,
    "hosts.*.prefer_resident": False,
    "affinity.ttl_seconds": 900,
    "affinity.match_threshold": 0.8,
    "affinity.ephemeral_window_seconds": 30,
    "placement.rebind.enabled": False,
    "placement.rebind.max_per_hour": 6,
    "placement.rebind.safety_factor": 2.0,
    "placement.rebind.per_thread_cooldown_seconds": 120.0,
    "placement.balance_abs_threshold": 2.0,
    "placement.balance_rel_threshold": 1.2,
}


def test_the_allow_list_is_exactly_the_twelve_policy_paths():
    """The list is the contract: policy, never identity or transport."""
    assert len(EDITABLE_PATHS) == 12
    assert set(EDITABLE_PATHS) == set(VALID_VALUES)
    for path in EDITABLE_PATHS:
        assert path not in ("listen_port", "log_file", "log_level", "auth.admin_key")
        assert "api_key" not in path and "addresses" not in path


@pytest.mark.parametrize("pattern", EDITABLE_PATHS)
def test_every_editable_field_accepts_a_value_and_lands_in_the_right_place(
        tmp_path, pattern):
    cfg = load_config(write_config(tmp_path))
    path = pattern.replace("*", "alpha")
    value = VALID_VALUES[pattern]

    effective = policy_value(cfg, path, value)
    policy_assign(cfg, path, effective)

    assert policy_read(cfg, path) == value
    # …and it is the live object that changed, not a copy: the registry holds
    # `cfg.affinity` and every HostRuntime holds its HostConfig.
    if pattern.startswith("hosts."):
        assert getattr(cfg.host("alpha"), pattern.split(".")[-1]) == value
    elif pattern.startswith("affinity."):
        assert getattr(cfg.affinity, pattern.split(".")[-1]) == value


# A value the loader refuses, per field family. The point of the issue is that
# the page and the start-up log say the same thing, so the assertion is string
# equality with what `load_config` raises for the same value in the file.
REFUSED: list[tuple[str, Any]] = [
    ("hosts.alpha.weight", -1),
    ("hosts.alpha.spill_threshold", 99),
    ("affinity.ttl_seconds", 0),
    ("affinity.match_threshold", 1.5),
    ("affinity.ephemeral_window_seconds", -1),
    ("placement.rebind.max_per_hour", -1),
    ("placement.rebind.safety_factor", -1),
    ("placement.rebind.per_thread_cooldown_seconds", -1),
    ("placement.balance_abs_threshold", "soon"),
    ("placement.balance_rel_threshold", -1),
]


@pytest.mark.parametrize("path,value", REFUSED)
def test_a_value_refused_at_startup_is_refused_with_the_same_message(
        tmp_path, path, value):
    cfg = load_config(write_config(tmp_path))
    with pytest.raises(ConfigError) as startup:
        load_config(config_with(tmp_path, path, value))
    with pytest.raises(ConfigError) as page:
        policy_value(cfg, path, value)
    assert str(page.value) == str(startup.value)


def test_a_boolean_field_wants_a_real_boolean(tmp_path):
    """`bool("false")` is true; the loader reads it that way and the page must not.

    A JSON body has `true`/`false` and nothing else, so a string is a mistake
    the page can see — accepting it would flip the value silently.
    """
    cfg = load_config(write_config(tmp_path))
    with pytest.raises(ConfigError) as err:
        policy_value(cfg, "hosts.alpha.prefer_resident", "false")
    assert "must be true or false" in str(err.value)
    assert policy_value(cfg, "hosts.alpha.prefer_resident", True) is True


@pytest.mark.parametrize("path", [
    "listen_port", "listen_host", "log_file", "log_level",
    "auth.admin_key", "auth.enabled",
    "hosts.alpha.api_key", "hosts.alpha.addresses", "hosts.alpha.node_id",
    "hosts.alpha.max_threads", "hosts.alpha.models",
    "affinity.header", "placement.binding_strategy", "placement.loader",
    "nonsense.path",
])
def test_a_path_outside_the_allow_list_is_refused_with_a_reason(path):
    """403, not 404: the field exists, it is simply not settable."""
    assert policy_target(path) is None
    reason = policy_refusal(path)
    assert path in reason
    assert "config.yaml" in reason or "restart" in reason or "admin/nodes" in reason


def test_the_refusal_names_identity_and_transport_as_such():
    assert "credential" in policy_refusal("hosts.alpha.api_key")
    assert "identity" in policy_refusal("hosts.alpha.node_id")
    assert "start-up" in policy_refusal("listen_port")
    assert "orphan" in policy_refusal("affinity.header")


def test_a_policy_override_round_trips_through_the_file(tmp_path):
    """The acceptance case: set, written, read back, in force — file untouched."""
    path = write_config(tmp_path)
    before = path.read_bytes()
    cfg = load_config(path)
    overlay = FleetOverlay(path=tmp_path / "runtime.yaml")
    assert apply_overlay(cfg, overlay) == []
    assert cfg.overrides == {}
    assert cfg.baseline["affinity.ttl_seconds"] == 1800

    for dotted, value in (("affinity.ttl_seconds", 900),
                          ("placement.rebind.max_per_hour", 6),
                          ("hosts.alpha.weight", 2.0)):
        overlay.set_path(dotted, policy_value(cfg, dotted, value))
    save_overlay(overlay)

    raw = yaml.safe_load((tmp_path / "runtime.yaml").read_text(encoding="utf-8"))
    assert raw["policy"] == {"affinity": {"ttl_seconds": 900},
                             "placement": {"rebind": {"max_per_hour": 6}}}
    assert raw["hosts"]["overrides"] == {"alpha": {"weight": 2.0}}

    # A restart: the same file, a fresh Config, the overlay merged over it.
    fresh = load_config(path)
    assert apply_overlay(fresh, load_overlay(tmp_path / "runtime.yaml")) == []
    assert fresh.affinity.ttl_seconds == 900
    assert fresh.placement.rebind.max_per_hour == 6
    assert fresh.host("alpha").weight == 2.0
    assert fresh.overrides == {"affinity.ttl_seconds": 900,
                               "placement.rebind.max_per_hour": 6,
                               "hosts.alpha.weight": 2.0}
    assert path.read_bytes() == before


def test_a_dropped_override_falls_back_to_the_file(tmp_path):
    path = write_config(tmp_path)
    cfg = load_config(path)
    overlay = FleetOverlay(path=tmp_path / "runtime.yaml")
    apply_overlay(cfg, overlay)
    overlay.set_path("affinity.ttl_seconds", 900)
    overlay.set_path("placement.rebind.max_per_hour", 6)
    save_overlay(overlay)
    assert overlay.drop_path("affinity.ttl_seconds") is True
    save_overlay(overlay)

    raw = yaml.safe_load((tmp_path / "runtime.yaml").read_text(encoding="utf-8"))
    assert raw["policy"] == {"placement": {"rebind": {"max_per_hour": 6}}}

    fresh = load_config(path)
    apply_overlay(fresh, load_overlay(tmp_path / "runtime.yaml"))
    assert fresh.affinity.ttl_seconds == 1800          # the file's value
    assert fresh.placement.rebind.max_per_hour == 6
    assert "affinity.ttl_seconds" not in fresh.overrides


def test_an_overlay_value_the_loader_would_refuse_is_a_warning(tmp_path):
    """A hand-edited overlay must not half-apply a block the loader refuses."""
    cfg = load_config(write_config(tmp_path))
    overlay = FleetOverlay()
    overlay.policy = {"affinity": {"ttl_seconds": "soon"}}
    warnings = apply_overlay(cfg, overlay)
    assert any("affinity.ttl_seconds must be an integer" in w for w in warnings)
    assert cfg.affinity.ttl_seconds == 1800
    # The page must not claim a value that never landed.
    assert "affinity.ttl_seconds" not in cfg.overrides


def test_an_unknown_key_in_the_policy_block_is_a_warning(tmp_path, caplog):
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump({"policy": {"affinity": {"ttl_seconds": 900,
                                                            "nonsense": 1}}}),
                    encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="ripple.config"):
        overlay = load_overlay(path)
    assert overlay.policy == {"affinity": {"ttl_seconds": 900}}
    assert any("overlay_unknown_key" in r.getMessage() for r in caplog.records)


def test_a_fleet_file_from_70_is_read_once_and_the_next_save_uses_runtime(
        tmp_path, caplog):
    """The rename is not a migration: one warning, the old file stays, the new
    name is written on the first save."""
    path = write_config(tmp_path)
    cfg = load_config(path)
    legacy = tmp_path / "fleet.yaml"
    legacy.write_text(yaml.safe_dump({
        "hosts": {"M5-beta": {"addresses": ["http://127.0.0.1:8001"]}},
        "overrides": {"alpha": {"weight": 3.0}},
    }), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="ripple.config"):
        overlay = load_overlay(cfg.fleet.file)
    assert list(overlay.hosts) == ["M5-beta"]
    assert overlay.overrides == {"alpha": {"weight": 3.0}}
    assert sum("overlay_legacy_file" in r.getMessage() for r in caplog.records) == 1
    assert not (tmp_path / "runtime.yaml").exists()

    save_overlay(overlay)
    assert (tmp_path / "runtime.yaml").is_file()
    assert legacy.is_file()                     # read, never rewritten
    assert load_overlay(cfg.fleet.file).overrides == {"alpha": {"weight": 3.0}}


def test_the_config_view_says_what_is_editable_and_what_is_overridden(tmp_path):
    cfg = load_config(write_config(tmp_path))
    overlay = FleetOverlay()
    overlay.set_path("affinity.ttl_seconds", 900)
    apply_overlay(cfg, overlay)

    view = cfg.summary()
    assert view["overrides"] == {"affinity.ttl_seconds": 900}
    assert view["editable"] == list(EDITABLE_PATHS)
    assert "hosts.*.weight" in view["editable"]
    assert "listen_port" not in view["editable"]
    assert view["affinity"]["ttl_seconds"] == 900


# -- the sentence that travels with every field (issue #139) ----------------
#
# The parameter table shows a row per field and a reader who does not know the
# name; the sentence that explains it is part of the schema, not of the page,
# so a rename has to carry it along. These three tests are what makes that
# true: a value that is missing, empty or a restatement of its own path fails
# here, and a new entry in the allow-list without a sentence fails with it.

def _squash(text: str) -> str:
    """`hosts.*.spill_threshold` and `HOSTS * SPILL THRESHOLD` compare equal."""
    return text.lower().replace(".", "").replace("_", "")


def test_every_help_sentence_is_a_sentence(tmp_path):
    """Non-empty, at least 20 characters, and it ends in a full stop."""
    cfg = load_config(write_config(tmp_path))
    help_text = cfg.summary()["help"]

    assert help_text, "the parameter table has no help at all"
    assert help_text == FIELD_HELP, "summary() must ship the table, not a copy"
    for path, sentence in help_text.items():
        assert isinstance(sentence, str) and sentence.strip(), path
        assert sentence.endswith("."), f"{path}: no full stop"
        assert len(sentence) >= 20, f"{path}: too short to explain anything"


def test_every_editable_path_has_its_own_sentence():
    """The allow-list is the list; a new path without a sentence fails here."""
    for path in EDITABLE_PATHS:
        assert path in FIELD_HELP, f"{path} may be set at the page but is unexplained"
        assert FIELD_HELP[path].strip(), f"{path} has an empty sentence"


def test_no_sentence_just_repeats_its_path():
    """`spill_threshold.` is a name twice, not an explanation."""
    for path, sentence in FIELD_HELP.items():
        assert _squash(sentence) != _squash(path), f"{path}: the sentence is the path"
        assert len(_squash(sentence)) > len(_squash(path)), \
            f"{path}: the sentence says nothing beyond the name"
