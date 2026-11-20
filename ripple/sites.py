"""Sites as data (issue #67): named locations, their networks, and the check.

`hosts[].site` used to be a free string — `SA`, `SB` — that nothing looked at.
The M5 declared `site: SA` and listed `http://192.0.2.11:8000` first, but the
machine stands in site-b and answers on `198.51.100.49`; RIPPLE only ran
because the loopback probe failed first. A declaration nobody checks is a label,
not data.

So a site is a block with an id and the networks it contains:

    sites:
      - id: sa
        name: site-a
        networks: [192.0.2.0/24]

A host names a site by id (`hosts[].site: sa`). The claim is then checkable: the
address a node is *actually* reached through is matched against the networks,
and a contradiction is one `ripple doctor` line (plus a hint on the fleet card).

Three rules this module keeps deliberately:

* **The site is a location, not a link.** `site` says where the machine stands.
  Which address RIPPLE uses is decided by reachability and the configured order
  (`HostRuntime.resolve`), never by the site — otherwise a host with a
  Thunderbolt bridge and no address on it would be reached over a link that does
  not exist.
* **Placement never reads a site.** Two hosts in two locations with the same
  weights are balanced exactly as two hosts in one location; a site is not a
  routing rule and there is no "prefer the local site".
* **Loopback places nothing** (issue #76). A node reached over `127.0.0.1` is
  this machine, and "this machine" is not a network fact: loopback is in no
  site's networks, and putting it into one would make every site contain every
  machine's loopback — an `OK` that confirms nothing. So `ripple init` writes no
  `site:` on such a node and `ripple doctor` reports `SKIP` *and says why*
  (`is_local_address`). No location is invented and no comparison is claimed.

Backwards compatible: `site: SA` as a plain string still parses (it is just an
id nothing defines, which is one warning and not an error), and a host without a
site is a host without a claim.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Sequence, Union
from urllib.parse import urlsplit

#: A parsed network — `ipaddress` is the parser, not a hand-written mask check.
#: `Union` and not `A | B`: this line runs at import, and Python 3.9 (the floor
#: in `pyproject.toml`) has no `type.__or__`.
Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


@dataclass(frozen=True)
class SiteConfig:
    """One named location: an id, a human name and the networks it contains."""

    id: str
    name: str = ""
    networks: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """What a report shows: the name when there is one, else the id."""
        return self.name or self.id

    def parsed(self) -> list[Network]:
        """The networks as `ipaddress` objects (already validated at load)."""
        return [ipaddress.ip_network(text, strict=False) for text in self.networks]

    def contains(self, address: Address | str | None) -> bool:
        if address is None:
            return False
        ip = ip_of(address) if isinstance(address, str) else address
        if ip is None:
            return False
        return any(ip.version == net.version and ip in net for net in self.parsed())

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "networks": list(self.networks)}


def parse_networks(raw: Any, *, where: str) -> list[str]:
    """Validate a site's network list. Returns canonical CIDR text.

    CIDR, not an address/mask pair: one token, `ipaddress` parses it, and a typo
    is refused here instead of silently matching nothing later. The failure mode
    of an unparsed network is the one this issue exists for — a declaration that
    looks checked and is not.
    """
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{where}: 'networks' must be a list of CIDR networks "
                         f"(e.g. 192.0.2.0/24)")
    out: list[str] = []
    for entry in raw:
        text = str(entry or "").strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"{where}: {text!r} is not a CIDR network "
                             f"(write the prefix, e.g. 192.0.2.0/24)") from exc
        canonical = str(net)
        if canonical not in out:
            out.append(canonical)
    return out


def ip_of(address: Any) -> Address | None:
    """The IP inside an address as RIPPLE writes it, or None.

    Accepts `http://10.0.0.5:8000`, `10.0.0.5:8000`, `10.0.0.5` and
    `http://[::1]:8000`; `localhost` is read as `127.0.0.1` because that is what
    it resolves to on the machine that runs RIPPLE. A name that is not localhost
    is None: resolving it here would make the answer depend on DNS at check time.
    """
    text = str(address or "").strip()
    if not text:
        return None
    if "://" in text:
        host = urlsplit(text).hostname or ""
    else:
        host = text
        if host.startswith("["):
            host = host.split("]")[0].lstrip("[")
        elif host.count(":") == 1:                 # host:port, not an IPv6 literal
            host = host.split(":", 1)[0]
    host = host.strip("[]").strip()
    if not host:
        return None
    if host.lower() == "localhost":
        host = "127.0.0.1"
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_local_address(address: Any) -> bool:
    """True when the address can only mean "this machine" — loopback.

    `localhost` counts too (`ip_of` reads it as `127.0.0.1`), and `::1` as well.
    A site is a claim about a *network*; loopback is not one a site can honestly
    contain, so a node reached over it cannot be placed (issue #76): the caller
    reports the fact instead of deriving a location from it.
    """
    ip = ip_of(address)
    return ip is not None and ip.is_loopback


def site_by_id(sites: Sequence[SiteConfig], site_id: Any) -> SiteConfig | None:
    text = str(site_id or "").strip()
    if not text:
        return None
    for site in sites or ():
        if site.id == text:
            return site
    return None


def site_for_address(sites: Sequence[SiteConfig], address: Any) -> SiteConfig | None:
    """The site whose networks contain `address` — first match in config order.

    Config order decides an overlap, and an overlap is reported once at load
    (`config.py`), so this never has to guess in silence.
    """
    ip = ip_of(address)
    if ip is None:
        return None
    for site in sites or ():
        if site.contains(ip):
            return site
    return None


def site_id_for_address(sites: Sequence[SiteConfig], address: Any) -> str:
    """The derived site id for an address, `""` when no network contains it."""
    site = site_for_address(sites, address)
    return site.id if site is not None else ""


def overlapping_pairs(sites: Sequence[SiteConfig]) -> list[tuple[str, str]]:
    """Pairs of *different* sites whose networks overlap.

    Not an error — an operator may split one network into two locations on
    purpose — but it makes the derivation order-dependent, which is worth one
    warning instead of a silent first-match.
    """
    out: list[tuple[str, str]] = []
    parsed = [(site.id, site.parsed()) for site in sites or ()]
    for i, (left_id, left) in enumerate(parsed):
        for right_id, right in parsed[i + 1:]:
            if left_id == right_id:
                continue
            if any(a.overlaps(b) for a in left for b in right
                   if a.version == b.version):
                out.append((left_id, right_id))
    return out


def network_summary(site: SiteConfig | None, limit: int = 3) -> str:
    """`192.0.2.0/24, 198.51.100.0/24` — for a one-line report."""
    if site is None or not site.networks:
        return "no networks"
    shown = ", ".join(site.networks[:limit])
    if len(site.networks) > limit:
        shown += f" (+{len(site.networks) - limit} more)"
    return shown
