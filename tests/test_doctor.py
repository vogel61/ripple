"""Tests for `ripple doctor` (issue #63).

Every node here is a stub (`tests/support.StubUpstream`) served on an ephemeral
port, and the proxy is a real uvicorn instance from `live_proxy_factory`. No test
talks to a real oMLX, and none reads or writes the operator's state: the config
file doctor reads names a temporary key store, a temporary ledger and temporary
operator stores.

The config file is the single source of truth for both sides — doctor parses it
and the live proxy is built from `load_config` of the same file, so a check that
passes here is a check against the configuration the proxy actually runs with.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ripple import doctor
from ripple.config import load_config
from ripple.keys import KeyStore
from tests.support import StubModel, closed_port


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def write_config(tmp_path: Path, hosts: List[Dict[str, Any]], *,
                 auth: Optional[Dict[str, Any]] = None,
                 keys_file: Optional[Path] = None,
                 sites: Optional[List[Dict[str, Any]]] = None,
                 rebind: Optional[Dict[str, Any]] = None) -> Path:
    """A config file that doctor and the live proxy both read.

    Every path that would otherwise be machine state (`~/.ripple/…`, the host
    ledger) is redirected into `tmp_path`, so a test can never touch the
    operator's files.
    """
    store = keys_file if keys_file is not None else tmp_path / "keys.yaml"
    placement: Dict[str, Any] = {
        "loading": {"calibration_file": str(tmp_path / "calibration.yaml")},
        "maintenance_file": str(tmp_path / "maintenance.yaml"),
        "model_policy_file": str(tmp_path / "models.yaml"),
    }
    if rebind is not None:
        placement["rebind"] = rebind
    data: Dict[str, Any] = {
        "listen_host": "127.0.0.1",
        "listen_port": closed_port(),
        "log_level": "warning",
        "hosts": hosts,
        "discovery": {"interval_seconds": 3600.0, "timeout_seconds": 2.0},
        "placement": placement,
        "stats": {"file": str(tmp_path / "host_stats.json"), "persist": False},
        # The runtime fleet overlay (issue #70) — same rule, same reason.
        "fleet": {"file": str(tmp_path / "fleet.yaml"), "persist": False},
        "auth": auth if auth is not None else {"enabled": False,
                                               "keys_file": str(store)},
    }
    data["auth"].setdefault("keys_file", str(store))
    if sites:
        data["sites"] = sites
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def host_entry(name: str, url: str, *, node_id: Optional[str] = None,
               api_key: str = "stub-admin-key") -> Dict[str, Any]:
    entry: Dict[str, Any] = {"name": name, "addresses": [url], "api_key": api_key,
                             "max_inflight": 2, "max_threads": 4,
                             "spill_threshold": 1}
    if node_id:
        entry["node_id"] = node_id
    return entry


def run_doctor(config: Path, url: str, *extra: str) -> int:
    return doctor.main(["--config", str(config), "--url", url,
                        "--timeout", "2", *extra])


def dead_url() -> str:
    """A URL nothing serves: doctor must skip the proxy checks, not fail them."""
    return f"http://127.0.0.1:{closed_port()}"


def lines(out: str, name: str) -> List[str]:
    return [line for line in out.splitlines()
            if len(line.split()) > 1 and line.split()[1] == name]


def failed_lines(out: str) -> List[str]:
    """Report lines that are a FAIL — the summary's "0 FAIL" is not one."""
    return [line for line in out.splitlines() if line.startswith("FAIL ")]


def line_for(out: str, name: str) -> str:
    found = lines(out, name)
    assert found, f"no {name!r} line in:\n{out}"
    return found[0]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_doctor_is_green_against_a_live_proxy(live_proxy_factory, stub_factory,
                                              tmp_path, capsys) -> None:
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)],
                        chat_content="ok")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])
    url = live_proxy_factory(load_config(config))

    code = run_doctor(config, url)
    out = capsys.readouterr().out

    assert code == 0, out
    assert not failed_lines(out), out
    assert line_for(out, "python").startswith("OK")
    assert line_for(out, "config").startswith("OK")
    assert line_for(out, "node").startswith("OK")
    assert "answered in" in line_for(out, "node")
    assert "matches the config" in line_for(out, "identity")
    assert line_for(out, "admin").startswith("OK")
    assert line_for(out, "dashboard").startswith("OK")
    assert line_for(out, "completion").startswith("OK")
    assert "m1" in line_for(out, "completion")
    assert "Result: OK" in out


