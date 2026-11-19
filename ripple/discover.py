"""Find oMLX nodes without knowing their addresses (issue #61).

`ripple init` cannot ask the operator for an address list; the first run has to
find something to talk to. This module is that first run: it probes addresses it
is *given*, and sweeps a network only when the operator passes `--scan CIDR` — a
stranger's LAN must not be swept unasked.

The probe is deliberately the same one `ripple/hosts.py` uses for identity:
`GET /api/cluster/node_id`. Reachability alone is not evidence that an oMLX node
answers, so a port that accepts a connection and then says nothing is not a node
(`_probe` returns nothing for it). The module talks HTTP with `httpx` like the
rest of RIPPLE and does not import the `Ripple`/`Fleet` machinery: discovery runs
before there is a config to build a fleet from.

Three facts about the live oMLX build shape the probe (measured 16.11.2026 on
0.8.0rc1):

* `/api/cluster/node_id` answers **without** a key — so identity is free.
* `/v1/models` answers **401** without one. That is not a failure of the probe:
  a node that says 401 is a node, and the operator then learns they still owe
  a key. `Found.models` stays empty and the reason is reported by `main`.
* `/admin/api/models` is the admin endpoint; 401/403/404 there means "no admin
  rights", not "not a node".

Timeouts are per address and never fatal: a silent host costs at most `timeout`
seconds and the rest of the run continues.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx

# The three endpoints the probe needs, named once so a test and the docs agree.
IDENTITY_PATH = "/api/cluster/node_id"
MODELS_PATH = "/v1/models"
ADMIN_PATH = "/admin/api/models"

DEFAULT_ADDRESS = "http://127.0.0.1:8000"
DEFAULT_PORT = 8000
DEFAULT_TIMEOUT = 1.0
DEFAULT_CONCURRENCY = 64

# A sweep is a diagnostic, not a mapping service. /16 is the largest range worth
# waiting for at 64 in flight; anything wider is almost certainly a typo (a /8
# would be 16 million probes and hours of traffic).
MAX_SCAN_ADDRESSES = 65536

# Reverse DNS is a convenience for the suggested name, never a requirement. The
# lookup runs in a daemon thread so a resolver that hangs cannot hold the run
# open — the thread is abandoned and the name falls back to the node_id.
REVERSE_DNS_BUDGET = 0.3


@dataclass(frozen=True)
class Found:
    """One reachable oMLX node, as `ripple init` needs it.

    `models` is empty both when the node carries nothing and when it refused to
    tell us (no key yet — oMLX answers 401 on `/v1/models`). The two are not the
    same statement, so `main` reports the reason; `discover()` keeps the pinned
    shape and callers that need the distinction should pass a key.
    """

    address: str            # "http://127.0.0.1:8000" — normalised, no trailing slash
    node_id: Optional[str]  # from GET /api/cluster/node_id; None if the node has none
    models: Tuple[str, ...]
    latency_ms: float       # measured round trip of the identity probe
    name: str               # suggested host name, e.g. "local", or the node's hostname
    admin: bool             # True if an admin endpoint answered with 200


@dataclass(frozen=True)
class _Probe:
    """A `Found` plus why its model list is empty — CLI detail, not public API."""

    found: Found
    reason: Optional[str]


# -- addresses ------------------------------------------------------------
def normalise(address: str) -> str:
    """`10.0.0.1:8000/` → `http://10.0.0.1:8000`.

    The scheme is assumed to be http: oMLX speaks plain HTTP on the LAN, and a
    bare `host:port` is what an operator types. Trailing slashes go because every
    path in this module is absolute.
    """
    text = address.strip()
    if not text:
        raise ValueError("empty address")
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.hostname:
        raise ValueError(f"not an address: {address!r}")
    return text.rstrip("/")


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _reverse_name(host: str) -> Optional[str]:
    """The host's own name, if the resolver knows one within the budget.

    A stranger's LAN usually has no PTR records and some resolvers take seconds
    to say so, so the lookup is bounded and its thread is a daemon: a stuck
    resolver loses its thread, not the run.
    """
    box: List[str] = []

    def work() -> None:
        try:
            name = socket.gethostbyaddr(host)[0]
        except OSError:
            return
        if name:
            box.append(name)

    thread = threading.Thread(target=work, name="ripple-discover-rdns", daemon=True)
    thread.start()
    thread.join(REVERSE_DNS_BUDGET)
    if not box:
        return None
    return box[0]


def suggest_name(address: str, node_id: Optional[str]) -> str:
    """A host name the operator can keep: `local`, a DNS name, or a node_id stub.

    Loopback is `local` because that is what it is — the node on this machine,
    and the name every example config uses for it. Nothing here is a claim about
    the node's real hostname; it is a suggestion for `ripple init` to offer.
    """
    host = urlparse(address).hostname or ""
    if _is_loopback(host):
        return "local"
    name = _reverse_name(host)
    if name:
        return name.split(".")[0].lower()
    if node_id:
        return "node-" + node_id.split("-")[0].lower()
    return "node-" + host.lower().replace(".", "-").replace(":", "-")


# -- probing ---------------------------------------------------------------
def _headers(api_key: Optional[str]) -> dict:
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def _status_reason(response: httpx.Response, path: str) -> str:
    phrase = response.reason_phrase or ""
    return f"{response.status_code} {phrase} on {path}".replace("  ", " ").strip()


async def _read_models(client: httpx.AsyncClient, address: str, timeout: float,
                       api_key: Optional[str]
                       ) -> Tuple[Tuple[str, ...], Optional[str], Optional[int]]:
    """The node's model ids, plus the reason and HTTP status when they are empty.

    The status is what tells `_probe` whether a failed model list is still
    evidence of an oMLX node (401: the node wants a key) or evidence against one
    (404: no such endpoint here).
    """
    try:
        r = await client.get(address + MODELS_PATH, headers=_headers(api_key),
                             timeout=timeout)
    except httpx.TimeoutException:
        return (), f"timeout on {MODELS_PATH}", None
    except httpx.HTTPError as exc:
        return (), f"{type(exc).__name__} on {MODELS_PATH}", None
    if r.status_code in (401, 403):
        return (), _status_reason(r, MODELS_PATH) + " — pass --omlx-key", r.status_code
    if r.status_code != 200:
        return (), _status_reason(r, MODELS_PATH), r.status_code
    try:
        body = r.json()
    except ValueError:
        return (), f"unreadable JSON on {MODELS_PATH}", r.status_code
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return (), f"no 'data' list on {MODELS_PATH}", r.status_code
    ids = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("id"):
            model_id = str(entry["id"])
            if model_id not in ids:
                ids.append(model_id)
    return tuple(ids), None, r.status_code


async def _read_admin(client: httpx.AsyncClient, address: str, timeout: float,
                      api_key: Optional[str]) -> bool:
    """Does an admin endpoint answer 200 for this key?

    401/403/404 all mean "no admin rights" — the endpoint is absent on a build
    that does not have it, and the key is not an admin key on one that does.
    Neither is an error about the node, so neither is reported as one.
    """
    try:
        r = await client.get(address + ADMIN_PATH, headers=_headers(api_key),
                             timeout=timeout)
    except httpx.HTTPError:
        return False
    return r.status_code == 200


async def _probe(client: httpx.AsyncClient, address: str, timeout: float,
                 api_key: Optional[str]) -> Optional[_Probe]:
    """Probe one address; None if nothing that looks like an oMLX node answers."""
    t0 = time.perf_counter()
    try:
        identity = await client.get(address + IDENTITY_PATH, headers=_headers(api_key),
                                    timeout=timeout)
    except httpx.HTTPError:
        # A refused connect, a timeout, a closed port: not a node. Nothing else
        # is asked — a silent address must cost one timeout, not three.
        return None
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    node_id = None
    identity_ok = identity.status_code == 200
    if identity_ok:
        try:
            body = identity.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("node_id"):
            node_id = str(body["node_id"])
    elif identity.status_code >= 500:
        # An answering server that fails its own identity endpoint is not a node
        # we can use, and a 5xx on every scanned port would be noise.
        return None

    models, reason, model_status = await _read_models(client, address, timeout, api_key)
    if not identity_ok:
        # No identity endpoint (an older build, a 401, a 404). The model list is
        # then the only evidence: an oMLX node answers it 200 with a `data` list,
        # or 401 because it wants a key. A random web server answers 404, or 200
        # with a body that is not a model list.
        if model_status == 200:
            evidence = reason is None
        else:
            evidence = model_status in (401, 403)
        if not evidence:
            return None

    admin = await _read_admin(client, address, timeout, api_key)
    found = Found(address=address, node_id=node_id, models=models,
                  latency_ms=latency_ms, name=suggest_name(address, node_id),
                  admin=admin)
    return _Probe(found=found, reason=reason)


async def _probe_all(addresses: Sequence[str], *, timeout: float,
                     api_key: Optional[str],
                     concurrency: int) -> List[_Probe]:
    """Probe every address, at most `concurrency` at a time, in input order."""
    sem = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient(follow_redirects=False,
                                 limits=httpx.Limits(
                                     max_connections=max(8, concurrency + 8),
                                     max_keepalive_connections=8)) as client:

        async def one(address: str) -> Optional[_Probe]:
            async with sem:
                return await _probe(client, address, timeout, api_key)

        results = await asyncio.gather(*(one(a) for a in addresses))
    return [r for r in results if r is not None]


# -- public API ------------------------------------------------------------
async def discover(addresses: List[str], *, timeout: float = 1.0,
                   api_key: Optional[str] = None) -> List[Found]:
    """Probe exactly the given addresses; never look anywhere else.

    An address that does not answer within `timeout` is skipped, not raised:
    discovery runs on a first start, where a wrong guess in the list is normal.
    Results keep the input order.
    """
    probes = await _probe_detailed(addresses, timeout=timeout, api_key=api_key,
                                   concurrency=len(addresses) or 1)
    return [p.found for p in probes]


def _unique_by_identity(probes: Sequence[_Probe]) -> List[_Probe]:
    """Drop repeats of the same node, keeping the first probe of it.

    Identity is the node_id (`hosts.py`'s rule): `localhost:8000` and
    `127.0.0.1:8000` are the same machine, and a node that answers on two
    interfaces must not be offered twice to `ripple init`. A node without a
    node_id cannot be matched and is kept as it is — two addressless nodes are
    indistinguishable, and silently dropping one would be worse.
    """
    out: List[_Probe] = []
    seen = set()
    for probe in probes:
        node_id = probe.found.node_id
        if node_id:
            if node_id in seen:
                continue
            seen.add(node_id)
        out.append(probe)
    return out


async def _probe_detailed(addresses: Sequence[str], *, timeout: float,
                          api_key: Optional[str],
                          concurrency: int) -> List[_Probe]:
    """`discover` with the empty-model reasons kept for the CLI."""
    norm: List[str] = []
    for address in addresses:
        candidate = normalise(address)
        if candidate not in norm:
            norm.append(candidate)
    probes = await _probe_all(norm, timeout=timeout, api_key=api_key,
                              concurrency=concurrency)
    return _unique_by_identity(probes)


def _scan_addresses(cidr: str, port: int) -> List[str]:
    network = ipaddress.ip_network(cidr, strict=False)
    if network.num_addresses > MAX_SCAN_ADDRESSES:
        raise ValueError(
            f"{cidr} holds {network.num_addresses} addresses; the largest range "
            f"worth sweeping is {MAX_SCAN_ADDRESSES} (a /16)")
    out = []
    for host in network.hosts():
        text = str(host)
        out.append(f"http://[{text}]:{port}" if host.version == 6
                   else f"http://{text}:{port}")
    return out


async def scan_network(cidr: str, *, port: int = 8000, timeout: float = 0.5,
                       concurrency: int = 64,
                       api_key: Optional[str] = None) -> List[Found]:
    """Sweep `cidr` for oMLX nodes on `port`. Only ever called behind `--scan`.

    A sweep is traffic on someone's network, so this function is not on any
    default path: `main` reaches it only through the explicit flag, and a test
    points it at 127.0.0.1/32.
    """
    addresses = _scan_addresses(cidr, port)
    probes = await _probe_all(addresses, timeout=timeout, api_key=api_key,
                              concurrency=concurrency)
    probes = _unique_by_identity(probes)
    # Numeric order, so two runs report the same list.
    probes.sort(key=lambda p: ipaddress.ip_address(
        urlparse(p.found.address).hostname or "0.0.0.0"))
    return [p.found for p in probes]


# -- CLI -------------------------------------------------------------------
def _format_line(probe: _Probe) -> str:
    found = probe.found
    models = f"models={len(found.models)}"
    if probe.reason:
        models += f" ({probe.reason})"
    return (f"{found.address:<28} node_id={found.node_id or '-'}  {models}  "
            f"latency={found.latency_ms:.1f}ms  admin={'yes' if found.admin else 'no'}  "
            f"name={found.name}")


def _format_json(probes: Sequence[_Probe]) -> str:
    payload = []
    for probe in probes:
        found = probe.found
        payload.append({
            "address": found.address,
            "node_id": found.node_id,
            "models": list(found.models),
            "latency_ms": found.latency_ms,
            "name": found.name,
            "admin": found.admin,
            # Not part of `Found`: why `models` is empty, when it is not because
            # the node carries nothing (a missing key answers 401).
            "reason": probe.reason,
        })
    return json.dumps(payload, indent=2)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ripple discover",
        description="Find oMLX nodes. Probes the given addresses; sweeps a "
                    "network only with --scan.")
    parser.add_argument("--address", action="append", default=[], metavar="URL",
                        help="address to probe, e.g. http://10.0.0.5:8000 "
                             "(repeatable; default http://127.0.0.1:8000)")
    parser.add_argument("--scan", action="append", default=[], metavar="CIDR",
                        help="also sweep this network on --port (repeatable); "
                             "off unless given")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port for --scan (default {DEFAULT_PORT})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"seconds per probe (default {DEFAULT_TIMEOUT})")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"parallel probes while scanning (default "
                             f"{DEFAULT_CONCURRENCY})")
    parser.add_argument("--omlx-key", default=None, metavar="KEY",
                        help="oMLX API key (default: $OMLX_API_KEY)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="machine-readable output")
    args = parser.parse_args(argv)

    api_key = args.omlx_key if args.omlx_key is not None else os.environ.get("OMLX_API_KEY")
    api_key = api_key or None

    try:
        addresses = [normalise(a) for a in (args.address or [DEFAULT_ADDRESS])]
    except ValueError as exc:
        parser.error(str(exc))
    # A bad CIDR is rejected before anything is probed: the localhost pass must
    # not run first only to end in an argument error.
    for cidr in args.scan:
        try:
            _scan_addresses(cidr, args.port)
        except ValueError as exc:
            parser.error(str(exc))

    async def run() -> List[_Probe]:
        probes = await _probe_detailed(addresses, timeout=args.timeout,
                                       api_key=api_key,
                                       concurrency=max(1, len(addresses)))
        seen = {p.found.address for p in probes}
        for cidr in args.scan:
            if not args.as_json:
                print(f"scanning {cidr} on port {args.port}", file=sys.stderr)
            try:
                found = await scan_network(cidr, port=args.port,
                                           timeout=args.timeout,
                                           concurrency=args.concurrency,
                                           api_key=api_key)
            except ValueError as exc:
                parser.error(str(exc))
            for f in found:
                if f.address in seen:
                    continue
                seen.add(f.address)
                probes.append(_Probe(found=f, reason=None))
        # The same node can be reached as localhost and as 127.0.0.1; the
        # explicitly given addresses stay first.
        return _unique_by_identity(probes)

    probes = asyncio.run(run())

    if args.as_json:
        print(_format_json(probes))
    else:
        for probe in probes:
            print(_format_line(probe))
        if not probes:
            print("no oMLX node answered" +
                  ("" if api_key else " (no API key set: OMLX_API_KEY or --omlx-key)"),
                  file=sys.stderr)
    return 0 if probes else 1


if __name__ == "__main__":
    raise SystemExit(main())
