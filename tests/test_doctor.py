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
                 rebind: Optional[Dict[str, Any]] = None,
                 replication: Optional[Dict[str, Any]] = None,
                 listen_port: Optional[int] = None) -> Path:
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
        "listen_port": listen_port if listen_port is not None else closed_port(),
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
    # What `ripple init` writes since #159: the oMLX key under the second name
    # the runtime reads, so the per-host state sampling has a key. `replication={}`
    # is the pre-#159 (and hand-written) shape: only `defaults.api_key` set.
    data["replication"] = ({"admin_key": "stub-admin-key"} if replication is None
                           else replication)
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


def test_admin_rights_without_the_sampling_key_are_not_reported_as_ok(
        live_proxy_factory, stub_factory, tmp_path, capsys) -> None:
    """Issue #159: doctor and the start warning have to agree.

    The node answers `/admin/api/activity` with 200 for `defaults.api_key`, so
    doctor can prove admin rights — but the per-host state sampling reads
    `replication.admin_key`, which this configuration does not have. Reporting
    `OK` here was the contradiction the operator saw: a green doctor and an empty
    fleet-card history on the same file.
    """
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)], chat_content="ok")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)],
                          replication={})
    url = live_proxy_factory(load_config(config))

    code = run_doctor(config, url)
    out = capsys.readouterr().out

    assert code == 0, out                  # a limitation, not a broken install
    admin = line_for(out, "admin")
    assert admin.startswith("WARN"), admin
    assert "HTTP 200" in admin             # the fact doctor measured
    assert "replication.admin_key" in admin
    assert "state curves stay empty" in admin
    assert not failed_lines(out), out