# ---------------------------------------------------------------------------
# The completion probe reads the answer (issue #82)
# ---------------------------------------------------------------------------
def test_an_empty_answer_is_a_finding_that_names_the_reasoning_channel(
        live_proxy_factory, stub_factory, tmp_path, capsys) -> None:
    """The cold start's finding 1: HTTP 200, `choices` there, `content` empty.

    A reasoning model spent the whole token budget on its reasoning channel, so
    the envelope looks healthy and the answer is nothing. Doctor used to call
    this OK because it only counted `choices`; the operator then blamed the
    client for the empty replies.
    """
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)],
                        chat_content="", chat_finish_reason="length")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])
    url = live_proxy_factory(load_config(config))

    code = run_doctor(config, url)
    out = capsys.readouterr().out

    assert code == 1, out
    line = line_for(out, "completion")
    assert line.startswith("FAIL"), line
    assert "the answer is empty" in line, line
    assert "reasoning" in line and "max_tokens" in line, line
    assert "Result: FAILED" in out


def test_an_empty_answer_that_stopped_by_itself_is_still_a_finding(
        live_proxy_factory, stub_factory, tmp_path, capsys) -> None:
    """Empty is empty: the verdict does not depend on the finish reason."""
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)],
                        chat_content="", chat_finish_reason="stop")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])
    url = live_proxy_factory(load_config(config))

    code = run_doctor(config, url)
    out = capsys.readouterr().out

    assert code == 1, out
    line = line_for(out, "completion")
    assert line.startswith("FAIL"), line
    assert "empty message" in line and "finish_reason stop" in line, line


def test_the_word_back_is_ok_and_the_probe_asks_for_it_with_room_to_spare(
        live_proxy_factory, stub_factory, tmp_path, capsys) -> None:
    """The same node with text is OK — and the probe's budget is not five tokens.

    The floor is the measurement, not a taste: the reasoning model needed 16
    tokens before a visible character appeared in three of three runs, so a
    probe that sends fewer can only produce a false finding.
    """
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)],
                        chat_content="OK.")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])
    url = live_proxy_factory(load_config(config))

    code = run_doctor(config, url)
    out = capsys.readouterr().out

    assert code == 0, out
    line = line_for(out, "completion")
    assert line.startswith("OK"), line
    assert "m1" in line
    # What doctor actually sent: the word, and enough budget for a reasoning
    # model to reach it.
    body = stub.state.chat_bodies[-1]
    assert "ok" in body["messages"][0]["content"].lower(), body
    assert body["max_tokens"] >= 16, body


def test_a_node_that_answers_something_else_warns_without_failing(
        live_proxy_factory, stub_factory, tmp_path, capsys) -> None:
    """Text came back, so the node serves — a WARN, naming what it said."""
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)],
                        chat_content="stub answer")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])
    url = live_proxy_factory(load_config(config))

    code = run_doctor(config, url)
    out = capsys.readouterr().out

    assert code == 0, out
    line = line_for(out, "completion")
    assert line.startswith("WARN"), line
    assert "stub answer" in line, line


def test_every_check_appears_once_per_subject(stub_factory, tmp_path,
                                              capsys) -> None:
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    for name in ("python", "config", "port", "node", "identity", "admin",
                 "keys", "dashboard", "completion"):
        assert len(lines(out, name)) == 1, f"{name}: {out}"


# ---------------------------------------------------------------------------
# A node that is gone
# ---------------------------------------------------------------------------
def test_stopped_node_fails_with_a_readable_reason(stub_factory, tmp_path,
                                                   capsys) -> None:
    stub = stub_factory()
    url = stub.url
    config = write_config(tmp_path, [host_entry("M3", url,
                                                node_id=stub.state.node_id)])
    stub.stop()

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 1, out
    node = line_for(out, "node")
    assert node.startswith("FAIL"), node
    assert "M3: unreachable" in node
    assert url in node
    # the dependent checks say so instead of failing a second time
    assert line_for(out, "identity").startswith("SKIP")
    assert line_for(out, "admin").startswith("SKIP")
    assert "Result: FAILED" in out


