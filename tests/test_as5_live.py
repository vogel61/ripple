"""Tests for tools/as5_live.py — a stub fleet, no network, no models.

The runner takes its HTTP client by injection, so every test here drives the
real orchestration (preflight, phases, restore, report) against a fake that
behaves like two oMLX hosts behind RIPPLE. Nothing listens on a socket, nothing
sleeps for real: the clock is advanced by the fake's `sleep`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ripple.config import AuthConfig, Config, HostConfig  # noqa: E402
from tools import as5_live  # noqa: E402
from tools.as5_live import (EXIT_FAILED, EXIT_M5_TOUCHED, EXIT_OK,  # noqa: E402
                            EXIT_PREFLIGHT, EXIT_SERVED, Options, Resp, Runner)

KEEP = "alpha"
WORK = "beta"
HEAVY = as5_live.HEAVY_DEFAULT
PROBE = as5_live.PROBE_DEFAULT
GB = 1e9
CEILING = 526.5 * GB
HEAVY_SIZE = 453.6 * GB
PROBE_SIZE = 111.6 * GB
SMALL_SIZE = 6.5 * GB


# ---------------------------------------------------------------------------
# A fake fleet: two oMLX hosts plus the RIPPLE views the runner reads
# ---------------------------------------------------------------------------
class FakeFleet:
    def __init__(self, *, heavy_loaded=None, probe_loaded=None,
                 probe_allowed=None, phase1_host=WORK, as5c_mode="conflict",
                 as5c_host=WORK, fail_chat_at=None, evict_heavy_on_probe_load=True):
        self.heavy_loaded = dict(heavy_loaded or {KEEP: True, WORK: True})
        self.probe_loaded = dict(probe_loaded or {KEEP: False, WORK: False})
        self.probe_allowed = dict(probe_allowed or {KEEP: False, WORK: True})
        self.phase1_host = phase1_host
        self.as5c_mode = as5c_mode
        self.as5c_host = as5c_host
        self.fail_chat_at = fail_chat_at
        self.evict_heavy_on_probe_load = evict_heavy_on_probe_load
        self.threads: list[dict] = []
        self.upstream_probe_requests = 0
        self.calls: list[tuple[str, str]] = []
        self.changes: list[tuple[str, str, str]] = []   # (verb, host, model)
        self.chats = 0
        self.now = 0.0

    # -- clock -------------------------------------------------------------
    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    # -- helpers -----------------------------------------------------------
    def used(self, host: str) -> float:
        total = SMALL_SIZE
        if self.heavy_loaded[host]:
            total += HEAVY_SIZE
        if self.probe_loaded[host]:
            total += PROBE_SIZE
        return total

    def heavy_thread_hosts(self) -> set[str]:
        return {t["host"] for t in self.threads if t["model"] == HEAVY}

    def _hosts(self) -> list[dict]:
        rows = []
        for name in (KEEP, WORK):
            rows.append({
                "name": name, "role": "entry" if name == KEEP else "worker",
                "weight": 3 if name == KEEP else 2,
                "address": f"http://{name.lower()}.test:8000",
                "status": "healthy", "healthy": True, "identity_ok": True,
                "guard_enabled": True, "hard_threshold": 0.95,
                "used_bytes": self.used(name), "ceiling_bytes": CEILING,
                "inflight": 0, "max_inflight": 6,
                "thread_slots": sum(1 for t in self.threads if t["host"] == name),
                "max_threads": 20,
            })
        return rows

    def _models(self) -> list[dict]:
        rows = []
        for model in (HEAVY, PROBE, "small"):
            hosts = []
            for name in (KEEP, WORK):
                loaded = (self.heavy_loaded[name] if model == HEAVY
                          else self.probe_loaded[name] if model == PROBE else True)
                size = (HEAVY_SIZE if model == HEAVY
                        else PROBE_SIZE if model == PROBE else SMALL_SIZE)
                hosts.append({
                    "host": name, "available": True, "loaded": loaded,
                    "pinned": False, "estimated_size": size,
                    "allowed": (self.probe_allowed[name] if model == PROBE else True),
                    "threads": sum(1 for t in self.threads
                                   if t["host"] == name and t["model"] == model),
                })
            rows.append({"id": model, "hosts": hosts})
        return rows

    def _status(self, host: str) -> dict:
        models = []
        for model, loaded, size in (
                (HEAVY, self.heavy_loaded[host], HEAVY_SIZE),
                (PROBE, self.probe_loaded[host], PROBE_SIZE),
                ("small", True, SMALL_SIZE)):
            models.append({"id": model, "loaded": loaded, "pinned": False,
                           "estimated_size": size, "last_access": 1.0})
        return {"models": models}

    def _json(self, payload: dict, status: int = 200,
              headers: dict[str, str] | None = None) -> Resp:
        return Resp(status=status, headers=headers or {},
                    text=json.dumps(payload), latency_s=0.01)

    def _conflict(self) -> Resp:
        offered = [n for n in (KEEP, WORK) if self.probe_allowed[n]]
        detail = "; ".join(f"{n}: conflict (would evict active {HEAVY})"
                           for n in offered)
        return self._json(
            {"error": {"message": f"no host can take {PROBE!r} without evicting "
                                  f"a running thread — {detail}",
                       "type": "ripple_error", "code": 503,
                       "reason": "conflict"}},
            status=503)

    def _serve_probe(self, host: str) -> Resp:
        first = not self.probe_loaded[host]
        self.probe_loaded[host] = True
        if self.evict_heavy_on_probe_load and host == KEEP:
            self.heavy_loaded[host] = False
        self.upstream_probe_requests += 1
        return self._json({"choices": [{"message": {"content": "ok"}}]},
                          headers={"x-ripple-host": host,
                                   "x-ripple-reason": "cold_load" if first
                                   else "ephemeral"})

    def _no_healthy_host(self, reason: str = "no_healthy_host") -> Resp:
        """The 503 the rehearsal got right after the 111 GB cold load."""
        return self._json(
            {"error": {"message": f"no healthy host offers {PROBE!r}",
                       "type": "ripple_error", "code": 503,
                       "reason": reason}},
            status=503)

    def _chat(self, body: dict) -> Resp:
        model = body.get("model")
        self.chats += 1
        if self.fail_chat_at is not None and self.chats == self.fail_chat_at:
            raise RuntimeError("stub upstream exploded")
        if model == HEAVY:
            # A thread per distinct session id; the host with fewer of them wins,
            # ties go to the heavier node — enough to bind one thread per host.
            sid = (body.get("messages") or [{}])[0].get("content", "")[:24]
            counts = {n: sum(1 for t in self.threads
                             if t["host"] == n and t["model"] == HEAVY)
                      for n in (KEEP, WORK)}
            host = min((KEEP, WORK), key=lambda n: (counts[n], n != KEEP))
            self.threads.append({"model": HEAVY, "host": host, "key": sid})
            return self._json({"choices": [{"message": {"content": "ok"}}]},
                              headers={"x-ripple-host": host,
                                       "x-ripple-reason": "thread"})
        if model == PROBE:
            if {KEEP, WORK} <= self.heavy_thread_hosts():
                # AS-5c: both nodes hold a live DeepSeek thread.
                if self.as5c_mode == "serve":
                    return self._serve_probe(self.as5c_host)
                return self._conflict()
            # AS-5b: the probe is free to cold load.
            return self._serve_probe(self.phase1_host)
        return self._json({"error": {"message": "unknown model"}}, status=404)

    # -- the transport surface --------------------------------------------
    def request(self, method: str, url: str, *, json_body=None, headers=None,
                timeout=None, retry_5xx: bool = True) -> Resp:
        self.calls.append((method, url))
        self.now += 1.0
        if url.endswith("/healthz"):
            return self._json({"ok": True, "version": "test", "uptime_seconds": 1.0,
                               "capabilities": {"eviction_guard": True},
                               "threads": {"active": len(self.threads),
                                           "registry": [dict(t, turns=1,
                                                             idle_seconds=1.0)
                                                        for t in self.threads]},
                               "hosts": self._hosts(), "models": self._models()})
        if url.endswith("/stats"):
            return self._json({"by_model": {PROBE: {"requests":
                                                    self.upstream_probe_requests}},
                               "requests_total": 0, "http_503_total": 0})
        if url.endswith("/v1/models/status"):
            return self._json(self._status(self._host_of(url)))
        if "/v1/models/" in url and url.endswith("/load"):
            host, model = self._host_of(url), self._model_of(url)
            self.changes.append(("load", host, model))
            if model == HEAVY:
                self.heavy_loaded[host] = True
            if model == PROBE:
                self.probe_loaded[host] = True
            return self._json({"status": "ok"})
        if "/v1/models/" in url and url.endswith("/unload"):
            host, model = self._host_of(url), self._model_of(url)
            self.changes.append(("unload", host, model))
            if model == HEAVY:
                self.heavy_loaded[host] = False
            if model == PROBE:
                self.probe_loaded[host] = False
            return self._json({"status": "ok"})
        if url.endswith("/v1/chat/completions"):
            return self._chat(json_body or {})
        raise AssertionError(f"unexpected call {method} {url}")

    @staticmethod
    def _host_of(url: str) -> str:
        for name in (KEEP, WORK):
            if name.lower() in url:
                return name
        raise AssertionError(f"no host in {url}")

    @staticmethod
    def _model_of(url: str) -> str:
        for model in (HEAVY, PROBE):
            if model in url:
                return model
        raise AssertionError(f"no model in {url}")

    # -- assertions used by the tests --------------------------------------
    def mutation_calls(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u in self.calls if m == "POST"]


def make_config() -> Config:
    return Config(
        hosts=[
            HostConfig(name=KEEP, role="entry", weight=3.0,
                       addresses=[f"http://{KEEP.lower()}.test:8000"],
                       node_id="node-keep", hard_threshold=0.95),
            HostConfig(name=WORK, role="worker", weight=2.0,
                       addresses=[f"http://{WORK.lower()}.test:8000"],
                       node_id="node-work", hard_threshold=0.95),
        ],
        auth=AuthConfig(enabled=True, allow_loopback=True),
    )


def make_runner(tmp_path: Path, fleet: FakeFleet, **opt_kw) -> Runner:
    opts = Options(ripple="http://ripple.test", log_dir=tmp_path / "logs",
                   report_dir=tmp_path / "docs", mail=False, **opt_kw)
    log = as5_live.Logger(opts.log_dir / "test.log")
    return Runner(make_config(), opts, http=fleet, log=log,
                  clock=fleet.clock, sleep=fleet.sleep)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def test_preflight_refuses_when_heavy_is_missing_on_the_work_host(tmp_path):
    fleet = FakeFleet(heavy_loaded={KEEP: True, WORK: False})
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_PREFLIGHT
    assert fleet.mutation_calls() == [], "preflight must not change anything"
    assert runner.phases == []
    assert not (tmp_path / "docs").exists(), "a refused run writes no report"
    failed = [c.name for c in runner.checks if c.required and not c.ok]
    assert any("loaded" in name and WORK in name for name in failed)


def test_preflight_refuses_when_the_probe_is_already_loaded(tmp_path):
    fleet = FakeFleet(probe_loaded={KEEP: False, WORK: True})
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_PREFLIGHT
    assert fleet.mutation_calls() == []
    failed = [c.name for c in runner.checks if c.required and not c.ok]
    assert any("not loaded" in name and WORK in name for name in failed)


# ---------------------------------------------------------------------------
# AS-5b abort paths
# ---------------------------------------------------------------------------
def test_a_response_on_the_keep_host_aborts_and_restores(tmp_path):
    fleet = FakeFleet(phase1_host=KEEP)      # the M5 serves the probe, evicting DeepSeek
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_M5_TOUCHED
    # the M5 was restored immediately: probe off, DeepSeek back on
    assert ("unload", KEEP, PROBE) in fleet.changes
    assert ("load", KEEP, HEAVY) in fleet.changes
    assert fleet.heavy_loaded[KEEP] is True
    assert fleet.probe_loaded[KEEP] is False
    assert runner.phases[0]["status"] == "failed"
    assert runner.restore_ok is True


def test_the_restore_runs_when_a_phase_raises(tmp_path):
    fleet = FakeFleet(fail_chat_at=3)
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_FAILED
    # AS-5b unloaded DeepSeek on the work host, so the restore must load it again
    assert ("load", WORK, HEAVY) in fleet.changes
    assert fleet.heavy_loaded == {KEEP: True, WORK: True}
    assert fleet.probe_loaded == {KEEP: False, WORK: False}
    assert runner.restore_ok is True
    report = (tmp_path / "docs" / f"as5-live-{as5_live.datetime.now():%Y-%m-%d}.md")
    assert report.is_file()


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------
def test_the_deadline_skips_remaining_phases_and_still_restores(tmp_path):
    fleet = FakeFleet()
    runner = make_runner(tmp_path, fleet, deadline_s=15.0)   # trips inside AS-5b

    code = runner.execute()

    ids = [p["id"] for p in runner.phases]
    assert code == EXIT_FAILED
    assert "AS-5c" not in ids, "no new phase may start after the deadline"
    assert runner.restore_ok is True
    assert fleet.heavy_loaded == {KEEP: True, WORK: True}
    assert fleet.probe_loaded == {KEEP: False, WORK: False}
    report = tmp_path / "docs" / f"as5-live-{as5_live.datetime.now():%Y-%m-%d}.md"
    assert "deadline" in report.read_text().lower()


# ---------------------------------------------------------------------------
# The happy path and AS-5c
# ---------------------------------------------------------------------------
def test_a_full_run_passes_and_writes_the_report(tmp_path):
    fleet = FakeFleet()                       # conflict mode: AS-5c must refuse
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_OK
    assert [p["id"] for p in runner.phases] == ["AS-5b", "restore", "AS-5c"]
    assert all(p["status"] == "pass" for p in runner.phases)
    rows = runner.phases[0]["rows"]
    assert len(rows) == 10
    assert {r["host"] for r in rows} == {WORK}
    assert rows[0]["reason"] == "cold_load"
    # the M5's DeepSeek was never unloaded
    assert ("unload", KEEP, HEAVY) not in fleet.changes
    assert fleet.heavy_loaded == {KEEP: True, WORK: True}
    assert fleet.probe_loaded == {KEEP: False, WORK: False}

    report = tmp_path / "docs" / f"as5-live-{as5_live.datetime.now():%Y-%m-%d}.md"
    text = report.read_text()
    assert "| Phase | Result | Measured |" in text
    assert "AS-5b" in text and "AS-5c" in text
    assert "| AS-5b |" in text and "**pass**" in text
    assert "Exit code:** 0" in text


def test_as5c_served_instead_of_refused_exits_5(tmp_path):
    fleet = FakeFleet(as5c_mode="serve", as5c_host=WORK)
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_SERVED
    phase = next(p for p in runner.phases if p["id"] == "AS-5c")
    assert phase["status"] == "failed"
    assert phase["status_code"] == 200
    # and the probe was cleaned up again
    assert fleet.probe_loaded == {KEEP: False, WORK: False}


def test_as5c_conflict_must_name_every_offered_host(tmp_path):
    # both hosts offer the probe: the message must name both, and a message that
    # only names one is a failure. The stub names both, so this run passes and
    # the report records the two names.
    fleet = FakeFleet(probe_allowed={KEEP: True, WORK: True})
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_OK
    phase = next(p for p in runner.phases if p["id"] == "AS-5c")
    assert phase["named_hosts"] == [KEEP, WORK]
    assert phase["upstream_requests_before"] == phase["upstream_requests_after"]


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
def test_verify_only_reports_an_intact_fleet_without_changing_it(tmp_path):
    fleet = FakeFleet()
    runner = make_runner(tmp_path, fleet)

    assert runner.watchdog() == EXIT_OK
    assert fleet.mutation_calls() == []


def test_verify_only_repairs_a_broken_fleet(tmp_path):
    fleet = FakeFleet(probe_loaded={KEEP: False, WORK: True},
                      heavy_loaded={KEEP: True, WORK: False})
    runner = make_runner(tmp_path, fleet)

    assert runner.watchdog() == EXIT_OK
    assert ("unload", WORK, PROBE) in fleet.changes
    assert ("load", WORK, HEAVY) in fleet.changes
    assert fleet.heavy_loaded == {KEEP: True, WORK: True}
    assert fleet.probe_loaded == {KEEP: False, WORK: False}


def test_verify_only_reports_a_failed_repair(tmp_path, capsys):
    class Broken(FakeFleet):
        def request(self, method, url, **kw):
            if url.endswith("/load"):
                return self._json({"error": "nope"}, status=500)
            return super().request(method, url, **kw)

    fleet = Broken(heavy_loaded={KEEP: True, WORK: False})
    runner = make_runner(tmp_path, fleet)

    assert runner.watchdog() == as5_live.EXIT_RESTORE_FAILED
    out = capsys.readouterr().out
    assert "RESTORE FAILED" in out
    # a human gets the exact commands, not a hint
    assert "/v1/models/" in out and "curl" in out


# ---------------------------------------------------------------------------
# The HTTP layer itself: retries are the difference between a hiccup and a
# failed run at 01:00, so they are tested, not assumed.
# ---------------------------------------------------------------------------
def _http_with(handler) -> as5_live.Http:
    http = as5_live.Http(sleep=lambda _s: None)
    http._client = httpx.Client(transport=httpx.MockTransport(handler))
    return http


def test_http_retries_5xx_three_times_but_never_4xx():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/bad":
            return httpx.Response(503, text="nope")
        if request.url.path == "/missing":
            return httpx.Response(404, text="nope")
        return httpx.Response(200, text="ok")

    http = _http_with(handler)
    assert http.request("GET", "http://x/bad").status == 503
    assert len(seen) == 3
    seen.clear()
    assert http.request("GET", "http://x/missing").status == 404
    assert len(seen) == 1
    seen.clear()
    assert http.request("GET", "http://x/ok").status == 200
    assert len(seen) == 1


def test_http_retries_connection_errors():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text="ok")

    http = _http_with(handler)
    assert http.request("GET", "http://x/ok").status == 200
    assert state["n"] == 3


def test_http_gives_up_after_three_connection_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http = _http_with(handler)
    with pytest.raises(as5_live.TransportError):
        http.request("GET", "http://x/ok")


def test_a_503_on_a_chat_is_not_retried_because_it_is_the_measurement():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(503, text='{"error": {"reason": "conflict"}}')

    http = _http_with(handler)
    assert http.request("POST", "http://x/v1/chat/completions",
                        retry_5xx=False).status == 503
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# The transient 503 no_healthy_host right after a cold load
#
# Measured on 14.11.: request 1 cold-loaded 111,6 GB in 26,6 s and answered 200
# with reason cold_load; request 2, sent immediately, got
#   503 {"error": {"reason": "no_healthy_host"}}
# because one health probe timed out during the load and hosts.py sets
# `healthy` from every single probe result. The host was reachable again seconds
# later. The tool must wait that out — and must never retry the deliberate
# `conflict` 503 of AS-5c.
# ---------------------------------------------------------------------------
class FlakyPool(FakeFleet):
    """RIPPLE's pool loses the work host for a while after the first probe.

    The first probe request is served (the cold load), and only then does the
    host drop out of the pool — exactly the order the rehearsal log shows.
    """

    def __init__(self, *, pool_down_seconds: float = 6.0,
                 pool_down_forever: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.pool_down_seconds = pool_down_seconds
        self.pool_down_forever = pool_down_forever
        self.pool_down_since: float | None = None

    def pool_down(self) -> bool:
        if self.pool_down_since is None:
            return False
        if self.pool_down_forever:
            return True
        return (self.now - self.pool_down_since) < self.pool_down_seconds

    def _hosts(self) -> list[dict]:
        rows = super()._hosts()
        if self.pool_down():
            for row in rows:
                if row["name"] == WORK:
                    row["healthy"] = False
                    row["status"] = "down"     # ripple/hosts.py: healthy else down
        return rows

    def _chat(self, body: dict) -> Resp:
        if body.get("model") == PROBE:
            if self.pool_down():
                return self._no_healthy_host()
            if self.pool_down_since is None:
                resp = super()._chat(body)
                self.pool_down_since = self.now   # the probe that timed out
                return resp
        return super()._chat(body)


class PoolGoneButHealthy(FakeFleet):
    """503 no_healthy_host although /healthz says the host is healthy: the
    health wait cannot help, so the retry budget has to stop the loop."""

    def _chat(self, body: dict) -> Resp:
        if body.get("model") == PROBE:
            return self._no_healthy_host()
        return super()._chat(body)


def test_a_transient_no_healthy_host_is_waited_out_and_retried(tmp_path):
    fleet = FlakyPool()                       # the pool is back after ~6 s
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_OK
    phase = next(p for p in runner.phases if p["id"] == "AS-5b")
    assert phase["status"] == "pass"
    assert len(phase["rows"]) == 10
    assert {r["status"] for r in phase["rows"]} == {200}
    assert {r["host"] for r in phase["rows"]} == {WORK}
    assert phase["rows"][0]["reason"] == "cold_load"

    text = (tmp_path / "logs" / "test.log").read_text()
    # request 2 got the 503 and was sent again — two lines for it
    assert text.count("chat[as5b #2] model=") == 2
    assert "HTTP 503 no_healthy_host" in text
    # the wait is visible: unhealthy polls, then healthy, then the retry
    assert "wait: beta healthy=false" in text
    assert "wait: beta healthy=true" in text
    assert "back in the pool after" in text
    assert "retry 1/3" in text
    # and the retried request is the one that counted
    assert "attempt=2/4" in text
    # nothing was invented: no request reached the entry host
    assert ("unload", KEEP, HEAVY) not in fleet.changes
    assert fleet.heavy_loaded == {KEEP: True, WORK: True}


def test_a_host_that_never_recovers_in_the_window_fails_with_exit_1(tmp_path):
    # out of the pool for longer than the tool is willing to wait (20 s)
    fleet = FlakyPool(pool_down_seconds=40.0)
    runner = make_runner(tmp_path, fleet, health_wait_s=20.0)

    assert runner.execute() == EXIT_FAILED
    assert WORK in runner.abort_message
    assert "never became healthy again" in runner.abort_message
    assert "last /healthz" in runner.abort_message
    assert "healthy=False" in runner.abort_message

    text = (tmp_path / "logs" / "test.log").read_text()
    polls = text.count("wait: beta healthy=false")
    assert 1 <= polls <= 15, f"the wait must be bounded, saw {polls} polls"
    # bounded, and it did give up rather than loop: the run finished and the
    # fleet was put back once the pool returned
    assert runner.restore_ok is True
    assert fleet.heavy_loaded == {KEEP: True, WORK: True}
    assert fleet.probe_loaded == {KEEP: False, WORK: False}


def test_the_conflict_503_is_never_retried(tmp_path):
    fleet = FakeFleet()                       # AS-5c: conflict, by the book
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_OK
    phase = next(p for p in runner.phases if p["id"] == "AS-5c")
    assert phase["reason"] == "conflict"

    text = (tmp_path / "logs" / "test.log").read_text()
    assert text.count("chat[as5c probe] model=") == 1, "a conflict is the measurement"
    assert "wait:" not in text, "nothing may be waited for on a conflict"
    assert "no_healthy_host" not in text


def test_the_retry_budget_is_three_retries_and_then_exit_1(tmp_path):
    fleet = PoolGoneButHealthy()
    runner = make_runner(tmp_path, fleet)

    assert runner.execute() == EXIT_FAILED
    text = (tmp_path / "logs" / "test.log").read_text()
    assert text.count("chat[as5b #1] model=") == 4, "one send plus three retries"
    assert "attempt=4/4" in text
    assert "retries" in runner.abort_message
    assert runner.restore_ok is True
    assert fleet.heavy_loaded == {KEEP: True, WORK: True}


def test_the_health_wait_is_configurable_on_the_command_line():
    parser = as5_live.build_parser()
    assert parser.parse_args([]).health_wait_seconds == 120.0
    args = parser.parse_args(["--health-wait-seconds", "45"])
    assert args.health_wait_seconds == 45.0
