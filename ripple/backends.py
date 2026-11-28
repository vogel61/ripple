"""Backend adapter contract (§6.2, FA-23).

RIPPLE does not talk to oMLX directly but through one adapter per backend
type. Every adapter declares what it can really do; capabilities are probed
**against the running backend** at startup, not against the configuration. If a
capability is missing, the requirement depending on it switches off visibly —
no silent degradation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

ALL_CAPABILITIES = ("availability", "residency", "capacity", "lifecycle",
                    "identity", "prefix_cache")

#: How long a *connect* may take before an address counts as unreachable.
#: A refused connect answers at once; a blackholed one costs the whole timeout,
#: and `HostRuntime.resolve()` walks the address list — measured 20.11.2026:
#: 5.00 s per unreachable address, i.e. 15 s per pass over the four alpha
#: addresses (issue #117). A LAN connect is milliseconds, so the cap is the
#: difference between "unreachable" and "slow" and nothing else; the *read*
#: timeout keeps `timeout`, because a node in a cold load answers late.
CONNECT_TIMEOUT_SECONDS = 1.5


@dataclass
class Health:
    healthy: bool
    version: str | None = None
    ceiling_bytes: int | None = None
    used_bytes: int | None = None
    loaded_count: int | None = None
    model_count: int | None = None
    rtt_ms: float | None = None
    error: str | None = None


@dataclass
class ModelInfo:
    id: str
    available: bool = True
    loaded: bool = False
    pinned: bool = False
    estimated_size: int | None = None
    actual_size: int | None = None
    context_len: int | None = None
    vision: bool = False
    # oMLX's own word for what serves the id (`/v1/models/status`): `llm`, `vlm`,
    # `markitdown`… Kept raw and uninterpreted — the transport parses, the fleet
    # decides what an engine type means (`Host.model_builtin`, issue #118). An
    # empty string is "the node did not say", never "an ordinary model".
    engine_type: str = ""
    # What the node says the id *is*, from `/v1/models` (`llm`, `embedding`,
    # `vlm`…). Kept apart from `engine_type` on purpose: `engine_type` names the
    # engine that serves the id, `model_type` the request class — and a model is
    # routed as a utility model by the latter (issue #193). Empty is "the node
    # did not say", never "an ordinary llm".
    model_type: str = ""
    last_access: float | None = None
    # oMLX's own loading signals (`/v1/models/status`). None = the backend does
    # not report them, which is not the same as "not loading" — a caller must
    # treat None as "unknown", never as false.
    is_loading: bool | None = None
    loading_started_at: float | None = None   # epoch seconds, oMLX's clock
    # The model this row is a *profile* of (`X:agent`, `X:think-1m`). oMLX
    # builds a profile row by copying the source model's status dict, so the
    # row repeats the base engine's `actual_size` although it occupies no
    # memory of its own — the measured sum has to know which rows are real
    # (issue #195). Empty means "a real engine": only the payload's own field
    # is trusted, never the id's shape.
    source_model_id: str = ""
    # oMLX's statement that **this node is the coordinator** of a parallel
    # deployment (issue #194). The deployment record exists only on rank 0 and
    # is never replicated to peers, so a worker reports `false` for the very
    # same model — the flag says "coordinator here", never "no deployment
    # anywhere". Measured 25.11.2026 on oMLX 0.8.3: every row was false.
    distributed: bool = False
    # oMLX's own `cluster` object from `/v1/models/status`: `deployment_id`,
    # `world_size`, `tensor_parallel_size`, `pipeline_stages`, `strategy`,
    # `backend`, `target_context_tokens`, `profile`. Passed through untouched —
    # the keys are the engine's vocabulary, and renaming one here would make
    # this no longer "what the node said". None = the node did not report it.
    cluster: dict | None = None


@dataclass
class Capacity:
    ceiling_bytes: int
    used_bytes: int
    model_count: int | None = None
    loaded_count: int | None = None


class Backend:
    """The contract from §6.2. Every method may return None."""

    name: str = "backend"
    capabilities: frozenset[str] = frozenset()

    def __init__(self, base_url: str, api_key: str = "", *,
                 client: httpx.AsyncClient, timeout: float = 5.0,
                 connect_timeout: float | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client
        self.timeout = timeout
        # One cap for every adapter: the connect is the phase an unreachable
        # address burns, and the resolution pays it once per address in the
        # list (issue #117).
        cap = CONNECT_TIMEOUT_SECONDS if connect_timeout is None else connect_timeout
        self.connect_timeout = min(float(timeout), float(cap))

    # -- helpers -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json"}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        return h

    async def _get(self, path: str, timeout: float | None = None) -> Any:
        limit = timeout or self.timeout
        # Connect capped, read at `limit`: an unreachable address must cost the
        # cap and not the whole probe budget (issue #117).
        r = await self.client.get(self.base_url + path, headers=self._headers(),
                                  timeout=httpx.Timeout(limit, connect=self.connect_timeout))
        r.raise_for_status()
        return r.json()

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    # -- contract ----------------------------------------------------------
    async def health(self) -> Health:
        raise NotImplementedError

    async def models(self) -> list[ModelInfo]:
        raise NotImplementedError

    async def residency(self) -> dict[str, ModelInfo] | None:
        return None

    async def capacity(self) -> Capacity | None:
        return None

    async def identity(self) -> str | None:
        return None

    async def load(self, model_id: str, *, timeout: float = 600.0) -> bool:
        return False

    async def unload(self, model_id: str) -> bool:
        return False


class OmlxBackend(Backend):
    """oMLX 0.8.x — all six capabilities, verified against the live instance."""

    name = "omlx"

    async def health(self) -> Health:
        t0 = time.perf_counter()
        try:
            data = await self._get("/health")
        except (httpx.HTTPError, ValueError) as exc:
            return Health(healthy=False, error=f"{type(exc).__name__}: {exc}")
        rtt = (time.perf_counter() - t0) * 1000.0
        pool = (data or {}).get("engine_pool") or {}
        return Health(
            healthy=str((data or {}).get("status", "")).lower() == "healthy",
            ceiling_bytes=_as_int(pool.get("final_ceiling")),
            used_bytes=_as_int(pool.get("current_model_memory")),
            loaded_count=_as_int(pool.get("loaded_count")),
            model_count=_as_int(pool.get("model_count")),
            rtt_ms=round(rtt, 2),
        )

    async def models(self) -> list[ModelInfo]:
        data = await self._get("/v1/models")
        out: list[ModelInfo] = []
        for m in (data or {}).get("data") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            out.append(ModelInfo(
                id=str(m["id"]),
                available=True,
                context_len=_as_int(m.get("max_model_len")),
                vision=str(m.get("model_type", "")).lower() in ("vlm", "vision"),
                engine_type=str(m.get("engine_type") or ""),
                model_type=str(m.get("model_type") or ""),
            ))
        return out

    async def residency(self) -> dict[str, ModelInfo] | None:
        """`/v1/models/status` is the only source carrying `estimated_size`,
        `pinned` and `last_access` together (note: the key is `models`, not
        `data` — a wrong key looks like "nothing loaded")."""
        try:
            data = await self._get("/v1/models/status")
        except (httpx.HTTPError, ValueError):
            return None
        models = (data or {}).get("models")
        if not isinstance(models, list):
            return None
        out: dict[str, ModelInfo] = {}
        for m in models:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            out[str(m["id"])] = ModelInfo(
                id=str(m["id"]),
                available=True,
                loaded=bool(m.get("loaded")),
                pinned=bool(m.get("pinned")),
                estimated_size=_as_int(m.get("estimated_size")),
                actual_size=_as_int(m.get("actual_size")),
                context_len=_as_int(m.get("max_context_window") or m.get("model_context_length")),
                vision=str(m.get("engine_type", "")).lower() in ("vlm", "vision")
                or str(m.get("model_type", "")).lower() in ("vlm", "vision"),
                engine_type=str(m.get("engine_type") or ""),
                # Some oMLX versions carry the field here too. When they do,
                # this is the fresher answer and `Host.model_type()` prefers it;
                # when they do not, the list map kept by the host still answers
                # (issue #193).
                model_type=str(m.get("model_type") or ""),
                last_access=_as_float(m.get("last_access")),
                is_loading=_as_bool_opt(m.get("is_loading")),
                loading_started_at=_as_float(m.get("loading_started_at")),
                # Only a profile row carries this; a real engine does not
                # (issue #195).
                source_model_id=str(m.get("source_model_id") or ""),
                # `/v1/models/status` is the only endpoint that carries the
                # cluster facts, so they are read here and nowhere else
                # (issue #194). A missing flag is not a coordinator, and a
                # `cluster` that is not an object is no cluster at all —
                # both stay at their defaults instead of guessing.
                distributed=bool(m.get("distributed")),
                cluster=(m.get("cluster")
                         if isinstance(m.get("cluster"), dict) else None),
            )
        return out

    async def capacity(self) -> Capacity | None:
        h = await self.health()
        if h.ceiling_bytes is None or h.used_bytes is None:
            return None
        return Capacity(ceiling_bytes=h.ceiling_bytes, used_bytes=h.used_bytes,
                        model_count=h.model_count, loaded_count=h.loaded_count)

    async def identity(self) -> str | None:
        try:
            data = await self._get("/api/cluster/node_id")
        except (httpx.HTTPError, ValueError):
            return None
        if isinstance(data, dict):
            nid = data.get("node_id")
            return str(nid) if nid else None
        return None

    async def version(self) -> str | None:
        try:
            data = await self._get("/api/cluster/node_id")
        except (httpx.HTTPError, ValueError):
            return None
        if isinstance(data, dict) and data.get("version"):
            return str(data["version"])
        return None

    async def load(self, model_id: str, *, timeout: float = 600.0) -> bool:
        try:
            r = await self.client.post(f"{self.base_url}/v1/models/{model_id}/load",
                                       headers=self._headers(), timeout=timeout)
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    async def unload(self, model_id: str) -> bool:
        try:
            r = await self.client.post(f"{self.base_url}/v1/models/{model_id}/unload",
                                       headers=self._headers(), timeout=120.0)
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    async def lifecycle_available(self) -> bool:
        """Check whether the load route exists without loading a model: OPTIONS
        on an existing route answers 405 (Method Not Allowed), on a missing one
        404."""
        try:
            r = await self.client.request("OPTIONS",
                                          f"{self.base_url}/v1/models/x/load",
                                          headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError:
            return False
        return r.status_code != 404


class OpenAICompatibleBackend(Backend):
    """Proves the boundary: `availability` only, everything else explicitly not.

    It exists not to be used but to make FA-23 measurable — RIPPLE must run
    without oMLX signals and must then also say so.
    """

    name = "openai_compatible"

    async def health(self) -> Health:
        t0 = time.perf_counter()
        try:
            await self._get("/v1/models")
        except (httpx.HTTPError, ValueError) as exc:
            return Health(healthy=False, error=f"{type(exc).__name__}: {exc}")
        return Health(healthy=True, rtt_ms=round((time.perf_counter() - t0) * 1000.0, 2))

    async def models(self) -> list[ModelInfo]:
        data = await self._get("/v1/models")
        out: list[ModelInfo] = []
        for m in (data or {}).get("data") or []:
            if isinstance(m, dict) and m.get("id"):
                out.append(ModelInfo(id=str(m["id"]), available=True,
                                     context_len=_as_int(m.get("max_model_len"))))
        return out


async def probe_capabilities(backend: Backend, *,
                             health: Health | None = None
                             ) -> tuple[frozenset[str], list[str], list[ModelInfo]]:
    """Probe capabilities against the running backend (FA-23).

    Returns (capabilities, notes, models). `notes` are the visible reasons for
    missing capabilities — they end up in the log and in /healthz.

    `health` is the answer the caller already holds for this tick. The capacity
    probe would otherwise ask `/health` a second time, and the caller of the
    healthy-transition re-read (issue #86) has just probed it — two probes for one
    answer, on a path that exists to avoid exactly that.
    """
    notes: list[str] = []
    caps: set[str] = set()

    models: list[ModelInfo] = []
    try:
        models = await backend.models()
        if models:
            caps.add("availability")
        else:
            notes.append("availability missing: /v1/models returned no models")
    except (httpx.HTTPError, ValueError) as exc:
        notes.append(f"availability missing: /v1/models unreadable ({type(exc).__name__})")

    try:
        residency = await backend.residency()
    except (httpx.HTTPError, ValueError):
        residency = None
    if residency is not None:
        caps.add("residency")
        caps.add("prefix_cache")
    else:
        notes.append("residency missing: no loaded models with size readable "
                     "→ FA-18 (eviction guard) is switched off")

    if health is not None:
        cap = (Capacity(ceiling_bytes=health.ceiling_bytes, used_bytes=health.used_bytes,
                        model_count=health.model_count, loaded_count=health.loaded_count)
               if health.ceiling_bytes is not None and health.used_bytes is not None
               else None)
    else:
        try:
            cap = await backend.capacity()
        except (httpx.HTTPError, ValueError):
            cap = None
    if cap is not None and cap.ceiling_bytes > 0:
        caps.add("capacity")
    else:
        notes.append("capacity missing: final_ceiling/usage unreadable "
                     "→ no eviction forecast")

    try:
        nid = await backend.identity()
    except (httpx.HTTPError, ValueError):
        nid = None
    if nid:
        caps.add("identity")
    else:
        notes.append("identity missing: no node_id → address counts as identity (RB-9)")

    if isinstance(backend, OmlxBackend):
        try:
            if await backend.lifecycle_available():
                caps.add("lifecycle")
            else:
                notes.append("lifecycle missing: no load/unload route")
        except (httpx.HTTPError, ValueError):
            notes.append("lifecycle missing: load/unload route not probeable")

    return frozenset(caps), notes, models


def merge_capabilities(backends: Iterable[Backend]) -> frozenset[str]:
    """Fleet capabilities: a requirement is only armed when **every** active
    host can satisfy it — otherwise the protection would be incomplete."""
    caps: set[str] | None = None
    for b in backends:
        caps = set(b.capabilities) if caps is None else caps & set(b.capabilities)
    return frozenset(caps or ())


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool_opt(value: Any) -> bool | None:
    """Tri-state: True / False / None (= the field was not reported at all).

    oMLX reports `is_loading` as a real boolean, but a version without the field
    must not be read as "not loading": that would make RIPPLE believe a running
    load had ended and re-enable probes in the middle of it.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None