# ---------------------------------------------------------------------------
# Admin rights: the normal case for a stranger, so a warning
# ---------------------------------------------------------------------------
def test_missing_admin_rights_is_a_warning_not_a_failure(live_proxy_factory,
                                                         stub_factory, tmp_path,
                                                         capsys) -> None:
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)],
                        activity_status=401, chat_content="ok")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])
    url = live_proxy_factory(load_config(config))

    code = run_doctor(config, url)
    out = capsys.readouterr().out

    assert code == 0, out
    admin = line_for(out, "admin")
    assert admin.startswith("WARN"), admin
    assert "read-only, no admin rights" in admin
    assert not failed_lines(out), out


# ---------------------------------------------------------------------------
# The key store
# ---------------------------------------------------------------------------
def test_missing_keys_file_warns_and_names_the_path(stub_factory, tmp_path,
                                                    capsys) -> None:
    stub = stub_factory()
    keys = tmp_path / "state" / "keys.yaml"      # parent does not exist either
    config = write_config(
        tmp_path, [host_entry("M5", stub.url, node_id=stub.state.node_id)],
        auth={"enabled": True, "admin_key": "test-admin",
              "allow_loopback": True, "keys_file": str(keys)})

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    line = line_for(out, "keys")
    assert line.startswith("WARN"), line
    assert str(keys) in line
    assert "missing" in line
    assert "admin key set" in line


def test_missing_keys_file_is_fine_with_auth_off(stub_factory, tmp_path,
                                                 capsys) -> None:
    stub = stub_factory()
    keys = tmp_path / "keys.yaml"
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)],
                          auth={"enabled": False, "keys_file": str(keys)})

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    line = line_for(out, "keys")
    assert line.startswith("OK"), line
    assert str(keys) in line


def test_corrupt_keys_file_is_a_failure(stub_factory, tmp_path, capsys) -> None:
    stub = stub_factory()
    keys = tmp_path / "keys.yaml"
    keys.write_text("keys: [\n", encoding="utf-8")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)],
                          auth={"enabled": True, "keys_file": str(keys)})

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 1, out
    line = line_for(out, "keys")
    assert line.startswith("FAIL"), line
    assert str(keys) in line
    assert "YAML" in line


def test_world_readable_keys_file_warns_and_never_prints_a_key(stub_factory,
                                                               tmp_path,
                                                               capsys) -> None:
    stub = stub_factory()
    keys = tmp_path / "keys.yaml"
    secret = KeyStore(keys).create("cli").key
    os.chmod(keys, 0o644)
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)],
                          auth={"enabled": True, "keys_file": str(keys),
                                "admin_key": "test-admin"})

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    line = line_for(out, "keys")
    assert line.startswith("WARN"), line
    assert "0644" in line and "0600" in line
    assert "1 key(s)" in line
    assert secret not in out, "the key value must never reach the output"
    assert "test-admin" not in out, "the admin key must never reach the output"


def test_missing_admin_key_warns(stub_factory, tmp_path, capsys) -> None:
    stub = stub_factory()
    keys = tmp_path / "keys.yaml"
    KeyStore(keys).create("cli")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)],
                          auth={"enabled": True, "keys_file": str(keys)})

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    line = line_for(out, "keys")
    assert line.startswith("WARN"), line
    assert "no admin key" in line


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_node_id_mismatch_fails_and_names_both_values(stub_factory, tmp_path,
                                                      capsys) -> None:
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id="expected-node-id")])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 1, out
    line = line_for(out, "identity")
    assert line.startswith("FAIL"), line
    assert stub.state.node_id in line
    assert "expected-node-id" in line


def test_missing_node_id_in_the_config_warns(stub_factory, tmp_path,
                                            capsys) -> None:
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url)])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    line = line_for(out, "identity")
    assert line.startswith("WARN"), line
    assert "no node_id" in line


