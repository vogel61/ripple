# Sites: where a node stands (issue #67)

`hosts[].site` used to be a free string. The M5 declared `site: SA` and listed its LAN address first; the machine
stands in site-b and is reached over a different network. RIPPLE ran only because the loopback probe failed
first — the declaration was never wrong *in the program*, because nothing read
it.

A site is data now: a name plus the networks it contains. The claim is checked
against the address a node is **actually** reached through.

## The block

```yaml
sites:
  - id: sa
    name: site-a
    networks:
      - 192.0.2.0/24
      - 203.0.113.0/29
  - id: sb
    name: site-b
    networks:
      - 198.51.100.0/24

hosts:
  - name: alpha
    site: sa
    addresses:
      - http://192.0.2.5:8000
```

* `id` is what a host names; it is required and unique.
* `networks` are **CIDR** (`ipaddress` parses them — an address/mask pair is
  refused, because a network that silently matches nothing is the failure this
  issue is about). `192.0.2.7/24` is stored as `192.0.2.0/24`.
* A site without networks is a warning: a name nobody can check.
* Two sites whose networks overlap are a warning: the first site in the file
  wins, which is worth knowing.

Backwards compatible: `site: SA` as a plain string still parses. If no `sites:`
block defines `SA`, that is **one warning**, not an error — the configuration
keeps loading, and the old behaviour (a label nobody reads) is at least said out
loud.

## What is checked

`ripple doctor` adds one line per host:

```
OK   site       alpha: http://192.0.2.5:8000 is in site-a (sa), as declared
WARN site       alpha: declares site-a (sa), but http://198.51.100.9:8000
                is reached in site-b (sb) — the node is not where
                the configuration says
WARN site       alpha: declares site-a (sa), but http://203.0.113.9:8000
                is in none of its networks (192.0.2.0/24)
SKIP site       beta: no site declared
```

Three different answers, on purpose: confirmed, contradicted (both sites named),
and unconfirmed (usually a missing network). `SKIP` when the node is unreachable —
the address is the fact the check needs — and `SKIP` naming the loopback when the
address that answered is `127.0.0.1`: that node is the machine `doctor` runs on, a
site that contained loopback would contain every machine's loopback, so nothing
is compared and the line says so instead of reporting an `OK` (issue #76). A
`site:` written on such a node is unconfirmed there, not contradicted.

`/healthz` carries the same two facts per host — `site` (declared),
`site_derived`, `site_mismatch`, `site_unconfirmed` — and the fleet card shows a
hint when they disagree. The page never invents a location; it repeats what the
proxy measured.

## What a site is *not*

* **Not a link.** `site` says where the machine stands. Which address RIPPLE
  uses is decided by reachability and the configured order
  (`HostRuntime.resolve`). A site rule would point a copy at the Thunderbolt
  bridge while the bridge has no address — measured 17.11.2026: the bridge interface carried a
  self-assigned link-local address.
* **Not a routing rule.** Placement never reads a site: two hosts with the same
  weights are balanced the same way whether they stand in one room or two. There
  is no "prefer the local site" and there will not be one by accident — a test
  holds it.
* **Not a replacement for `addresses`.** `hosts[].addresses` stays the probing
  order. `hosts[].replication.address` is an ordered list: the link the operator
  wants first (a bridge), the fallback after it (the LAN). The first entry that
  answers is used, and a plan that had to fall back says so.

## `ripple init`

`init` derives the local network from the interface that carries the default
route (`ifconfig` on macOS, `ip -o -4 addr` on Linux; loopback and link-local are
dropped) and asks for a name, default `local`. A node whose address is inside the
network gets `site: local`; a node outside it stays without a site instead of
inheriting a claim `doctor` would then have to contradict.

Nothing internal is written into a tracked file: `config.example.yaml` carries
exactly one site, `local`, with `127.0.0.0/8` and the documentation range
`192.0.2.0/24` (RFC 5737).
