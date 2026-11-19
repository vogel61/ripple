"""Sites as data (issue #67): the block, the derivation, the check.

The state of the world before this issue: `hosts[].site` was a free string that
nothing looked at. The M5 declared `site: WD` and listed `192.0.2.11` first;
the machine stands in Traiskirchen and answers on `198.51.100.49`. RIPPLE ran only
because the loopback probe failed first.

These tests hold the four properties that make the declaration checkable:

* the block parses and is validated (`ipaddress` checks the CIDR, not a
  hand-written mask reader);
* the site of an address is *derived* from the address RIPPLE actually uses;
* a contradiction is exactly one `ripple doctor` warning naming both sites;
* placement never reads a site — same weights, two locations, same decision.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from ripple import doctor
from ripple.config import ConfigError, HostConfig, load_config
from ripple.replication import (LocalSide, configured_ssh_targets,
                                reachable_endpoint, ssh_target_for)
from ripple.sites import SiteConfig, ip_of, site_for_address, site_id_for_address

from tests.support import (chat_body, closed_port, host_config, host_row,
                           make_config, prompt_for, stub_model_loaded)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def write_config(tmp_path: Path, hosts: list[dict[str, Any]], *,
                 sites: Any = None) -> Path:
    """A configuration file with every machine-state path inside `tmp_path`."""
    data: dict[str, Any] = {
        "listen_host": "127.0.0.1",
        "listen_port": closed_port(),
        "log_level": "warning",
        "hosts": hosts,
        "discovery": {"interval_seconds": 3600.0, "timeout_seconds": 2.0},
        "placement": {
            "loading": {"calibration_file": str(tmp_path / "calibration.yaml")},
            "maintenance_file": str(tmp_path / "maintenance.yaml"),
            "model_policy_file": str(tmp_path / "models.yaml"),
        },
        "stats": {"file": str(tmp_path / "host_stats.json"), "persist": False},
        "auth": {"enabled": False, "keys_file": str(tmp_path / "keys.yaml")},
    }
    if sites is not None:
        data["sites"] = sites
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def entry(name: str, url: str, *, site: str = "", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "addresses": [url], "weight": 1}
    if site:
        row["site"] = site
    row.update(extra)
    return row


SITES = [
    {"id": "wd", "name": "Wien", "networks": ["192.0.2.0/24"]},
    {"id": "tr", "name": "Traiskirchen", "networks": ["198.51.100.0/24"]},
]


# ---------------------------------------------------------------------------
# 1. The block parses and is validated
# ---------------------------------------------------------------------------
def test_the_sites_block_is_read_and_its_networks_are_canonical(tmp_path):
    path = write_config(tmp_path, [entry("a", "http://192.0.2.5:8000", site="wd")],
                        sites=[{"id": "wd", "name": "Wien",
                                "networks": ["192.0.2.7/24", "198.51.100.0/24"]}])
    cfg = load_config(path)
    assert [s.id for s in cfg.sites] == ["wd"]
    site = cfg.sites[0]
    assert site.name == "Wien"
    # canonical: the host bits are gone, so the same network written twice is
    # the same network (and a comparison later is not a string comparison)
    assert site.networks == ["192.0.2.0/24", "198.51.100.0/24"]
    assert cfg.hosts[0].site == "wd"
    assert cfg.site_of(cfg.hosts[0]) is site


def test_a_bare_string_site_still_parses_and_is_one_warning(tmp_path):
    """The old world: `site: WD` with no block. Not an error — a warning."""
    path = write_config(tmp_path, [entry("a", "http://127.0.0.1:8000", site="WD")])
    cfg = load_config(path)
    assert cfg.hosts[0].site == "WD"
    assert cfg.sites == []
    assert cfg.site_of(cfg.hosts[0]) is None
    matching = [w for w in cfg.warnings if "site 'WD'" in w]
    assert len(matching) == 1
    assert "not defined" in matching[0]


def test_a_host_without_a_site_is_silent(tmp_path):
    path = write_config(tmp_path, [entry("a", "http://127.0.0.1:8000", site="")])
    cfg = load_config(path)
    assert cfg.hosts[0].site == ""
    assert [w for w in cfg.warnings if "site" in w] == []


def test_a_network_that_is_not_cidr_is_refused(tmp_path):
    """A mask pair is the shape this issue rules out: `ipaddress` refuses it."""
    path = write_config(tmp_path, [entry("a", "http://127.0.0.1:8000")],
                        sites=[{"id": "wd", "networks": ["192.0.2.0 255.255.255.0"]}])
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "192.0.2.0 255.255.255.0" in str(exc.value)
    assert "CIDR" in str(exc.value)


def test_a_site_needs_an_id_and_a_unique_one(tmp_path):
    path = write_config(tmp_path, [entry("a", "http://127.0.0.1:8000")],
                        sites=[{"name": "Wien", "networks": ["192.0.2.0/24"]}])
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "'id' missing" in str(exc.value)

    path = write_config(tmp_path, [entry("a", "http://127.0.0.1:8000")],
                        sites=[{"id": "wd", "networks": ["192.0.2.0/24"]},
                               {"id": "wd", "networks": ["198.51.100.0/24"]}])
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "duplicate site id" in str(exc.value)


def test_a_site_without_networks_is_a_warning_not_an_error(tmp_path):
    path = write_config(tmp_path, [entry("a", "http://127.0.0.1:8000", site="wd")],
                        sites=[{"id": "wd", "name": "Wien"}])
    cfg = load_config(path)
    assert cfg.sites[0].networks == []
    assert any("site wd: no networks" in w for w in cfg.warnings)


def test_overlapping_sites_are_reported_once(tmp_path):
    path = write_config(tmp_path, [entry("a", "http://127.0.0.1:8000")],
                        sites=[{"id": "wd", "networks": ["192.0.2.0/24"]},
                               {"id": "tr", "networks": ["192.0.2.128/25"]}])
    cfg = load_config(path)
    overlap = [w for w in cfg.warnings if "overlap" in w]
    assert len(overlap) == 1
    assert "wd" in overlap[0] and "tr" in overlap[0]


# ---------------------------------------------------------------------------
# 2. Derivation: address → site
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("address, expected", [
    ("http://192.0.2.5:8000", "wd"),
    ("https://192.0.2.5", "wd"),
    ("192.0.2.5:8000", "wd"),
    ("192.0.2.5", "wd"),
    ("198.51.100.7:8000", "tr"),
    ("http://203.0.113.9:8000", ""),        # inside no network
    ("", ""),
    (None, ""),
])
def test_the_site_of_an_address_is_derived(address, expected):
    sites = [SiteConfig(id=s["id"], name=s["name"], networks=s["networks"])
             for s in SITES]
    assert site_id_for_address(sites, address) == expected


def test_localhost_and_ipv6_are_understood():
    sites = [SiteConfig(id="local", networks=["127.0.0.0/8"]),
             SiteConfig(id="v6", networks=["::1/128"])]
    assert ip_of("http://localhost:8000").is_loopback
    assert site_for_address(sites, "http://localhost:8000").id == "local"
    assert site_for_address(sites, "http://[::1]:8000").id == "v6"


def test_a_name_that_is_not_localhost_is_not_resolved():
    """DNS at check time would make the answer depend on something else."""
    assert ip_of("http://node.example.com:8000") is None


# ---------------------------------------------------------------------------
# 3. The doctor check: one warning with both names
# ---------------------------------------------------------------------------
def run_doctor(config: Path) -> doctor.Report:
    """Doctor against a dead proxy port: only the config and node checks run."""
    url = f"http://127.0.0.1:{closed_port()}"
    return asyncio.run(doctor.diagnose(str(config), url, 2.0))


def site_checks(report: doctor.Report) -> list[doctor.Check]:
    return [c for c in report.checks if c.name == "site"]


def checks_for(config: Path, address: str, name: str = "M5-alpha") -> list[doctor.Check]:
    """The site checks for one node, reached through `address`.

    A stub can only bind `127.0.0.1`, and since issue #76 loopback is not an
    address a site can place a node in — so the "reached through" address is
    handed to the check directly instead of being faked with a loopback stub.
    `doctor._site_checks` is the function `doctor` itself runs.
    """
    cfg = load_config(config)
    host = next(h for h in cfg.hosts if h.name == name)
    return doctor._site_checks(cfg, [doctor.NodeProbe(host=host, address=address)])


def test_a_contradiction_is_exactly_one_warning_with_both_names(tmp_path):
    """The M5 case: declares WD, is reached in Traiskirchen."""
    config = write_config(tmp_path, [entry("M5-alpha", "http://203.0.113.9:8000",
                                           site="wd")],
                          sites=[{"id": "wd", "name": "Wien",
                                  "networks": ["192.0.2.0/24"]},
                                 {"id": "tr", "name": "Traiskirchen",
                                  "networks": ["203.0.113.0/24"]}])

    checks = checks_for(config, "http://203.0.113.9:8000")
    assert len(checks) == 1
    check = checks[0]
    assert check.status == doctor.WARN
    assert "Wien" in check.reason and "Traiskirchen" in check.reason
    assert "wd" in check.reason and "tr" in check.reason
    assert check.detail["declared"] == "wd"
    assert check.detail["derived"] == "tr"


def test_a_matching_site_is_ok(tmp_path):
    config = write_config(tmp_path, [entry("M5-alpha", "http://192.0.2.5:8000",
                                           site="wd")],
                          sites=[{"id": "wd", "name": "Wien",
                                  "networks": ["192.0.2.0/24"]}])

    checks = checks_for(config, "http://192.0.2.5:8000")
    assert [c.status for c in checks] == [doctor.OK]
    assert "as declared" in checks[0].reason


def test_an_address_in_no_network_is_reported_as_unconfirmed(tmp_path):
    config = write_config(tmp_path, [entry("M5-alpha", "http://198.51.100.9:8000",
                                           site="wd")],
                          sites=[{"id": "wd", "name": "Wien",
                                  "networks": ["192.0.2.0/24"]}])

    checks = checks_for(config, "http://198.51.100.9:8000")
    assert [c.status for c in checks] == [doctor.WARN]
    assert "192.0.2.0/24" in checks[0].reason
    assert checks[0].detail["derived"] is None


def test_no_declared_site_is_one_skip(tmp_path):
    config = write_config(tmp_path, [entry("a", "http://127.0.0.1:8000")])
    report = run_doctor(config)
    checks = site_checks(report)
    assert [c.status for c in checks] == [doctor.SKIP]


# ---------------------------------------------------------------------------
# 4. /healthz carries both locations
# ---------------------------------------------------------------------------
async def test_healthz_carries_the_declared_and_the_derived_site(stub_factory,
                                                                ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    cfg = make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1, site="wd"),
        host_config("B", b.url, node_id=b.state.node_id, weight=1, site="tr"),
    ], sites=[SiteConfig(id="wd", name="Wien", networks=["127.0.0.1/32"]),
              SiteConfig(id="tr", name="Traiskirchen",
                         networks=["198.51.100.0/24"])])
    harness = await ripple_factory(cfg)
    health = await harness.healthz()

    row_a = host_row(health, "A")
    assert row_a["site"] == "wd"                 # declared
    assert row_a["site_name"] == "Wien"
    assert row_a["site_derived"] == "wd"         # derived from the used address
    assert row_a["site_mismatch"] is False

    row_b = host_row(health, "B")
    assert row_b["site"] == "tr"
    assert row_b["site_derived"] == "wd"         # 127.0.0.1 is in wd
    assert row_b["site_mismatch"] is True
    assert row_b["site_derived_name"] == "Wien"


async def test_healthz_without_sites_keeps_the_old_fields(stub_factory,
                                                         ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1, site="WD")]))
    row = host_row(await harness.healthz(), "A")
    assert row["site"] == "WD"                   # the old free string
    assert row["site_derived"] is None
    assert row["site_mismatch"] is False


# ---------------------------------------------------------------------------
# 5. Placement never reads a site
# ---------------------------------------------------------------------------
async def _distribution(cfg: Any, count: int = 6) -> list[str]:
    from ripple.proxy import Ripple
    import httpx

    ripple = Ripple(cfg)
    await ripple.fleet.start(warmup=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=ripple.app),
                               base_url="http://ripple", timeout=30.0)
    hosts: list[str] = []
    try:
        for i in range(count):
            resp = await client.post("/v1/chat/completions",
                                     json=chat_body("m1", prompt_for(i), turns=2))
            assert resp.status_code == 200, resp.text
            hosts.append(resp.headers["x-ripple-host"])
    finally:
        await client.aclose()
        await ripple.fleet.stop()
        await ripple.proxy_client.aclose()
    return hosts


async def test_two_locations_do_not_change_a_single_decision(stub_factory):
    """Same weights, same models, two locations: the same hosts, in order.

    A site is where a machine stands, not a preference. If placement ever read
    it, the second run would prefer one of the two hosts — the shape of a rule
    that would also point a copy at a bridge without an address.
    """
    def fleet() -> list[Any]:
        a = stub_factory(models=[stub_model_loaded("m1")])
        b = stub_factory(models=[stub_model_loaded("m1")])
        return [host_config("A", a.url, node_id=a.state.node_id, weight=3),
                host_config("B", b.url, node_id=b.state.node_id, weight=2)]

    plain = await _distribution(make_config(fleet()))
    located_hosts = fleet()
    located_hosts[0].site = "wd"
    located_hosts[1].site = "tr"
    located = await _distribution(make_config(
        located_hosts,
        sites=[SiteConfig(id="wd", name="Wien", networks=["192.0.2.0/24"]),
               SiteConfig(id="tr", name="Traiskirchen",
                          networks=["198.51.100.0/24"])]))

    assert located == plain


def test_placement_does_not_touch_the_site_field():
    """The static half of the same statement: no code path in placement.py."""
    source = (ROOT / "ripple" / "placement.py").read_text(encoding="utf-8")
    assert re.search(r"\.site\b", source) is None
    assert "sites" not in re.sub(r"#.*", "", source)


# ---------------------------------------------------------------------------
# 6. Replication addresses are an ordered list
# ---------------------------------------------------------------------------
def test_a_single_replication_address_still_parses(tmp_path):
    path = write_config(tmp_path, [entry(
        "a", "http://192.0.2.1:8000",
        replication={"address": "192.0.2.10", "model_roots": ["/models"]})])
    cfg = load_config(path)
    assert cfg.hosts[0].replication.address == ["192.0.2.10"]
    assert cfg.hosts[0].replication.ssh_target == "192.0.2.10"
    assert cfg.summary()["hosts"][0]["replication"]["address"] == ["192.0.2.10"]


def test_the_order_of_the_replication_addresses_is_kept(tmp_path):
    path = write_config(tmp_path, [entry(
        "a", "http://192.0.2.1:8000",
        replication={"address": ["192.0.2.10", "198.51.100.10"],
                     "ssh_user": "operator", "model_roots": ["/models"]})])
    cfg = load_config(path)
    rep = cfg.hosts[0].replication
    assert rep.address == ["192.0.2.10", "198.51.100.10"]
    assert configured_ssh_targets(cfg.hosts[0], cfg) == ["operator@192.0.2.10",
                                                         "operator@198.51.100.10"]
    assert ssh_target_for(cfg.hosts[0], cfg) == "operator@192.0.2.10"


class _Transport:
    """Stands in for the SSH transport: only the probe answers."""

    def __init__(self, answers: dict[str, tuple[bool, str | None]]) -> None:
        self.answers = answers
        self.tried: list[str] = []

    async def probe(self, ep: Any) -> tuple[bool, str | None]:
        self.tried.append(ep.ssh_target)
        return self.answers.get(ep.ssh_target, (False, "no route to host"))


def _replication_cfg(tmp_path: Path, addresses: list[str]) -> Any:
    path = write_config(tmp_path, [entry(
        "a", "http://192.0.2.1:8000",
        replication={"address": addresses, "model_roots": ["/models"]})])
    return load_config(path)


def test_the_first_reachable_address_wins(tmp_path):
    cfg = _replication_cfg(tmp_path, ["192.0.2.10", "198.51.100.10"])
    transport = _Transport({"192.0.2.10": (False, "no address assigned"),
                            "198.51.100.10": (True, None)})
    ep, ok, error, tried = asyncio.run(
        reachable_endpoint(transport, cfg.hosts[0], cfg))
    assert ok is True and error is None
    assert ep.ssh_target == "198.51.100.10"
    assert transport.tried == ["192.0.2.10", "198.51.100.10"]
    assert tried == ["192.0.2.10 (no address assigned)"]


def test_no_address_answering_is_one_refusal_naming_them_all(tmp_path):
    cfg = _replication_cfg(tmp_path, ["192.0.2.10", "198.51.100.10"])
    transport = _Transport({})
    ep, ok, error, tried = asyncio.run(
        reachable_endpoint(transport, cfg.hosts[0], cfg))
    assert ok is False
    assert ep.ssh_target == "192.0.2.10"         # the intended one, named
    assert len(tried) == 2
    assert error is not None
    assert "192.0.2.10" in error and "198.51.100.10" in error


def test_this_machine_needs_no_ssh_destination(tmp_path):
    """The loopback host is the local side, whatever its address list says."""
    path = write_config(tmp_path, [entry(
        "local", "http://127.0.0.1:8000",
        replication={"address": ["192.0.2.10"], "model_roots": ["/models"]})])
    cfg = load_config(path)
    assert ssh_target_for(cfg.hosts[0], cfg) == ""
    assert configured_ssh_targets(cfg.hosts[0], cfg) == ["192.0.2.10"]
    assert LocalSide(name="local", ignored_address="192.0.2.10") is not None


# ---------------------------------------------------------------------------
# 7. The example configuration is neutral
# ---------------------------------------------------------------------------
def test_the_example_config_carries_one_neutral_site():
    cfg = load_config(ROOT / "config.example.yaml")
    assert [s.id for s in cfg.sites] == ["local"]
    assert cfg.sites[0].networks == ["127.0.0.0/8", "192.0.2.0/24"]
    assert cfg.hosts[0].site == "local"
    assert cfg.site_of(cfg.hosts[0]) is cfg.sites[0]


def test_no_private_network_in_the_tracked_example():
    """A tracked file must not carry the operator's LAN (issue #67, point 4).

    `10.0.0.0/8` and `192.168.0.0/16` in a published example are somebody's real
    network sooner or later; the example uses loopback and the documentation
    range (RFC 5737) instead.
    """
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    private = re.findall(r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)\b", text)
    assert private == []


def test_the_example_parses_without_a_site_warning():
    cfg = load_config(ROOT / "config.example.yaml")
    assert [w for w in cfg.warnings if "site" in w] == []


# ---------------------------------------------------------------------------
# 8. `ripple init` derives the network and asks for a name
# ---------------------------------------------------------------------------
def test_init_writes_the_derived_network_and_the_name(tmp_path, monkeypatch):
    from ripple import init as init_module

    monkeypatch.setattr(init_module, "_interface_networks",
                        lambda: ["192.0.2.0/24"])
    port = closed_port()
    assert init_module.main([
        "--yes", "--port", str(port), "--home-dir", str(tmp_path),
        "--config", str(tmp_path / "config.yaml"),
        "--host", "http://192.0.2.5:1", "--site-name", "Wien West",
        "--no-check"]) == 0

    cfg = load_config(tmp_path / "config.yaml")
    assert [s.id for s in cfg.sites] == ["wien-west"]
    assert cfg.sites[0].name == "Wien West"
    assert cfg.sites[0].networks == ["192.0.2.0/24"]
    # the node is inside the network, so it gets the id — and only then
    assert cfg.hosts[0].site == "wien-west"
    assert [w for w in cfg.warnings if "site" in w] == []


def test_init_leaves_a_node_outside_the_network_without_a_site(tmp_path, monkeypatch):
    from ripple import init as init_module

    monkeypatch.setattr(init_module, "_interface_networks",
                        lambda: ["192.0.2.0/24"])
    assert init_module.main([
        "--yes", "--port", str(closed_port()), "--home-dir", str(tmp_path),
        "--config", str(tmp_path / "config.yaml"),
        "--host", "http://127.0.0.1:1", "--no-check"]) == 0

    cfg = load_config(tmp_path / "config.yaml")
    assert [s.id for s in cfg.sites] == ["local"]
    assert cfg.hosts[0].site == ""