# ---------------------------------------------------------------------------
# Configuration and port
# ---------------------------------------------------------------------------
def test_missing_config_fails_and_names_the_path(tmp_path, capsys) -> None:
    missing = tmp_path / "nope.yaml"

    code = run_doctor(missing, dead_url())
    out = capsys.readouterr().out

    assert code == 1, out
    line = line_for(out, "config")
    assert line.startswith("FAIL"), line
    assert str(missing) in line
    # the node and key checks cannot run without a configuration
    assert line_for(out, "node").startswith("SKIP")
    assert line_for(out, "keys").startswith("SKIP")


def test_port_held_by_a_foreign_process_fails_naming_it(tmp_path,
                                                        capsys) -> None:
    config = write_config(tmp_path, [host_entry("M5", "http://127.0.0.1:9",
                                                node_id="unused")])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]

        code = run_doctor(config, f"http://127.0.0.1:{port}")
        out = capsys.readouterr().out

    assert code == 1, out
    line = line_for(out, "port")
    assert line.startswith("FAIL"), line
    assert "held by" in line
    # the proxy checks cannot run against a foreign listener
    assert line_for(out, "dashboard").startswith("SKIP")
    assert line_for(out, "completion").startswith("SKIP")


def test_free_port_is_ok(stub_factory, tmp_path, capsys) -> None:
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    line = line_for(out, "port")
    assert line.startswith("OK"), line
    assert "free" in line


# ---------------------------------------------------------------------------
# Rebinding without a prefill rate (issue #81)
# ---------------------------------------------------------------------------
def test_a_node_without_pp_tps_warns_that_rebind_cannot_fire(
        stub_factory, tmp_path, capsys) -> None:
    """`default_pp_tps: 0` means *not measured*, so no move can be priced.

    `plan_rebind` skips every destination it cannot price, so with this
    configuration rebinding is on and can never fire — silently, which is the
    failure the issue names. One node, one warning, and the warning says which
    field has to change.
    """
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out                 # a warning, not a broken install
    warned = [line for line in out.splitlines() if line.startswith("WARN ")]
    assert len(warned) == 1, out          # exactly one warning, not one per node
    line = warned[0]
    assert line.split()[1] == "rebind", line
    assert "rebind is on, but no destination has a prefill rate" in line
    assert "it cannot fire" in line
    assert "hosts[].pp_tps" in line
    assert "placement.rebind.default_pp_tps" in line


def test_a_measured_pp_tps_silences_the_rebind_warning(stub_factory, tmp_path,
                                                       capsys) -> None:
    stub = stub_factory()
    host = host_entry("M5", stub.url, node_id=stub.state.node_id)
    host["pp_tps"] = 3680.0
    config = write_config(tmp_path, [host])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    assert not [line for line in out.splitlines() if line.startswith("WARN ")], out
    line = line_for(out, "rebind")
    assert line.startswith("OK"), line
    assert "M5 can be priced" in line
    assert "every enabled node has a prefill rate" in line


def test_a_default_pp_tps_prices_every_node(stub_factory, tmp_path,
                                            capsys) -> None:
    """A non-zero default is a rate too — then nothing is missing."""
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)],
                          rebind={"default_pp_tps": 970.0})

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    assert not [line for line in out.splitlines() if line.startswith("WARN ")], out
    assert line_for(out, "rebind").startswith("OK")


def test_one_priced_destination_is_enough(stub_factory, tmp_path, capsys) -> None:
    """The issue's condition is *no* destination — one is a working rebind."""
    m5, m3 = stub_factory(), stub_factory()
    priced = host_entry("M5", m5.url, node_id=m5.state.node_id)
    priced["pp_tps"] = 3680.0
    unpriced = host_entry("M3", m3.url, node_id=m3.state.node_id)
    config = write_config(tmp_path, [priced, unpriced])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    assert not [line for line in out.splitlines() if line.startswith("WARN ")], out
    line = line_for(out, "rebind")
    assert line.startswith("OK"), line
    # the node that can never be a destination is named, not hidden
    assert "M5 can be priced" in line and "M3 has no prefill rate" in line