def test_the_installed_config_reports_admin_rights_as_ok(live_proxy_factory,
                                                         stub_factory, tmp_path,
                                                         capsys) -> None:
    """With the key `init` now writes, the admin line is OK again — no caveat."""
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)], chat_content="ok")
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])
    url = live_proxy_factory(load_config(config))

    code = run_doctor(config, url)
    out = capsys.readouterr().out

    assert code == 0, out
    admin = line_for(out, "admin")
    assert admin.startswith("OK"), admin
    assert "admin rights (HTTP 200 on /admin/api/activity)" in admin
    assert "state curves" not in admin


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
    failure the issue names. Two nodes (one node is `SKIP`: a move needs a
    destination, issue #176), one warning, and the warning says which field has
    to change.
    """
    m5, m3 = stub_factory(), stub_factory()
    config = write_config(tmp_path, [
        host_entry("M5", m5.url, node_id=m5.state.node_id),
        host_entry("M3", m3.url, node_id=m3.state.node_id)])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out                 # a warning, not a broken install
    warned = [line for line in out.splitlines()
              if line.startswith("WARN ") and "rebind" in line]
    assert len(warned) == 1, out          # exactly one warning, not one per node
    line = warned[0]
    assert line.split()[1] == "rebind", line
    assert "rebind is on, but no destination has a prefill rate" in line
    assert "it cannot fire" in line
    assert "hosts[].pp_tps" in line
    assert "placement.rebind.default_pp_tps" in line


def test_a_measured_pp_tps_silences_the_rebind_warning(stub_factory, tmp_path,
                                                       capsys) -> None:
    m5, m3 = stub_factory(), stub_factory()
    a = host_entry("M5", m5.url, node_id=m5.state.node_id)
    a["pp_tps"] = 3680.0
    b = host_entry("M3", m3.url, node_id=m3.state.node_id)
    b["pp_tps"] = 1200.0
    config = write_config(tmp_path, [a, b])

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    # The `secrets` line is a different finding (issue #179) and is expected
    # here: these fixtures carry a literal key on purpose.
    assert not [line for line in out.splitlines()
                if line.startswith("WARN ") and "rebind" in line], out
    line = line_for(out, "rebind")
    assert line.startswith("OK"), line
    assert "M5, M3 can be priced" in line
    assert "every enabled node has a prefill rate" in line


def test_a_default_pp_tps_prices_every_node(stub_factory, tmp_path,
                                            capsys) -> None:
    """A non-zero default is a rate too — then nothing is missing."""
    m5, m3 = stub_factory(), stub_factory()
    config = write_config(tmp_path, [
        host_entry("M5", m5.url, node_id=m5.state.node_id),
        host_entry("M3", m3.url, node_id=m3.state.node_id)],
        rebind={"default_pp_tps": 970.0})

    code = run_doctor(config, dead_url())
    out = capsys.readouterr().out

    assert code == 0, out
    # The `secrets` line is a different finding (issue #179) and is expected
    # here: these fixtures carry a literal key on purpose.
    assert not [line for line in out.splitlines()
                if line.startswith("WARN ") and "rebind" in line], out
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
    # The `secrets` line is a different finding (issue #179) and is expected
    # here: these fixtures carry a literal key on purpose.
    assert not [line for line in out.splitlines()
                if line.startswith("WARN ") and "rebind" in line], out
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
    assert names == ["python", "config", "rebind", "secrets", "plist", "port",
                     "node", "identity",
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


# ---------------------------------------------------------------------------
# An empty `${VAR}` gets a line of its own (issue #153)
# ---------------------------------------------------------------------------
def test_empty_variable_is_its_own_line_not_a_folded_warning(tmp_path, monkeypatch):
    """`_check_config` folds warnings into "warning: <first> (+N more)".

    The empty key must not be folded: the operator has to read "the variable is
    empty" before the 401 from every node, or they conclude RIPPLE lost the key
    and start over. Same sentence `load_config` appends, by construction.
    """
    monkeypatch.delenv("UNSET_VAR_FOR_TEST", raising=False)
    path = write_config(tmp_path, [
        host_entry("alpha", "http://127.0.0.1:1", api_key="${UNSET_VAR_FOR_TEST}")])
    cfg = load_config(path)

    checks = doctor._env_checks(cfg)
    assert len(checks) == 1
    check = checks[0]
    assert (check.name, check.status) == ("config env", doctor.WARN)
    assert "hosts[0].api_key" in check.reason
    assert "UNSET_VAR_FOR_TEST" in check.reason

    report = doctor.Report(config_path=str(path), url="http://127.0.0.1:8010")
    report.add(check)
    line = [ln for ln in doctor.render(report).splitlines() if "config env" in ln]
    assert len(line) == 1, doctor.render(report)
    assert "UNSET_VAR_FOR_TEST" in line[0]


# ---------------------------------------------------------------------------
# The proxy URL is the configured one (issue #162)
# ---------------------------------------------------------------------------
def test_without_url_the_proxy_is_the_configured_listen_port(
        stub_factory, tmp_path, capsys) -> None:
    """An install on 8013 must be checked on 8013, not on a fixed 8010.

    Doctor used to carry `http://127.0.0.1:8010` as the default and derive the
    port check from it, so a wheel install with `listen_port: 8013` read
    "port 8010 … is free" and skipped dashboard and completion with "no RIPPLE
    on http://127.0.0.1:8010" while the instance was serving on 8013.
    """
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)],
                          listen_port=8013)

    doctor.main(["--config", str(config), "--timeout", "2"])
    out = capsys.readouterr().out

    # The header line names the instance that was actually checked.
    assert "proxy: http://127.0.0.1:8013" in out, out
    assert "8013" in line_for(out, "port"), out
    # The checks that need a proxy point at the same instance.
    assert "http://127.0.0.1:8013" in line_for(out, "dashboard"), out
    assert "8010" not in out.splitlines()[1], out


def test_an_explicit_url_wins_over_the_configured_port(
        stub_factory, tmp_path, capsys) -> None:
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)],
                          listen_port=8013)
    url = dead_url()

    run_doctor(config, url, "--json")
    payload = json.loads(capsys.readouterr().out)

    port = next(c for c in payload["checks"] if c["check"] == "port")
    assert payload["url"] == url
    assert port["detail"]["port"] == doctor._host_port(url)[1]
    assert "8013" not in payload["url"]


def test_an_unloadable_config_falls_back_to_the_default_url(
        tmp_path, capsys) -> None:
    """No port to read means the old default, not a crash before the checks.

    The config line has to report the broken file, so the remaining checks must
    still run — and they need some URL to run against.
    """
    path = tmp_path / "broken.yaml"
    path.write_text("listen_port: not-a-port\nhosts: []\n", encoding="utf-8")

    code = doctor.main(["--config", str(path), "--timeout", "2", "--json"])
    payload = json.loads(capsys.readouterr().out)

    config_check = next(c for c in payload["checks"] if c["check"] == "config")
    port = next(c for c in payload["checks"] if c["check"] == "port")
    assert payload["url"] == "http://127.0.0.1:8010"
    assert config_check["status"] == doctor.FAIL
    assert port["detail"]["port"] == 8010
    assert code == 1

# -- is this file safe to share? (issue #179) ------------------------------
def test_a_literal_credential_in_the_config_is_named_as_one(
        stub_factory, tmp_path, capsys):
    """The file an operator pastes into a forum carries the key with it."""
    stub = stub_factory()
    config = write_config(tmp_path, [host_entry("M5", stub.url,
                                                node_id=stub.state.node_id)])

    code = run_doctor(config, stub.url, "--json")
    report = json.loads(capsys.readouterr().out)
    line = {c["check"]: c for c in report["checks"]}["secrets"]

    assert code in (0, 1)
    assert line["status"] == "WARN", line
    assert "hosts[M5].api_key" in line["reason"], line["reason"]
    assert "ripple init --force" in line["reason"], line["reason"]
    assert "secrets.yaml" in line["reason"], line["reason"]


def test_a_reference_is_not_a_credential(stub_factory, tmp_path, capsys):
    """`api_key_file` is what `init` writes — the file is shareable again."""
    stub = stub_factory()
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("omlx:\n  fleet: k-from-file\n", encoding="utf-8")
    entry = host_entry("M5", stub.url, node_id=stub.state.node_id, api_key="")
    entry["api_key_file"] = f"{secrets}#omlx.fleet"
    # …and the same shape under the second name the runtime reads (#159/#179).
    config = write_config(tmp_path, [entry],
                          replication={"admin_key_file": f"{secrets}#omlx.fleet"})

    run_doctor(config, stub.url, "--json")
    report = json.loads(capsys.readouterr().out)
    line = {c["check"]: c for c in report["checks"]}["secrets"]

    assert line["status"] == "OK", line
    assert "no credential in this file" in line["reason"], line["reason"]

# --- the launchd plist and the key in it (issue #185) ----------------------

def _plist_at(tmp_path, monkeypatch, text: str):
    """Point the service module at a plist in tmp_path."""
    from ripple import service
    path = tmp_path / "ai.ripple-test.plist"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(service, "plist_path", lambda lab=None: path)
    monkeypatch.setattr(service, "label", lambda: "ai.ripple-test")
    return path


def test_a_plist_with_a_key_nobody_needs_is_a_warning(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("hosts:\n  - name: a\n    addresses: [http://127.0.0.1:8000]\n"
                   "defaults:\n  api_key_file: ~/.ripple/secrets.yaml#omlx.fleet\n",
                   encoding="utf-8")
    _plist_at(tmp_path, monkeypatch,
              "<plist><dict><key>EnvironmentVariables</key><dict>"
              "<key>OMLX_API_KEY</key><string>secret</string></dict></dict></plist>")
    check = doctor._check_plist(str(cfg))
    assert check.status == doctor.WARN
    assert "carries the oMLX key in plain text" in check.reason
    assert check.detail["next"] == "ripple -perm"


def test_a_plist_without_a_key_is_ok_when_the_config_names_its_own(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("defaults:\n  api_key_file: ~/.ripple/secrets.yaml#omlx.fleet\n",
                   encoding="utf-8")
    _plist_at(tmp_path, monkeypatch, "<plist><dict><key>PATH</key></dict></plist>")
    check = doctor._check_plist(str(cfg))
    assert check.status == doctor.OK
    assert "no key inside" in check.reason


def test_a_plist_missing_the_key_the_config_asks_for_is_a_warning(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("defaults:\n  api_key: ${OMLX_API_KEY}\n", encoding="utf-8")
    _plist_at(tmp_path, monkeypatch, "<plist><dict><key>PATH</key></dict></plist>")
    check = doctor._check_plist(str(cfg))
    assert check.status == doctor.WARN
    assert "no OMLX_API_KEY" in check.reason


def test_no_plist_is_a_skip(tmp_path, monkeypatch):
    from ripple import service
    monkeypatch.setattr(service, "plist_path", lambda lab=None: tmp_path / "none.plist")
    check = doctor._check_plist(str(tmp_path / "config.yaml"))
    assert check.status == doctor.SKIP
    assert "ripple -perm" in check.reason
