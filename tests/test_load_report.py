"""load_report.py: union of overlapping intervals, dedupe, aggregation, buckets.

The fixture lines are verbatim from the real logs (M5 and M3, 14.11.2026) —
including the two shapes that matter: the diffusion model puts extra numbers
inside the tok/s parentheses, and the DEBUG "Chat completion request received:"
line sits directly next to the INFO line that is parsed.

No network, no ssh: every read goes through a function seam (`load_hosts` takes
a fetcher), so the unreachable-host path is a plain function, not a mock server.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools import load_report as lr

NOW = datetime(2026, 11, 14, 20, 0, 0)

OMLX = """\
2026-11-14 19:49:17,313 - omlx.server - INFO - [-] - Chat completion: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp, 15722 tokens in 102.81s (158.1 tok/s), prompt: 42644, finish_reason=stop, max_tokens=102400, request_max_tokens=None, stream_model_ttft=3.35s
2026-11-14 20:01:53,366 - omlx.server - DEBUG - [-] - Chat completion request received: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp, messages=157, stream=True, max_tokens=None, temp=None
2026-11-14 20:01:58,537 - omlx.server - INFO - [-] - Chat completion: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp, 463 tokens in 17.79s (31.9 tok/s), prompt: 115713, finish_reason=tool_calls, max_tokens=262144, request_max_tokens=None, stream_model_ttft=3.30s, stream_visible_ttft=3.30s
2026-11-14 07:10:04,559 - omlx.server - INFO - [-] - Chat completion: model=diffusiongemma-26B-A4B-it-6bit, 4399 tokens in 24.41s (180.2 tok/s e2e, output=182.3 tok/s, canvas=190.9 tok/s, prompt=2490.9 tok/s, work=3054.7 tok/s, steps=288), prompt: 288, finish_reason=stop, max_tokens=262144, request_max_tokens=None, stream_model_ttft=1.10s, stream_visible_ttft=1.10s
2026-11-13 21:02:11,004 - omlx.server - INFO - [-] - Chat completion: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp, 900 tokens in 30.00s (30.0 tok/s), prompt: 1000, finish_reason=stop, max_tokens=4096, request_max_tokens=None
"""

# Two requests that overlap: [19:57:37, 19:59:20] and [19:59:10, 19:59:30].
# Summed: 103 + 20 s. Union: 113 s — 10 s counted once, not twice. Both end
# before NOW, so a [NOW-1h, NOW] window contains them whole.
OVERLAP = """\
2026-11-14 19:59:20,000 - omlx.server - INFO - [-] - Chat completion: model=m, 100 tokens in 103.00s (1.0 tok/s), prompt: 5000, finish_reason=stop, max_tokens=4096, request_max_tokens=None, stream_model_ttft=3.00s
2026-11-14 19:59:30,000 - omlx.server - INFO - [-] - Chat completion: model=m, 50 tokens in 20.00s (2.5 tok/s), prompt: 5000, finish_reason=stop, max_tokens=4096, request_max_tokens=None, stream_model_ttft=1.00s
"""

REQ = ("2026-11-14 19:10:00 INFO    ripple.request: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp "
       "host={host} reason={reason} thread={thread} prompt_chars=1552 "
       "duration_ms={ms} status=200 note=stream")

RIPPLE = "\n".join([
    # every line twice: what the two handlers produced before the fix
    REQ.format(host="M5-alpha", reason="thread", thread="26311548f7805f9c", ms="267.4"),
    REQ.format(host="M5-alpha", reason="thread", thread="26311548f7805f9c", ms="267.4"),
    REQ.format(host="M5-alpha", reason="thread", thread="26311548f7805f9c", ms="132.6"),
    REQ.format(host="M5-alpha", reason="thread", thread="26311548f7805f9c", ms="132.6"),
    REQ.format(host="M3-beta", reason="ephemeral", thread="-", ms="7922.4"),
    REQ.format(host="M3-beta", reason="ephemeral", thread="-", ms="7922.4"),
    "2026-11-14 18:28:04 WARNING ripple: rebind thread=26311548f7805f9c "
    "from=M3-beta to=M5-alpha gain=2.6s cost=0.8s (1 moves in the last hour)",
    "2026-11-14 18:28:04 WARNING ripple: rebind thread=26311548f7805f9c "
    "from=M3-beta to=M5-alpha gain=2.6s cost=0.8s (1 moves in the last hour)",
    "2026-11-14 19:41:17 WARNING ripple: rebind thread=26311548f7805f9c "
    "from=M5-alpha to=M3-beta gain=4.0s cost=1.1s (2 moves in the last hour)",
    "2026-11-14 19:41:17 WARNING ripple: rebind thread=26311548f7805f9c "
    "from=M5-alpha to=M3-beta gain=4.0s cost=1.1s (2 moves in the last hour)",
    "2026-11-14 19:55:02 INFO    ripple: promote thread=abc123 batch->interactive",
    "2026-11-14 19:55:02 INFO    ripple: promote thread=abc123 batch->interactive",
])


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_parse_omlx_reads_the_real_line_shapes() -> None:
    records = lr.parse_omlx(OMLX)
    assert len(records) == 4, "the DEBUG request-received line must not be parsed"
    first = records[0]
    assert first.end == datetime(2026, 11, 14, 19, 49, 17)
    assert first.completion_tokens == 15722
    assert first.prompt_tokens == 42644
    assert first.total_s == pytest.approx(102.81)
    assert first.ttft_s == pytest.approx(3.35)
    assert first.decode_s == pytest.approx(99.46)
    # oMLX's own tok/s is decode-only: 15722 / 99.46 = 158.1
    assert first.completion_tokens / first.decode_s == pytest.approx(158.1, abs=0.1)
    # the diffusion variant with extra numbers inside the parentheses
    diffusion = next(r for r in records if r.model.startswith("diffusiongemma"))
    assert diffusion.completion_tokens == 4399
    assert diffusion.ttft_s == pytest.approx(1.10)
    # no stream_model_ttft: parsed, with no prefill measurement
    last = records[-1]
    assert last.ttft_s == 0.0
    assert last.decode_s == pytest.approx(30.0)


def test_parse_omlx_filters_by_window() -> None:
    since = datetime(2026, 11, 14, 0, 0, 0)
    assert len(lr.parse_omlx(OMLX, since=since)) == 3
    assert len(lr.parse_omlx(OMLX, since=since, until=datetime(2026, 11, 14, 12, 0))) == 1
    assert lr.parse_omlx(OMLX, since=datetime(2026, 11, 15)) == []


def test_parse_since_accepts_relative_and_iso() -> None:
    assert lr.parse_since("24h", NOW) == NOW - timedelta(hours=24)
    assert lr.parse_since("48h", NOW) == NOW - timedelta(hours=48)
    assert lr.parse_since("7d", NOW) == NOW - timedelta(days=7)
    assert lr.parse_since("2026-11-14T13:00", NOW) == datetime(2026, 11, 14, 13, 0)
    with pytest.raises(ValueError):
        lr.parse_since("yesterday", NOW)


# --------------------------------------------------------------------------- #
# Union, not sum
# --------------------------------------------------------------------------- #

def test_busy_time_is_the_union_not_the_sum() -> None:
    records = lr.parse_omlx(OVERLAP)
    start, end = NOW - timedelta(hours=1), NOW
    summary = lr.summarise(records, start, end)
    # intervals: 103 s + 20 s = 123 s summed, 113 s as a union
    assert summary["pp_seconds"] == pytest.approx(4.0, abs=0.05)
    assert summary["tg_seconds"] == pytest.approx(119.0, abs=0.05)
    assert summary["busy_seconds"] == pytest.approx(113.0, abs=0.05)
    assert summary["busy_seconds"] < summary["pp_seconds"] + summary["tg_seconds"]


def test_union_merges_touching_and_nested_intervals() -> None:
    t = datetime(2026, 11, 14, 12, 0, 0)
    iv = [(t, t + timedelta(seconds=10)),
          (t + timedelta(seconds=10), t + timedelta(seconds=20)),
          (t + timedelta(seconds=5), t + timedelta(seconds=8))]
    assert lr.union_seconds(iv) == pytest.approx(20.0)
    assert lr.merge_intervals(iv) == [(t, t + timedelta(seconds=20))]


def test_busy_is_clipped_to_the_window() -> None:
    t = datetime(2026, 11, 14, 20, 0, 0)
    iv = [(t - timedelta(seconds=100), t + timedelta(seconds=100))]
    assert lr.union_seconds(iv) == pytest.approx(200.0)
    assert lr.overlap_seconds(iv, t, t + timedelta(seconds=30)) == pytest.approx(30.0)


# --------------------------------------------------------------------------- #
# RIPPLE log: dedupe, rebinds, promotions
# --------------------------------------------------------------------------- #

def test_ripple_lines_are_deduplicated() -> None:
    log = lr.parse_ripple(RIPPLE)
    assert log.duplicates == 6, "every fixture line appears twice"
    assert len(log.requests) == 3
    assert [r.duration_ms for r in log.requests] == [267.4, 132.6, 7922.4]
    assert len(log.movements) == 3


def test_identical_lines_at_different_seconds_are_kept() -> None:
    line = REQ.format(host="M5-alpha", reason="thread", thread="t", ms="10.0")
    later = line.replace("19:10:00", "19:10:01")
    log = lr.parse_ripple(line + "\n" + later + "\n" + line + "\n")
    assert len(log.requests) == 2
    assert log.duplicates == 1


def test_movements_one_pair_in_each_direction_and_one_promotion() -> None:
    mv = lr.movements_summary(lr.parse_ripple(RIPPLE).movements)
    assert mv["rebind_count"] == 2
    assert mv["directions"] == {"M3-beta -> M5-alpha": 1, "M5-alpha -> M3-beta": 1}
    assert mv["promotions"] == 1
    assert mv["promotion_threads"] == ["abc123"]
    gains = sorted(r["gain_s"] for r in mv["rebinds"])
    assert gains == [2.6, 4.0]


def test_ripple_requests_per_host_and_mean_duration() -> None:
    by_host = lr.ripple_by_host(lr.parse_ripple(RIPPLE).requests)
    assert by_host["M5-alpha"]["requests"] == 2
    assert by_host["M5-alpha"]["mean_duration_ms"] == pytest.approx(200.0)
    assert by_host["M3-beta"]["mean_duration_ms"] == pytest.approx(7922.4)


# --------------------------------------------------------------------------- #
# Aggregation and hourly buckets
# --------------------------------------------------------------------------- #

def test_per_host_aggregation() -> None:
    records = lr.parse_omlx(OMLX)
    start, end = datetime(2026, 11, 14, 0, 0), NOW
    s = lr.summarise(records, start, end)
    assert s["requests"] == 4
    assert s["pp_seconds"] == pytest.approx(7.75, abs=0.05)
    # (102.81-3.35) + (17.79-3.30) + (24.41-1.10) + 30.00
    assert s["tg_seconds"] == pytest.approx(167.26, abs=0.05)
    assert s["mean_ttft_s"] == pytest.approx(7.75 / 4, abs=0.01)
    assert s["completion_tokens"] == 15722 + 463 + 4399 + 900
    assert s["nominal_prompt_tokens"] == 42644 + 115713 + 288 + 1000
    assert s["tg_tps"] == pytest.approx(s["completion_tokens"] / s["tg_seconds"], abs=0.1)
    assert 0.0 < s["busy_pct"] < 100.0
    assert s["idle_pct"] == pytest.approx(100.0 - s["busy_pct"], abs=0.05)


def test_idle_is_the_complement_of_the_union() -> None:
    records = lr.parse_omlx(OVERLAP)
    start, end = NOW - timedelta(hours=1), NOW
    s = lr.summarise(records, start, end)
    assert s["busy_pct"] == pytest.approx(100.0 * 113.0 / 3600.0, abs=0.05)
    assert s["busy_pct"] + s["idle_pct"] == pytest.approx(100.0, abs=0.1)


def test_hourly_bucketing_splits_at_the_hour() -> None:
    """A request that ends at 11:00:30 with 120 s spans two buckets: 90 + 30."""
    text = (
        "2026-11-14 10:30:00,000 - omlx.server - INFO - [-] - Chat completion: "
        "model=m, 100 tokens in 120.00s (1.0 tok/s), prompt: 10, finish_reason=stop, "
        "max_tokens=4096, request_max_tokens=None, stream_model_ttft=2.00s\n"
        "2026-11-14 11:00:30,000 - omlx.server - INFO - [-] - Chat completion: "
        "model=m, 100 tokens in 120.00s (1.0 tok/s), prompt: 10, finish_reason=stop, "
        "max_tokens=4096, request_max_tokens=None, stream_model_ttft=2.00s\n")
    start, end = datetime(2026, 11, 14, 10, 0), datetime(2026, 11, 14, 12, 0)
    buckets = dict(lr.hourly_profile(lr.parse_omlx(text), start, end))
    # 10:28:00-10:30:00 and 10:58:30-11:00:00
    assert buckets[datetime(2026, 11, 14, 10, 0)] == pytest.approx(210.0, abs=0.1)
    assert buckets[datetime(2026, 11, 14, 11, 0)] == pytest.approx(30.0, abs=0.1)
    assert sum(buckets.values()) == pytest.approx(
        lr.summarise(lr.parse_omlx(text), start, end)["busy_seconds"], abs=0.1)


def test_hourly_buckets_stay_inside_the_window() -> None:
    start = datetime(2026, 11, 14, 20, 12)
    end = datetime(2026, 11, 14, 22, 0)
    buckets = lr.hourly_profile([], start, end)
    assert [h.strftime("%H:%M") for h, _ in buckets] == ["20:00", "21:00"]
    assert all(v == 0.0 for _, v in buckets)


# --------------------------------------------------------------------------- #
# Sources: local vs ssh, unreachable host
# --------------------------------------------------------------------------- #

class _Host:
    def __init__(self, name: str, address: str) -> None:
        self.name = name
        self.addresses = [address]


def test_local_host_is_read_locally_and_remote_over_ssh(monkeypatch) -> None:
    monkeypatch.setattr(lr, "local_addresses", lambda: {"127.0.0.1", "192.0.2.11"})
    start, end = NOW - timedelta(hours=24), NOW
    local = lr.build_source(_Host("M5-alpha", "http://192.0.2.11:8000"), start, end,
                            no_remote=False)
    remote = lr.build_source(_Host("M3-beta", "http://192.0.2.12:8000"), start, end,
                             no_remote=False)
    assert local.kind == "local"
    assert local.paths[0] == str(Path.home() / ".omlx/logs/server.log")
    assert remote.kind == "ssh" and remote.target == "192.0.2.12"
    assert remote.paths[0] == "$HOME/.omlx/logs/server.log"
    # the rotation for the day the window starts in is included
    assert remote.paths[-1] == f"$HOME/.omlx/logs/server.log.{start.date().isoformat()}"


def test_no_m3_skips_remote_hosts_without_touching_them(monkeypatch) -> None:
    monkeypatch.setattr(lr, "local_addresses", lambda: {"127.0.0.1"})
    start, end = NOW - timedelta(hours=24), NOW
    src = lr.build_source(_Host("M3-beta", "http://192.0.2.12:8000"), start, end,
                          no_remote=True)
    assert src.kind == "skip"
    assert "no-m3" in src.display


def test_unreachable_host_is_marked_not_fatal() -> None:
    """The M5 part must still print; the M3 is 'unreachable', exit stays 0."""
    m5 = lr.Source(host="M5-alpha", kind="local", target="-", paths=["/x"], display="local")
    m3 = lr.Source(host="M3-beta", kind="ssh", target="192.0.2.12",
                   paths=["$HOME/.omlx/logs/server.log"], display="ssh 192.0.2.12")

    def fetcher(src: lr.Source, since: datetime) -> lr.Fetch:
        if src.kind == "ssh":
            return lr.Fetch(error="ssh exit 255: connect timed out")
        return lr.Fetch(text=OVERLAP, files=["/x/server.log"])

    loads = lr.load_hosts([m5, m3], NOW - timedelta(hours=1), NOW, fetcher=fetcher)
    assert loads[0].records and loads[0].error is None
    assert loads[1].error and "timed out" in loads[1].error
    assert loads[1].records == []

    text = lr.render(NOW - timedelta(hours=1), NOW, loads, lr.parse_ripple(RIPPLE), ["log"])
    assert "M5-alpha" in text
    assert "unreachable" in text and "192.0.2.12" in text
    assert "not a fleet total" in text


def test_ssh_command_is_a_grep_with_provenance() -> None:
    cmd = lr._remote_command(["$HOME/.omlx/logs/server.log"])
    assert cmd.startswith("for f in \"$HOME/.omlx/logs/server.log\"")
    assert "grep -hF" in cmd and lr.OMLX_GREP in cmd
    assert "echo \"READ $f\" >&2" in cmd
    assert cmd.endswith("exit 0"), "a missing rotation must not look like a dead host"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "listen_port: 8010\n"
        "hosts:\n"
        "  - name: M5-alpha\n"
        "    addresses: [http://192.0.2.11:8000]\n"
        "  - name: M3-beta\n"
        "    addresses: [http://192.0.2.12:8000]\n", encoding="utf-8")
    return path


def test_cli_renders_a_report_and_exits_zero(tmp_path: Path, monkeypatch,
                                             capsys) -> None:
    cfg = _write_config(tmp_path)
    log = tmp_path / "ripple.log"
    log.write_text(RIPPLE, encoding="utf-8")

    def fake_load(sources, since, until, *, fetcher=lr.fetch_source):
        return [lr.HostLoad(name="M5-alpha", records=lr.parse_omlx(OVERLAP),
                            source="local /x/server.log")]

    monkeypatch.setattr(lr, "load_hosts", fake_load)
    monkeypatch.setattr(lr, "local_addresses", lambda: {"127.0.0.1"})
    # A fixed window, not "24h": the fixture log is dated 2026-11-14, so a
    # relative window silently empties itself once the wall clock passes the
    # fixture by more than a day and the test goes red for a reason that has
    # nothing to do with the code.
    rc = lr.main(["--config", str(cfg), "--ripple-log", str(log),
                  "--since", "2026-11-14T00:00", "--hosts", "M5-alpha"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RIPPLE load report" in out and "per host" in out
    assert "hourly busy seconds" in out
    assert "M3-beta -> M5-alpha" in out


def test_cli_json_is_machine_readable(tmp_path: Path, monkeypatch, capsys) -> None:
    import json
    cfg = _write_config(tmp_path)
    log = tmp_path / "ripple.log"
    log.write_text(RIPPLE, encoding="utf-8")
    monkeypatch.setattr(lr, "load_hosts", lambda *a, **k: [
        lr.HostLoad(name="M5-alpha", records=lr.parse_omlx(OVERLAP), source="local")])
    monkeypatch.setattr(lr, "local_addresses", lambda: {"127.0.0.1"})
    rc = lr.main(["--config", str(cfg), "--ripple-log", str(log), "--json",
                  "--since", "2026-11-14T00:00", "--hosts", "M5-alpha"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["hosts"]["M5-alpha"]["requests"] == 2
    assert payload["movements"]["rebind_count"] == 2
    assert payload["window"]["seconds"] > 0
    assert payload["sources"]


def test_cli_exits_three_when_nothing_parsed(tmp_path: Path, monkeypatch,
                                             capsys) -> None:
    cfg = _write_config(tmp_path)
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(lr, "load_hosts", lambda *a, **k: [
        lr.HostLoad(name="M5-alpha", error="ssh exit 255: unreachable", source="local")])
    monkeypatch.setattr(lr, "local_addresses", lambda: {"127.0.0.1"})
    rc = lr.main(["--config", str(cfg), "--ripple-log", str(empty),
                  "--hosts", "M5-alpha"])
    capsys.readouterr()
    assert rc == 3


def test_cli_rejects_an_unknown_host(tmp_path: Path, capsys) -> None:
    cfg = _write_config(tmp_path)
    rc = lr.main(["--config", str(cfg), "--hosts", "M9-gamma"])
    assert rc == 2
    assert "unknown host" in capsys.readouterr().err