def test_rebind_switched_off_is_a_skip(stub_factory, tmp_path, capsys) -> None:
    """Nothing is silently missing when the feature is off."""
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)],
                          rebind={"enabled": False})

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    assert line_for(out, "rebind").startswith("SKIP")


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------
def test_json_report_is_machine_readable(stub_factory, tmp_path, capsys) -> None:
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])

    code = run_doctor(config, dead_url(), "--json")
    payload = json.loads(capsys.readouterr().out)

    assert payload["exit_code"] == code == 0
    names = [check["check"] for check in payload["checks"]]
    # `site` sits after `admin` (issue #67): it reads the address the probes
    # found, so it can only be answered once they are.
    assert names == ["python", "config", "rebind", "port", "node", "identity",
                     "admin", "site", "keys", "dashboard", "completion"]
    assert all(check["status"] in ("OK", "WARN", "FAIL", "SKIP")
               for check in payload["checks"])
    assert payload["summary"]["checks"] == len(payload["checks"])


def test_json_report_of_a_failure_still_parses(stub_factory, tmp_path,
                                               capsys) -> None:
    stub = stub_factory()
    url = stub.url
    config = write_config(tmp_path, [host_entry("M3", url,
                                                node_id=stub.state.node_id)])
    stub.stop()

    code = run_doctor(config, dead_url(), "--json")
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["exit_code"] == 1
    assert payload["summary"]["fail"] == 1
    failed = [c for c in payload["checks"] if c["status"] == "FAIL"]
    assert failed and failed[0]["check"] == "node"


# ---------------------------------------------------------------------------
# The reference path: a node reached over loopback (issue #76)
# ---------------------------------------------------------------------------
def test_init_then_doctor_skips_a_loopback_node_and_says_why(
        stub_factory, monkeypatch, tmp_path, capsys) -> None:
    """A fresh install's node is `127.0.0.1`: `SKIP` with the reason, not a lie.

    `ripple init` writes the `sites:` block but no `site:` on the node, because
    `_interface_networks` drops loopback — so nothing in the site's networks can
    contain it. Three answers were possible and only one is honest: an `OK`
    would claim a comparison that never happened (loopback is in every
    machine's network if it is in one), a `WARN` would blame the operator for
    the install `init` just wrote, and `SKIP` + "nothing was compared" is the
    fact. This drives the real `init` and the real `doctor`, not a description
    of them.
    """
    from ripple import init as init_module

    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    monkeypatch.setattr(init_module, "_interface_networks",
                        lambda: ["192.0.2.0/24"])
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)])
    config = tmp_path / "config.yaml"

    code = init_module.main(["--yes", "--no-check",
                             "--port", str(closed_port()),
                             "--home-dir", str(tmp_path),
                             "--config", str(config),
                             "--host", stub.url])
    assert code == 0, capsys.readouterr().out

    cfg = load_config(config)
    assert [site.id for site in cfg.sites] == ["local"]
    assert cfg.sites[0].networks == ["192.0.2.0/24"]
    assert cfg.hosts[0].site == "", "a loopback node must not carry a site:"

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out
    site_line = line_for(out, "site")

    assert code == 0, out
    assert site_line.startswith("SKIP"), site_line
    assert "reached over loopback" in site_line, site_line
    assert "nothing was compared" in site_line, site_line
    # the address that answered is the one the line names
    assert "127.0.0.1" in site_line, site_line


def test_a_hand_written_site_on_a_loopback_node_is_unconfirmed_not_contradicted(
        stub_factory, tmp_path, capsys) -> None:
    """`site:` on a loopback node used to be a `WARN`; it is a `SKIP` (issue #76).

    The declaration is not contradicted by `127.0.0.1` — loopback is in no
    network, so there is nothing to contradict it with. Reporting `WARN` there
    turned the normal single-machine install into a complaint; reporting `OK`
    would be worse. The line says "unconfirmed" and names the reason.
    """
    stub = stub_factory()
    entry = host_entry("local", stub.url, node_id=stub.state.node_id)
    entry["site"] = "local"
    config = write_config(tmp_path, [entry],
                          sites=[{"id": "local", "name": "local",
                                  "networks": ["192.0.2.0/24"]}])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out
    site_line = line_for(out, "site")

    assert code == 0, out
    assert site_line.startswith("SKIP"), site_line
    assert "declares site local (local)" in site_line, site_line
    assert "loopback" in site_line and "unconfirmed" in site_line, site_line
