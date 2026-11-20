"""Replicating a model from one node to another (issue #13).

Two halves, tested separately:

* `ripple.replication` on its own — the pure decisions (relative subpath, the
  settings payload, the round-trip comparison, the progress lines of both rsync
  flavours) and the preflight refusals. No RIPPLE instance: the point of those
  tests is that a copy which would fail halfway never starts.
* The admin endpoints — the job, the maintenance window during it, the report and
  the cancel path. `Ripple.replication_factory` is the seam: the tests hand in a
  `FakeTransport` and every byte the job "moves" is a number in a list.

The copy itself is exercised against the local `rsync` only where that is cheap
and hermetic (a 2 MB copy in a tmp directory, issue #26) and against a stub
script that refuses progress flags the way openrsync does. Nothing here needs
the fleet, a network or a real oMLX; the 111 GB live run is one documented
operator step (`docs/replication.md` §5), not a CI test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import pathlib
import shutil
import subprocess

import pytest

from ripple import replication as rep
from ripple.config import ReplicationConfig, ReplicationHostConfig, load_config

from tests.support import bearer, host_config, keyed_config, wait_until

ADMIN = bearer("test-admin-key")

GB = 1_000_000_000
QWEN = "Qwen3.8-Flash-Next-oQ4e-mtp"
SOURCE_ROOT = "/Users/operator/.omlx/models"
TARGET_ROOT = "/Volumes/models"


# ---------------------------------------------------------------------------
# A transport that moves nothing
# ---------------------------------------------------------------------------
class FakeTransport:
    """The `Transport` protocol, with every answer under the test's control.

    `release` is the copy's gate: while it is unset the job sits in `copy`, which
    is what makes "the maintenance window during the job" and "the cancel path"
    observable instead of a race.
    """

    def __init__(self, *, source_models: list[rep.RemoteModel] | None = None,
                 target_models: list[rep.RemoteModel] | None = None,
                 free_bytes: int | None = 2_000 * GB, has_model: bool = False,
                 ssh_ok: bool = True, ssh_error: str | None = None,
                 register: bool = True, reload_ok: bool = True,
                 put_status: int = 200, put_error: str | None = None,
                 drop_settings: tuple[str, ...] = (),
                 copy_error: str | None = None) -> None:
        self.source_models = list(source_models or [])
        self.target_models = list(target_models or [])
        self.free = free_bytes
        self.has_model = has_model
        self.ssh_ok = ssh_ok
        self.ssh_error = ssh_error
        self.register = register
        self.reload_ok = reload_ok
        self.put_status = put_status
        self.put_error = put_error
        self.drop_settings = drop_settings
        self.copy_error = copy_error

        self.calls: list[tuple[str, str]] = []
        self.put_payload: dict | None = None
        self.reload_calls: list[str] = []
        self.moves: list[tuple[str, str]] = []
        self.mkdirs: list[str] = []

        self.release = asyncio.Event()
        self.copy_started = asyncio.Event()
        self.copy_finished = asyncio.Event()

    # -- reads -------------------------------------------------------------
    async def probe(self, ep):
        self.calls.append(("probe", ep.name))
        return (self.ssh_ok, self.ssh_error)

    async def free_bytes(self, ep, path):
        self.calls.append(("free_bytes", ep.name))
        return self.free

    async def exists(self, ep, path):
        self.calls.append(("exists", ep.name))
        return self.has_model

    async def models(self, ep):
        self.calls.append(("models", ep.name))
        pool = self.source_models if ep.name == "A" else self.target_models
        return [dataclasses.replace(m) for m in pool]

    # -- writes ------------------------------------------------------------
    async def mkdir(self, ep, path):
        self.mkdirs.append(path)
        return True, None

    async def move(self, ep, src, dst):
        self.moves.append((src, dst))
        return True, None

    async def copy(self, src, dst, src_path, dst_path, *, on_progress=None,
                   cancel=None, total_bytes=0):
        self.copy_started.set()
        self.copy_total_bytes = total_bytes
        size = next((m.size_bytes for m in self.source_models
                     if m.model_path == src_path), 0)
        waiters = [asyncio.create_task(self.release.wait())]
        if cancel is not None:
            waiters.append(asyncio.create_task(cancel.wait()))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
        self.copy_finished.set()
        if cancel is not None and cancel.is_set():
            return rep.CopyResult(cancelled=True, bytes_done=size // 2,
                                  seconds=0.01, error="cancelled")
        if self.copy_error:
            return rep.CopyResult(ok=False, error=self.copy_error, seconds=0.01)
        # A real copy of 111 GB takes minutes; the fake takes long enough for
        # `seconds` and MB/s to be non-zero numbers rather than a rounding artefact.
        await asyncio.sleep(0.05)
        if on_progress is not None:
            on_progress(rep.Progress(bytes_done=size, percent=100.0,
                                     rate="1.2GB/s", eta="0:00:00"))
        return rep.CopyResult(ok=True, bytes_done=size, seconds=1.0)

    async def reload(self, ep):
        self.reload_calls.append(ep.name)
        if not self.reload_ok:
            return False, "Engine pool not initialized"
        if self.register:
            for m in self.source_models:
                if not any(t.id == m.id for t in self.target_models):
                    self.target_models.append(dataclasses.replace(m))
        return True, "Re-discovered 1 model from 1 directory"

    async def put_settings(self, ep, model_id, payload):
        self.put_payload = dict(payload)
        if self.put_error:
            return 0, None, self.put_error
        if self.put_status >= 400:
            return self.put_status, {"detail": "extra fields not permitted"}, None
        for i, m in enumerate(self.target_models):
            if m.id == model_id:
                stored = {k: v for k, v in payload.items()
                          if k not in self.drop_settings}
                merged = {**m.settings, **stored}
                # A dropped key is one the target did not take: it must be gone
                # from the read-back, not left at the source's value.
                for key in self.drop_settings:
                    merged.pop(key, None)
                self.target_models[i] = dataclasses.replace(m, settings=merged)
        return 200, {"success": True, "model_id": model_id}, None


def _model(model_id: str = QWEN, *, size: int = 111 * GB,
           path: str = f"{SOURCE_ROOT}/Jundot/{QWEN}",
           settings: dict | None = None) -> rep.RemoteModel:
    return rep.RemoteModel(
        id=model_id, model_path=path,
        settings=settings if settings is not None else {
            "max_context_window": 524288, "temperature": 0.7, "top_p": 0.95,
            "enable_thinking": True, "max_tokens": None,
            "display_name": "mlx-community/" + QWEN, "description": "",
            "active_profile_name": None,
        },
        pinned=False, is_favorite=True, is_hidden=False,
        estimated_size=size, actual_size=size)


def _hosts(a_url: str, b_url: str, *, a_node: str, b_node: str,
           source_root: str = SOURCE_ROOT, target_root: str = TARGET_ROOT) -> list:
    """A and B, both with replication facts: A the source, B the target."""
    return [
        host_config("A", a_url, node_id=a_node,
                    replication=ReplicationHostConfig(
                        address="192.0.2.11", ssh_user="vogel",
                        model_roots=[source_root])),
        host_config("B", b_url, node_id=b_node,
                    replication=ReplicationHostConfig(
                        address="203.0.113.2", ssh_user="vogel",
                        model_roots=[target_root])),
    ]


def _cfg(tmp_path, a, b, *, enabled: bool = True, source_root: str = SOURCE_ROOT,
         target_root: str = TARGET_ROOT):
    cfg = keyed_config(_hosts(a.url, b.url, a_node=a.state.node_id,
                              b_node=b.state.node_id,
                              source_root=source_root,
                              target_root=target_root),
                       str(tmp_path / "keys.yaml"))
    cfg.replication = ReplicationConfig(enabled=enabled, admin_key="omlx-key",
                                        free_space_factor=1.05)
    return cfg


_MISSING = object()


def _plan(entry=_MISSING, **overrides):
    """A preflight with everything right, then one thing wrong per test."""
    facts = dict(source="A", target="B", model=QWEN,
                 entry=_model() if entry is _MISSING else entry,
                 source_roots=[SOURCE_ROOT], target_roots=[TARGET_ROOT],
                 overwrite=False, free_bytes=2_000 * GB, target_has_model=False,
                 ssh_ok=True, ssh_error=None, ssh_target="user@203.0.113.2",
                 # A is this machine, B is the remote one (issue #28). No ignored
                 # address here: the warn is a test of its own.
                 local=rep.LocalSide(name="A"),
                 factor=1.05)
    facts.update(overrides)
    return rep.preflight(**facts)


# ---------------------------------------------------------------------------
# Pure decisions
# ---------------------------------------------------------------------------
def test_relative_subpath_keeps_the_layout_and_prefers_the_specific_root():
    found = rep.relative_under(f"{SOURCE_ROOT}/Jundot/{QWEN}",
                               [SOURCE_ROOT, "/Volumes/models"])
    assert found == (SOURCE_ROOT, f"Jundot/{QWEN}")
    # A root nested inside another one must not shadow the more specific one.
    assert rep.relative_under("/a/b/c/model", ["/a", "/a/b/c"]) == ("/a/b/c", "model")
    # Nothing under any root: no relative path, and the preflight has to refuse.
    assert rep.relative_under("/elsewhere/model", [SOURCE_ROOT]) is None
    assert rep.relative_under("", [SOURCE_ROOT]) is None


def test_the_staging_directory_is_a_dot_directory():
    """oMLX's discovery skips `.`-entries, so a half copy is never registered."""
    assert rep.STAGING_DIR.startswith(".")
    assert rep.staging_for(TARGET_ROOT, f"Jundot/{QWEN}") == \
        f"{TARGET_ROOT}/{rep.STAGING_DIR}/Jundot/{QWEN}"


def test_settings_payload_drops_the_three_keys_the_put_rejects_and_nulls():
    payload = rep.settings_payload(_model().settings)
    # Measured: GET reports 86 keys, PUT accepts 83 — these three are the rest.
    assert "display_name" not in payload
    assert "description" not in payload
    assert "active_profile_name" not in payload
    # `null` is oMLX's "default", and a fresh model already has it.
    assert "max_tokens" not in payload
    assert payload["temperature"] == 0.7
    assert payload["enable_thinking"] is True
    assert rep.settings_payload({}) == {}


def test_compare_settings_reports_a_changed_value_and_a_missing_key():
    sent = {"temperature": 0.7, "top_p": 0.95, "max_context_window": 524288}
    assert rep.compare_settings(sent, dict(sent)) == []
    # Float noise from JSON is not a mismatch.
    assert rep.compare_settings(sent, {"temperature": 0.7000000000000001,
                                       "top_p": 0.95,
                                       "max_context_window": 524288}) == []
    diff = rep.compare_settings(sent, {"temperature": 1.0, "top_p": 0.95})
    assert {d["key"] for d in diff} == {"temperature", "max_context_window"}
    missing = next(d for d in diff if d["key"] == "max_context_window")
    assert missing["reason"] == "not reported by the target"
    # Only what was sent is compared: a default we never touched is not a finding.
    assert rep.compare_settings({"temperature": 0.7},
                                {"temperature": 0.7, "ttl_seconds": None}) == []


def test_parse_progress_reads_rsync_info_progress2():
    p = rep.parse_progress("     1,234,567  45%  123.45MB/s    0:01:23")
    assert p is not None
    assert p.bytes_done == 1_234_567 and p.percent == 45.0
    assert p.rate == "123.45MB/s" and p.eta == "0:01:23"
    # A whole chunk separated by carriage returns: the last update wins.
    chunk = "  10  1%  1.00MB/s  0:10:00\r  20,000  99%  1.20GB/s  0:00:01\r"
    assert rep.parse_progress(chunk).percent == 99.0
    assert rep.parse_progress("sending incremental file list") is None


def test_models_from_api_tolerates_what_a_node_reports():
    models = rep.models_from_api({"models": [
        {"id": "m", "model_path": "/m", "estimated_size": "12", "actual_size": None,
         "settings": {"temperature": 0.5}, "pinned": True},
        {"no_id": True},
        "not a dict",
    ]})
    assert len(models) == 1
    assert models[0].id == "m" and models[0].estimated_size == 12
    assert models[0].pinned is True and models[0].is_favorite is False
    assert models[0].size_bytes == 12          # actual_size 0 → estimate
    assert rep.models_from_api({"models": None}) == []


# ---------------------------------------------------------------------------
# Preflight: refuse rather than half-copy
# ---------------------------------------------------------------------------
def test_a_healthy_plan_names_every_path_and_size():
    plan = _plan()
    assert plan.ok, plan.errors
    assert plan.relative == f"Jundot/{QWEN}"
    assert plan.target_path == f"{TARGET_ROOT}/Jundot/{QWEN}"
    assert plan.staging_path == f"{TARGET_ROOT}/{rep.STAGING_DIR}/Jundot/{QWEN}"
    assert plan.required_bytes == int(round(plan.size_bytes * 1.05))
    assert plan.settings_keys == 4           # 8 read − 3 rejected − 1 null
    assert plan.as_dict()["ok"] is True


def test_too_little_free_space_is_refused_before_anything_is_written():
    plan = _plan(free_bytes=int(111 * GB * 1.01))
    assert not plan.ok
    message = " ".join(plan.errors)
    assert "not enough space" in message
    assert "116.5 GB needed" in message      # 111 × 1.05, rounded


def test_the_5_percent_headroom_is_the_factor_not_a_guess():
    exact = int(round(111 * GB * 1.05))
    assert _plan(free_bytes=exact).ok
    assert not _plan(free_bytes=exact - 1).ok


def test_a_model_that_is_already_there_is_refused_unless_overwrite():
    refused = _plan(target_has_model=True)
    assert not refused.ok
    assert "already on B" in " ".join(refused.errors)
    assert "overwrite=1" in " ".join(refused.errors)

    allowed = _plan(target_has_model=True, overwrite=True)
    assert allowed.ok
    assert "will be replaced" in " ".join(allowed.warnings)


def test_a_model_the_source_does_not_offer_is_refused():
    plan = _plan(entry=None)
    assert not plan.ok
    assert "does not report a model" in " ".join(plan.errors)


def test_a_model_path_outside_the_configured_roots_is_refused():
    plan = _plan(entry=_model(path="/somewhere/else/model"))
    assert not plan.ok
    assert "under none of the configured model roots" in " ".join(plan.errors)
    assert "hosts[].replication.model_roots" in " ".join(plan.errors)


def test_an_unreachable_target_is_refused_with_the_ssh_error():
    plan = _plan(ssh_ok=False,
                 ssh_error="ssh: connect to host 203.0.113.2 port 22: "
                           "Operation timed out")
    assert not plan.ok
    assert "not reachable over SSH" in " ".join(plan.errors)
    assert "Operation timed out" in " ".join(plan.errors)


def test_unreadable_free_space_and_missing_roots_are_refused():
    assert "free space" in " ".join(_plan(free_bytes=None).errors)
    assert "no model root is configured for B" in \
        " ".join(_plan(target_roots=[]).errors)
    assert "under none of the configured" in " ".join(_plan(source_roots=[]).errors)


def test_a_model_without_a_size_and_the_same_node_on_both_sides_are_refused():
    zero = _plan(entry=_model(size=0))
    assert not zero.ok
    assert "no size" in " ".join(zero.errors)
    same = _plan(target="A")
    assert not same.ok
    assert "same node" in " ".join(same.errors)


def test_no_ssh_target_configured_is_refused():
    plan = _plan(ssh_target="")
    assert not plan.ok
    assert "no SSH target is configured for B" in " ".join(plan.errors)


def test_every_refusal_is_collected_not_just_the_first():
    # Three independent failures: no destination root, no space, already there.
    plan = _plan(free_bytes=1, target_roots=[], target_has_model=True)
    assert len(plan.errors) >= 3
    joined = " ".join(plan.errors)
    assert "no model root" in joined
    assert "not enough space" in joined
    assert "already on B" in joined


# ---------------------------------------------------------------------------
# `~` in model_roots (issue #20)
# ---------------------------------------------------------------------------
def test_a_home_relative_source_root_derives_the_same_subpath_as_the_absolute_one():
    """The source is this machine, so `~` is this home — expand it, do not refuse."""
    home = os.path.expanduser("~")
    entry = _model(path=f"{home}/.omlx/models/Jundot/{QWEN}")
    absolute = _plan(entry=entry, source_roots=[f"{home}/.omlx/models"])
    tilde = _plan(entry=entry, source_roots=["~/.omlx/models"])

    assert absolute.ok, absolute.errors
    assert tilde.ok, tilde.errors
    assert tilde.relative == absolute.relative == f"Jundot/{QWEN}"
    assert tilde.target_path == absolute.target_path
    # The pure helper agrees and reports the expanded root, not the literal `~`.
    assert rep.relative_under(f"{home}/.omlx/models/Jundot/{QWEN}",
                              ["~/.omlx/models"]) == (f"{home}/.omlx/models",
                                                      f"Jundot/{QWEN}")
    assert rep.local_root("~/.omlx/models") == f"{home}/.omlx/models"


def test_a_home_relative_root_mixed_with_absolute_ones_still_resolves():
    """The real config shape: one home-relative root next to an absolute one."""
    home = os.path.expanduser("~")
    plan = _plan(entry=_model(path=f"{home}/.omlx/models/mlx-works/{QWEN}"),
                 source_roots=["~/.omlx/models", "/Volumes/models"])
    assert plan.ok, plan.errors
    assert plan.relative == f"mlx-works/{QWEN}"


def test_a_home_relative_target_root_is_refused_by_name_and_never_planned():
    """The target's home is not known here: refuse, never create a literal `~`."""
    plan = _plan(target_roots=["~/.omlx/models"])
    assert not plan.ok
    message = " ".join(plan.errors)
    assert "~/.omlx/models" in message              # the root
    assert "of B" in message                        # the host
    assert "write the absolute path" in message     # what to do instead
    # Nothing was derived from it, so no path the job could use carries a `~`.
    assert plan.target_root == "" and plan.target_path == ""
    assert plan.staging_path == ""
    wire = plan.as_dict()
    assert all("~" not in wire[key]
               for key in ("target_root", "target_path", "staging_path"))


def test_a_root_that_is_still_relative_is_refused_on_both_sides():
    source = _plan(source_roots=["models"])
    assert "still relative" in " ".join(source.errors)
    assert source.relative == ""
    target = _plan(target_roots=["models"])
    assert "is relative" in " ".join(target.errors)
    assert target.target_path == ""
    # An empty root must not normalise to "/" and match every model on the node.
    empty = _plan(source_roots=[""], target_roots=[""])
    assert "empty model root" in " ".join(empty.errors)
    assert empty.relative == "" and empty.target_path == ""


def test_the_load_says_what_a_home_relative_root_will_do(tmp_path):
    """A warning at start, not a silent skip at the first plan (issue #20)."""
    path = tmp_path / "config.yaml"
    path.write_text("""
replication:
  enabled: true
  model_roots: ["~/.omlx/models"]
hosts:
  - name: alpha
    addresses: ["http://127.0.0.1:8000"]
    node_id: 22222222-2222-4222-8222-222222222222
    replication:
      model_roots: ["~/.omlx/models", "models/relative"]
""", encoding="utf-8")
    warnings = " | ".join(load_config(path).warnings)
    assert "replication.model_roots: model root '~/.omlx/models' is expanded as" in warnings
    assert "host alpha: hosts[].replication.model_roots" in warnings
    assert "'models/relative' is relative" in warnings
    # The absolute spelling stays silent: nothing to warn about.
    path.write_text("""
hosts:
  - name: alpha
    addresses: ["http://127.0.0.1:8000"]
    node_id: 22222222-2222-4222-8222-222222222222
    replication:
      model_roots: ["/Users/operator/.omlx/models"]
""", encoding="utf-8")
    assert "model root" not in " | ".join(load_config(path).warnings)


# ---------------------------------------------------------------------------
# rsync: the capability probe, both progress formats, the fallback (issue #26)
# ---------------------------------------------------------------------------
# Captured on the M5, 15.11.2026, with
# `rsync -a --progress --partial /tmp/rt-src/ /tmp/rt-dst/` over two 40 MB files
# (openrsync, protocol version 29). The percentage on each line is that *file's*
# and the copy is half done after the first one — which is the whole problem.
OPENRSYNC_PROGRESS = (
    "./\n"
    "a.bin\n"
    "       41943040 100%  348.71MB/s   00:00:00 (xfer#1, to-check=1/3)\n"
    "b.bin\n"
    "       41943040 100%  352.53MB/s   00:00:00 (xfer#2, to-check=2/3)\n")
# The same copy's `--stats` tail, also captured.
OPENRSYNC_STATS = (
    "Number of files: 3\n"
    "Total file size: 83886080 B\n"
    "sent 83896535 bytes  received 70 bytes  359916795 bytes/sec\n"
    "total size is 83886080  speedup is 1.00\n")
# GNU rsync's `--info=progress2`: cumulative bytes and the copy's own percentage,
# in the format GNU documents. The fleet has no GNU rsync, so this line is the
# documented shape and not a capture from this machine.
GNU_PROGRESS2 = ("sending incremental file list\n"
                 "     1,234,567  45%  123.45MB/s    0:01:23\r"
                 "     8,388,608 100%  124.10MB/s    0:00:00\r")


def test_the_captured_openrsync_output_becomes_the_copy_s_numbers():
    """The issue's capture, through the parser: bytes of the copy, 100 %, rate."""
    reader = rep.ProgressReader(flavour="openrsync")
    update = None
    for line in OPENRSYNC_PROGRESS.splitlines(keepends=True):
        found = reader.feed(line)
        if found is not None:
            update = found
            if found.files_done == 1:
                # One file of two is done: half the copy, not 100 % of it.
                assert found.percent == 50.0
                assert found.bytes_done == 41943040
    assert update is not None
    assert update.bytes_done == 83886080          # both files, not the last one
    assert update.percent == 100.0
    assert update.rate == "352.53MB/s"
    assert update.eta == "00:00:00"
    assert (update.files_done, update.files_total) == (2, 2)


def test_the_openrsync_percentage_is_the_copy_not_the_file():
    """A file's own percentage says nothing about the copy — the counter does."""
    # The first of two files, halfway: 25 % of the copy, not 50 %.
    reader = rep.ProgressReader(flavour="openrsync", total_bytes=83886080)
    update = reader.feed("a.bin\n       20971520  50%  348.71MB/s   00:00:01\r")
    assert update is not None
    assert update.percent == 25.0
    assert update.bytes_done == 20971520
    # Without a total to divide by (nothing has finished yet) the file's share
    # is the only honest number there is — it is reported, not invented.
    bare = rep.ProgressReader(flavour="openrsync")
    assert bare.feed("a.bin\n       20971520  50%  348.71MB/s   00:00:01\r").percent == 50.0
    # After the first file the counter carries the total: 1 of 2 entries done,
    # and the second file at 50 % makes it 75 %.
    assert reader.feed("       41943040 100%  348.71MB/s   00:00:00 "
                       "(xfer#1, to-check=1/3)\n").percent == 50.0
    update = reader.feed("b.bin\n       20971520  50%  352.53MB/s   00:00:01\r")
    assert update.percent == 75.0
    assert update.bytes_done == 62914560          # 41943040 done + 20971520


def test_the_gnu_progress2_line_is_taken_as_it_is():
    """GNU's numbers are already the copy's; nothing is recomputed."""
    reader = rep.ProgressReader(flavour="gnu")
    first = reader.feed(GNU_PROGRESS2)
    assert first is not None
    assert first.bytes_done == 8_388_608
    assert first.percent == 100.0
    # The single update of a whole chunk, as `parse_progress` hands it back.
    p = rep.parse_progress("     1,234,567  45%  123.45MB/s    0:01:23",
                           flavour="gnu")
    assert (p.bytes_done, p.percent) == (1_234_567, 45.0)
    assert p.rate == "123.45MB/s" and p.eta == "0:01:23"


def test_the_gnu_file_counter_is_read_as_what_is_still_to_come():
    """`to-chk` counts down, `to-check` counts up; confusing them halves a copy."""
    reader = rep.ProgressReader(flavour="openrsync")
    update = reader.feed("a.bin\n  1,048,576 100%  123.45MB/s    0:00:00 "
                         "(xfer#1, to-chk=2/3)\n")
    assert update.percent == pytest.approx(33.3)
    assert update.bytes_done == 1_048_576


def test_a_progress_line_split_across_two_reads_is_kept_whole():
    """A 4096-byte read can end inside a number; half a number is not progress."""
    reader = rep.ProgressReader(flavour="openrsync")
    assert reader.feed("a.bin\n       41943") is None
    update = reader.feed("040 100%  348.71MB/s   00:00:00 (xfer#1, to-check=1/3)\n")
    assert update is not None
    assert update.bytes_done == 41943040 and update.percent == 50.0


def test_the_openrsync_eta_is_recomputed_for_the_copy():
    """openrsync prints the eta of the *file*; the job needs the copy's."""
    now = [0.0]
    reader = rep.ProgressReader(flavour="openrsync", total_bytes=1000,
                                clock=lambda: now[0])
    reader.feed("a.bin\n")                        # the clock starts at the first byte
    now[0] = 100.0
    update = reader.feed("        250  25%    1.79MB/s   00:00:03\r")
    # A quarter of the copy in 100 s → about 300 s left, not the file's 3 s.
    assert update.eta == "00:05:00"
    assert update.rate == "1.79MB/s"              # the rate is the file's, as printed


def test_the_stats_summary_gives_bytes_and_rate_without_progress():
    """The numbers a copy has left when it ran without a progress flag."""
    stats = rep.parse_stats(OPENRSYNC_STATS)
    assert stats is not None
    assert stats.bytes_total == 83886080          # the files, not `sent`
    assert stats.sent == 83896535 and stats.received == 70
    assert stats.rate == "359.92MB/s"
    assert rep.parse_stats("") is None
    assert rep.parse_stats("sending incremental file list\n") is None


# -- the probe ---------------------------------------------------------------
class RecordingTransport(rep.SshTransport):
    """The real transport with its shell replaced by canned answers.

    `answers` is a list of `(needle, (rc, stdout, stderr))`; the first needle
    that appears in the command line answers it. `asked` is every command line,
    so a test can see what the probe asked and that it asked only once.
    """

    def __init__(self, answers, **kwargs):
        super().__init__(**kwargs)
        self.answers = list(answers)
        self.asked: list[list[str]] = []

    async def run(self, argv, *, timeout):
        self.asked.append(list(argv))
        joined = " ".join(argv)
        for needle, answer in self.answers:
            if needle in joined:
                return answer
        return 0, "", ""


OPENRSYNC_ANSWERS = [
    ("--info=progress2 --version",
     (1, "", "rsync: unrecognized option `--info=progress2'\n"
             "usage: rsync [-0468BCDEFHIKLOPRSTWVabcdghiklnopqrtuvxyz] [-e program]\n"
             "\tsource ... directory\n")),
    ("--help", (0, "usage: rsync [-a] [--progress] [--partial] [--stats] "
                   "source ... directory\n", "")),
]
GNU_ANSWERS = [("--version", (0, "rsync  version 3.2.7  protocol version 31\n", ""))]


async def test_the_probe_asks_this_machine_s_rsync_and_caches_the_answer():
    transport = RecordingTransport(OPENRSYNC_ANSWERS)
    assert await transport.rsync_progress_flag() == "--progress"
    assert transport.asked[0][1:] == ["--info=progress2", "--version"]
    assert len(transport.asked) == 2              # the version form, then --help
    # Cached: the second caller does not run a process again.
    assert await transport.rsync_progress_flag() == "--progress"
    assert len(transport.asked) == 2
    # …until it is asked to look again.
    assert await transport.rsync_progress_flag(refresh=True) == "--progress"
    assert len(transport.asked) == 4


async def test_the_argv_follows_the_probe_in_both_directions():
    gnu = RecordingTransport(GNU_ANSWERS)
    flag = await gnu.rsync_progress_flag()
    assert flag == "--info=progress2"
    argv = gnu._rsync_argv(rep.Endpoint("A"), rep.Endpoint("B"), "/m/x", "/n/x",
                           progress_flag=flag)
    assert "--info=progress2" in argv and "--progress" not in argv

    mac = RecordingTransport(OPENRSYNC_ANSWERS)
    flag = await mac.rsync_progress_flag()
    assert flag == "--progress"
    argv = mac._rsync_argv(rep.Endpoint("A", ssh_target="user@192.0.2.12"),
                           rep.Endpoint("B"), "/m/x", "/n/x", progress_flag=flag)
    assert "--progress" in argv and "--info=progress2" not in argv
    # What the copy needs in every case, and the two paths, still quoted: the
    # source is the far side of this copy, so it carries the SSH target.
    assert "-a" in argv and "--partial" in argv and "--stats" in argv
    assert argv[-2:] == ["user@192.0.2.12:/m/x/", "/n/x/"]


async def test_a_help_mention_is_believed_when_the_version_form_fails():
    """The exit code alone is not the answer: a wrapper can fail the first form.

    GNU's own help spells the option `--info=FLAGS` (measured against its
    documented help), so the second opinion looks for that spelling too.
    """
    transport = RecordingTransport([
        ("--info=progress2 --version", (1, "", "unknown option")),
        ("--help", (0, "      --info=FLAGS            fine-grained informational "
                       "verbosity\n", "")),
    ])
    assert await transport.rsync_progress_flag() == "--info=progress2"


async def test_a_probe_that_cannot_run_falls_through_to_progress():
    class Missing(RecordingTransport):
        async def run(self, argv, *, timeout):
            raise FileNotFoundError("rsync")

    assert await Missing([]).rsync_progress_flag() == "--progress"


# -- the fallback ------------------------------------------------------------
def _fake_rsync(tmp_path, *, rejects: tuple[str, ...],
                claims_info: bool = False) -> str:
    """A stand-in for a rsync that refuses `rejects` the way openrsync does.

    A message on stderr, its usage, exit 1 — and the real rsync for everything
    else, so the copy in the test really copies. `--help` answers itself, which
    is the point: a rsync whose help does not mention the flag it refuses.
    `claims_info` makes that help advertise `--info=FLAGS`, i.e. a probe that
    answers yes and a copy that then has to fall back.
    """
    pattern = "|".join(rejects)
    help_line = ("      --info=FLAGS            fine-grained informational verbosity"
                 if claims_info else
                 "usage: rsync [-a] [--progress] [--partial] [--stats] source ... directory")
    script = tmp_path / "fake-rsync"
    script.write_text(f"""#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    {pattern})
      echo "rsync: unrecognized option \\`$arg'" >&2
      echo "usage: rsync [-a] [--partial] [--stats] source ... directory" >&2
      exit 1
      ;;
    --help)
      echo "{help_line}"
      exit 0
      ;;
  esac
done
exec "$(command -v rsync)" "$@"
""", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _two_files(root, *, megabytes: int = 1) -> int:
    """`root` with two files of `megabytes` each; returns their total bytes."""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("a.bin", "b.bin"):
        (root / name).write_bytes(b"\xa5" * (megabytes * 1024 * 1024))
    return 2 * megabytes * 1024 * 1024


async def test_a_rsync_without_info_progress2_copies_with_progress(tmp_path):
    """The macOS case: the probe says no, the copy runs with `--progress`."""
    total = _two_files(tmp_path / "src")
    command = _fake_rsync(tmp_path, rejects=("--info=progress2",))
    transport = rep.SshTransport(rsync_command=command)
    updates: list[rep.Progress] = []

    result = await transport.copy(rep.Endpoint("A"), rep.Endpoint("B"),
                                  str(tmp_path / "src"), str(tmp_path / "dst"),
                                  on_progress=updates.append, total_bytes=total)

    assert result.ok, result.error
    assert result.progress_flag == "--progress"
    assert result.fallback_note == ""
    assert result.bytes_done == total
    assert updates and updates[-1].percent == 100.0
    assert (tmp_path / "dst" / "b.bin").stat().st_size == total // 2


async def test_a_wrong_probe_answer_costs_one_attempt_not_the_copy(tmp_path):
    """Probe says `--info=progress2`, rsync refuses it: the copy falls back.

    This is the middle tier — the flag the probe chose is rejected at copy time,
    so the next attempt drops to `--progress` and the numbers stay real.
    """
    total = _two_files(tmp_path / "src")
    command = _fake_rsync(tmp_path, rejects=("--info=progress2",), claims_info=True)
    transport = rep.SshTransport(rsync_command=command)
    updates: list[rep.Progress] = []

    result = await transport.copy(rep.Endpoint("A"), rep.Endpoint("B"),
                                  str(tmp_path / "src"), str(tmp_path / "dst"),
                                  on_progress=updates.append, total_bytes=total)

    assert result.ok, result.error
    assert result.progress_flag == "--progress"
    assert "rejected --info=progress2" in result.fallback_note
    assert "--stats, no percentage" not in result.fallback_note   # progress is real
    assert result.bytes_done == total
    assert updates and updates[-1].percent == 100.0


async def test_a_rsync_that_refuses_every_progress_flag_still_copies(tmp_path):
    """The last resort: no progress at all, and the job is not lost over it."""
    total = _two_files(tmp_path / "src")
    command = _fake_rsync(tmp_path, rejects=("--info=progress2", "--progress"))
    transport = rep.SshTransport(rsync_command=command)
    updates: list[rep.Progress] = []

    result = await transport.copy(rep.Endpoint("A"), rep.Endpoint("B"),
                                  str(tmp_path / "src"), str(tmp_path / "dst"),
                                  on_progress=updates.append, total_bytes=total)

    assert result.ok, result.error
    assert result.progress_flag == ""             # ran without a progress flag
    assert "--progress" in result.fallback_note   # and the report says why
    assert "--stats" in result.fallback_note
    assert updates == []                          # nothing to show, nothing faked
    assert result.bytes_done == total             # bytes come from --stats
    assert (tmp_path / "dst" / "a.bin").exists()


async def test_a_real_copy_of_two_small_files_reports_bytes_and_100_percent(
        tmp_path):
    """The local rsync itself, whatever it is — openrsync here, GNU elsewhere."""
    if shutil.which("rsync") is None:
        pytest.skip("no rsync on this machine")
    total = _two_files(tmp_path / "src")
    flavour = subprocess.run(["rsync", "--version"], capture_output=True,
                             text=True, timeout=20).stdout
    transport = rep.SshTransport()
    updates: list[rep.Progress] = []

    result = await transport.copy(rep.Endpoint("A"), rep.Endpoint("B"),
                                  str(tmp_path / "src"), str(tmp_path / "dst"),
                                  on_progress=updates.append, total_bytes=total)

    assert result.ok, result.error
    if "openrsync" in flavour:
        assert result.progress_flag == "--progress"
    else:
        assert result.progress_flag == "--info=progress2"
    assert result.bytes_done == total
    assert updates and updates[-1].percent == 100.0
    assert (tmp_path / "dst" / "a.bin").read_bytes() == (tmp_path / "src" / "a.bin").read_bytes()


# ---------------------------------------------------------------------------
# The endpoints: key, node, disabled switch
# ---------------------------------------------------------------------------
async def test_the_endpoints_require_the_admin_key(stub_factory, ripple_factory,
                                                   tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    harness.ripple.replication_factory = lambda cfg: FakeTransport(
        source_models=[_model()])
    assert (await harness.client.get("/admin/nodes/B/replication")).status_code == 401
    assert (await harness.client.post("/admin/nodes/B/replicate", json={})
            ).status_code == 401
    assert (await harness.client.delete("/admin/nodes/B/replication")
            ).status_code == 401
    assert (await harness.client.get(
        "/admin/replication/plan?source=A&target=B&model=m")).status_code == 401
    # With the key: an unknown node is a 404, not a 500.
    assert (await harness.client.get("/admin/nodes/nope/replication",
                                     headers=ADMIN)).status_code == 404
    assert (await harness.client.post("/admin/nodes/nope/replicate", json={},
                                      headers=ADMIN)).status_code == 404


async def test_replication_disabled_refuses_instead_of_copying(stub_factory,
                                                               ripple_factory,
                                                               tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b, enabled=False))
    fake = FakeTransport(source_models=[_model()])
    harness.ripple.replication_factory = lambda cfg: fake

    resp = await harness.client.post("/admin/nodes/B/replicate",
                                     json={"source": "A", "model": QWEN},
                                     headers=ADMIN)
    assert resp.status_code == 409
    assert "not enabled" in resp.json()["error"]["message"]
    assert fake.calls == []                      # nothing was even probed


# ---------------------------------------------------------------------------
# The job: copy, register, settings round trip, report
# ---------------------------------------------------------------------------
async def _start(harness, fake, *, overwrite: bool = False):
    harness.ripple.replication_factory = lambda cfg: fake
    return await harness.client.post("/admin/nodes/B/replicate",
                                     json={"source": "A", "model": QWEN,
                                           "overwrite": overwrite},
                                     headers=ADMIN)


async def _job(harness, name: str = "B") -> dict | None:
    resp = await harness.client.get(f"/admin/nodes/{name}/replication", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()["job"]


async def _wait_settled(harness, name: str = "B", timeout: float = 15.0) -> dict:
    """Poll the GET until the job leaves `running`."""
    seen: dict[str, dict] = {}

    async def poll() -> None:
        while True:
            job = await _job(harness, name)
            if job is not None and job["state"] != "running":
                seen["job"] = job
                return
            await asyncio.sleep(0.02)

    task = asyncio.create_task(poll())
    try:
        await wait_until(lambda: "job" in seen, timeout=timeout)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert "job" in seen, "the job never left `running`"
    return seen["job"]


async def test_the_happy_path_copies_registers_and_reports(stub_factory,
                                                           ripple_factory, tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()])
    fake.release.set()

    resp = await _start(harness, fake)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["plan"]["ok"] is True
    assert body["plan"]["size_gb"] == pytest.approx(111.0)
    assert body["plan"]["free_gb"] == pytest.approx(2000.0)

    job = await _wait_settled(harness)
    assert job["state"] == "done", job
    assert job["registered"] is True
    assert fake.reload_calls == ["B"]
    assert fake.moves == [(f"{TARGET_ROOT}/{rep.STAGING_DIR}/Jundot/{QWEN}",
                           f"{TARGET_ROOT}/Jundot/{QWEN}")]
    # The report names bytes and MB/s (issue #13, point 7).
    assert job["report"]["bytes"] == 111 * GB
    assert job["report"]["seconds"] > 0
    assert job["report"]["mb_per_s"] > 0
    assert job["report"]["not_replicated"] == []
    # The copy went into the dot-directory first, never straight into place.
    assert fake.mkdirs == [f"{TARGET_ROOT}/{rep.STAGING_DIR}/Jundot/{QWEN}"]


async def test_the_settings_round_trip_reads_puts_and_reads_back(stub_factory,
                                                                 ripple_factory,
                                                                 tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()])
    fake.release.set()

    assert (await _start(harness, fake)).status_code == 202
    job = await _wait_settled(harness)

    assert job["state"] == "done", job
    # What was written is what the GET reported, minus the three keys the PUT
    # rejects and minus the nulls — not a hand-built list of guesses.
    assert fake.put_payload == {
        "max_context_window": 524288, "temperature": 0.7, "top_p": 0.95,
        "enable_thinking": True,
    }
    assert job["settings"]["sent"] == 4
    assert job["settings"]["compared"] == 4
    assert job["settings"]["mismatches"] == []
    assert job["settings"]["error"] is None


async def test_a_settings_mismatch_is_reported_instead_of_claimed_as_success(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    # The target keeps everything but the temperature: the read-back differs.
    fake = FakeTransport(source_models=[_model()], drop_settings=("temperature",))
    fake.release.set()

    assert (await _start(harness, fake)).status_code == 202
    job = await _wait_settled(harness)

    assert job["state"] == "failed"
    assert job["registered"] is True            # the model *is* there
    assert [m["key"] for m in job["settings"]["mismatches"]] == ["temperature"]
    assert "settings differ" in job["error"]
    assert any("temperature" in item for item in job["report"]["not_replicated"])


async def test_a_rejected_settings_body_is_reported(stub_factory, ripple_factory,
                                                    tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()], put_status=422)
    fake.release.set()

    assert (await _start(harness, fake)).status_code == 202
    job = await _wait_settled(harness)
    assert job["state"] == "failed"
    assert "422" in job["settings"]["error"]
    assert "rejected" in job["error"]


async def test_a_target_that_does_not_list_the_model_after_reload_fails(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()], register=False)
    fake.release.set()

    assert (await _start(harness, fake)).status_code == 202
    job = await _wait_settled(harness)
    assert job["state"] == "failed"
    assert job["registered"] is False
    assert "is not listed on B" in job["error"]


async def test_a_size_mismatch_is_named_not_smoothed_over(stub_factory,
                                                          ripple_factory, tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()])
    original = fake.reload

    async def reload_and_shrink(ep):
        ok, msg = await original(ep)
        fake.target_models = [
            dataclasses.replace(m, estimated_size=m.estimated_size - 1)
            for m in fake.target_models]
        return ok, msg

    fake.reload = reload_and_shrink       # type: ignore[assignment]
    fake.release.set()

    assert (await _start(harness, fake)).status_code == 202
    job = await _wait_settled(harness)
    assert job["state"] == "failed"
    assert any("estimated_size differs" in item
               for item in job["report"]["not_replicated"])


# ---------------------------------------------------------------------------
# The maintenance window during the job
# ---------------------------------------------------------------------------
async def test_the_target_is_out_of_the_pool_while_the_job_runs(stub_factory,
                                                                ripple_factory,
                                                                tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()])
    # `release` stays unset: the job sits in the copy.

    assert (await _start(harness, fake)).status_code == 202
    await wait_until(lambda: fake.copy_started.is_set())

    window = await harness.client.get("/admin/nodes/B/maintenance", headers=ADMIN)
    assert window.status_code == 200
    assert window.json()["maintenance"]["by"] == "replication"
    assert window.json()["in_pool"] is False

    health = await harness.healthz()
    row = next(h for h in health["hosts"] if h["name"] == "B")
    assert row["maintenance"]["by"] == "replication"

    fake.release.set()
    job = await _wait_settled(harness)
    assert job["state"] == "done"
    # Back in the pool afterwards (issue #13, acceptance).
    window = await harness.client.get("/admin/nodes/B/maintenance", headers=ADMIN)
    assert window.json()["maintenance"] is None
    assert window.json()["in_pool"] is True


async def test_an_operator_window_is_neither_replaced_nor_cleared(stub_factory,
                                                                  ripple_factory,
                                                                  tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()])
    fake.release.set()
    harness.ripple.maintenance.set("B", minutes=30, by="dashboard", note="update")
    harness.ripple._apply_windows()

    assert (await _start(harness, fake)).status_code == 202
    job = await _wait_settled(harness)
    assert job["state"] == "done"
    window = await harness.client.get("/admin/nodes/B/maintenance", headers=ADMIN)
    assert window.json()["maintenance"]["by"] == "dashboard"
    assert window.json()["maintenance"]["minutes_left"] > 0


# ---------------------------------------------------------------------------
# The cancel path
# ---------------------------------------------------------------------------
async def test_a_cancelled_copy_registers_nothing_and_leaves_the_pool_clean(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()])

    assert (await _start(harness, fake)).status_code == 202
    await wait_until(lambda: fake.copy_started.is_set())
    assert (await _job(harness))["state"] == "running"

    cancelled = await harness.client.delete("/admin/nodes/B/replication",
                                            headers=ADMIN)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancelling"] is True

    job = await _wait_settled(harness)
    assert job["state"] == "cancelled"
    assert job["step"] == "cancelled"
    # Nothing was registered: no reload, no move into place, no settings write.
    assert fake.reload_calls == []
    assert fake.moves == []
    assert fake.put_payload is None
    assert job["registered"] is False
    # The staging copy is kept, so the next run resumes instead of starting over.
    assert fake.mkdirs == [f"{TARGET_ROOT}/{rep.STAGING_DIR}/Jundot/{QWEN}"]
    assert "resumes" in job["error"]
    # And the target is back in service.
    window = await harness.client.get("/admin/nodes/B/maintenance", headers=ADMIN)
    assert window.json()["maintenance"] is None
    assert window.json()["in_pool"] is True


async def test_cancelling_nothing_is_a_409(stub_factory, ripple_factory, tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    harness.ripple.replication_factory = lambda cfg: FakeTransport()
    resp = await harness.client.delete("/admin/nodes/B/replication", headers=ADMIN)
    assert resp.status_code == 409
    assert "no replication" in resp.json()["error"]["message"]


async def test_one_job_per_target(stub_factory, ripple_factory, tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()])

    assert (await _start(harness, fake)).status_code == 202
    await wait_until(lambda: fake.copy_started.is_set())
    second = await _start(harness, fake)
    assert second.status_code == 409
    assert "already running" in second.json()["error"]["message"]

    fake.release.set()
    await _wait_settled(harness)


# ---------------------------------------------------------------------------
# The plan endpoint is the dialog's source of truth
# ---------------------------------------------------------------------------
async def test_the_plan_endpoint_refuses_before_the_post(stub_factory,
                                                         ripple_factory, tmp_path):
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()], free_bytes=int(50 * GB))
    harness.ripple.replication_factory = lambda cfg: fake

    plan = await harness.client.get(
        f"/admin/replication/plan?source=A&target=B&model={QWEN}", headers=ADMIN)
    assert plan.status_code == 200
    body = plan.json()
    assert body["ok"] is False
    assert "not enough space" in " ".join(body["errors"])
    # The dialog's numbers are the same numbers the POST would use.
    assert body["size_bytes"] == 111 * GB
    assert body["free_bytes"] == 50 * GB

    refused = await harness.client.post("/admin/nodes/B/replicate",
                                        json={"source": "A", "model": QWEN},
                                        headers=ADMIN)
    assert refused.status_code == 409
    assert "not enough space" in refused.json()["error"]["message"]
    assert fake.copy_started.is_set() is False
    assert fake.mkdirs == []                    # not one byte was written


async def test_the_plan_endpoint_refuses_a_home_relative_target_root(
        stub_factory, ripple_factory, tmp_path):
    """`~` never reaches the dialog as a target path — it is refused there.

    The target's root comes from the fleet-wide fallback `replication.model_roots`
    on purpose: both spellings get the same treatment (issue #20).
    """
    home = os.path.expanduser("~")
    a = stub_factory()
    b = stub_factory()
    cfg = _cfg(tmp_path, a, b)
    cfg.hosts[0].replication.model_roots = ["~/.omlx/models"]
    cfg.hosts[1].replication.model_roots = []
    cfg.replication.model_roots = ["~/.omlx/models"]
    harness = await ripple_factory(cfg)
    fake = FakeTransport(
        source_models=[_model(path=f"{home}/.omlx/models/Jundot/{QWEN}")])
    harness.ripple.replication_factory = lambda cfg: fake

    plan = await harness.client.get(
        f"/admin/replication/plan?source=A&target=B&model={QWEN}", headers=ADMIN)
    body = plan.json()
    assert body["ok"] is False
    errors = " ".join(body["errors"])
    assert "~/.omlx/models" in errors and "of B" in errors
    # The source side was expanded, so the subpath is still known; the target
    # side was not, so nothing derived from it is in the answer.
    assert body["relative"] == f"Jundot/{QWEN}"
    assert body["target_path"] == "" and body["staging_path"] == ""
    assert "~" not in body["target_root"] + body["target_path"] + body["staging_path"]

    refused = await harness.client.post("/admin/nodes/B/replicate",
                                        json={"source": "A", "model": QWEN},
                                        headers=ADMIN)
    assert refused.status_code == 409
    assert "~/.omlx/models" in refused.json()["error"]["message"]
    assert fake.copy_started.is_set() is False
    assert fake.mkdirs == []                    # not one byte was written


# ---------------------------------------------------------------------------
# The one operation that cannot be undone
# ---------------------------------------------------------------------------
async def test_a_destination_that_is_the_root_itself_is_never_moved_into(
        stub_factory, ripple_factory, tmp_path):
    """`move` runs `rm -rf` on the destination: an empty relative path would wipe
    every model on the target. The job refuses instead of trusting the preflight."""
    a = stub_factory()
    b = stub_factory()
    harness = await ripple_factory(_cfg(tmp_path, a, b))
    fake = FakeTransport(source_models=[_model()])
    fake.release.set()
    harness.ripple.replication_factory = lambda cfg: fake

    # Build the plan by hand: relative "" and target_path == root, which the
    # preflight would refuse — this test is about the job's own guard.
    plan = rep.Plan(source="A", target="B", model=QWEN, source_path=_model().model_path,
                    relative="", target_root=TARGET_ROOT, target_path=TARGET_ROOT,
                    staging_path=rep.staging_for(TARGET_ROOT, ""),
                    size_bytes=111 * GB, required_bytes=int(111 * GB * 1.05),
                    free_bytes=2_000 * GB, ssh_target="user@203.0.113.2")
    job = rep.new_job(plan)
    await rep.run_job(job=job, plan=plan, transport=fake,
                      source=rep.Endpoint("A"), target=rep.Endpoint("B"))

    assert job["state"] == "failed"
    assert "not the root itself" in job["error"]
    assert fake.moves == []
    assert fake.mkdirs == []


# ---------------------------------------------------------------------------
# A copy whose rsync refuses the progress flag (issue #26)
# ---------------------------------------------------------------------------
class CopyingTransport(FakeTransport):
    """A `FakeTransport` whose copy step is the real `SshTransport`.

    Everything else stays fake — the model lists, the reload, the settings — so
    the job runs end to end while the rsync in it is a real process.
    """

    def __init__(self, rsync_command: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rsync = rep.SshTransport(rsync_command=rsync_command)

    async def copy(self, src, dst, src_path, dst_path, *, on_progress=None,
                   cancel=None, total_bytes=0):
        self.copy_started.set()
        result = await self.rsync.copy(src, dst, src_path, dst_path,
                                       on_progress=on_progress, cancel=cancel,
                                       total_bytes=total_bytes)
        self.copy_finished.set()
        return result


async def test_the_job_finishes_when_no_progress_flag_survives(tmp_path):
    """A rsync that refuses both flags: the job completes and says so.

    The whole point of issue #26 is that the copy step must not die on an
    option. The job reaches `done`, the bytes come from `--stats`, and the
    report names the fallback instead of showing a silent 0 %.
    """
    source_root = tmp_path / "models"
    target_root = tmp_path / "target"
    total = _two_files(source_root / "Jundot" / QWEN)
    command = _fake_rsync(tmp_path, rejects=("--info=progress2", "--progress"))
    fake = CopyingTransport(command, source_models=[
        _model(size=total, path=str(source_root / "Jundot" / QWEN))])
    # Both sides local, built by hand: rsync takes one remote side at most, and
    # what is under test here is the flag, not the addressing.
    plan = rep.Plan(source="A", target="B", model=QWEN,
                    source_path=str(source_root / "Jundot" / QWEN),
                    relative=f"Jundot/{QWEN}",
                    target_root=str(target_root),
                    target_path=str(target_root / "Jundot" / QWEN),
                    staging_path=rep.staging_for(str(target_root), f"Jundot/{QWEN}"),
                    size_bytes=total, required_bytes=int(total * 1.05),
                    free_bytes=2_000 * GB, ssh_target="")
    job = rep.new_job(plan)

    await rep.run_job(job=job, plan=plan, transport=fake,
                      source=rep.Endpoint("A"), target=rep.Endpoint("B"))

    assert job["state"] == "done", job
    assert job["percent"] == 100.0                # finished is finished
    assert job["bytes_done"] == total             # from `--stats`, not invented
    assert job["report"]["progress"] == "none"
    assert "--progress" in job["report"]["progress_note"]
    assert job["report"]["not_replicated"] == []  # a fallback is not a failure
    assert job["report"]["bytes"] == total
    assert job["report"]["mb_per_s"] > 0
    # The copy really happened, without a progress flag to lean on. It sits in
    # the staging directory: `move` is the fake's no-op, not rsync's.
    staged = (pathlib.Path(rep.staging_for(str(target_root), f"Jundot/{QWEN}"))
              / "a.bin")
    assert staged.stat().st_size == total // 2


async def test_the_job_reports_the_progress_flag_it_used(tmp_path):
    """The happy path names its flag, so a report is never silent about it."""
    if shutil.which("rsync") is None:
        pytest.skip("no rsync on this machine")
    source_root = tmp_path / "models"
    target_root = tmp_path / "target"
    total = _two_files(source_root / "Jundot" / QWEN)
    fake = CopyingTransport("rsync", source_models=[
        _model(size=total, path=str(source_root / "Jundot" / QWEN))])
    plan = rep.Plan(source="A", target="B", model=QWEN,
                    source_path=str(source_root / "Jundot" / QWEN),
                    relative=f"Jundot/{QWEN}",
                    target_root=str(target_root),
                    target_path=str(target_root / "Jundot" / QWEN),
                    staging_path=rep.staging_for(str(target_root), f"Jundot/{QWEN}"),
                    size_bytes=total, required_bytes=int(total * 1.05),
                    free_bytes=2_000 * GB, ssh_target="")
    job = rep.new_job(plan)

    await rep.run_job(job=job, plan=plan, transport=fake,
                      source=rep.Endpoint("A"), target=rep.Endpoint("B"))

    if shutil.which("rsync") is None:
        pytest.skip("no rsync on this machine")
    assert job["state"] == "done", job
    assert job["report"]["progress"] in ("--info=progress2", "--progress")
    assert job["report"]["progress_note"] == ""
    assert job["percent"] == 100.0


# ---------------------------------------------------------------------------
# The local side never goes over SSH (issue #28)
# ---------------------------------------------------------------------------
# The live shape, 16.11.2026: `replication.address` is set for *both* nodes (the
# Thunderbolt link needs it on the remote side) and only the entry host lists a
# loopback address. That combination made the first real copy die on rsync's
# "both source and destination cannot be remote files".
LIVE_SHAPE = """
replication:
  enabled: true
  admin_key: omlx-key
hosts:
  - name: alpha
    role: entry
    node_id: 11111111-1111-4111-8111-111111111111
    addresses:
      - http://192.0.2.11:8000
      - http://203.0.113.1:8000
      - http://localhost:8000
    replication:
      address: user@203.0.113.1
      ssh_user: vogel
      model_roots:
        - /Users/operator/.omlx/models
        - /Volumes/models
  - name: beta
    role: worker
    node_id: 22222222-2222-4222-8222-222222222222
    addresses:
      - http://192.0.2.12:8000
      - http://203.0.113.2:8000
      - http://198.51.100.49:8000
    replication:
      address: user@203.0.113.2
      ssh_user: vogel
      model_roots:
        - /Users/operator/.omlx/models
        - /Users/operator/.omlx/gguf
"""


def _live(tmp_path, text: str = LIVE_SHAPE):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def _plan_for(cfg, source: str, target: str) -> rep.Plan:
    """The preflight exactly as `proxy.py` builds it, for a configured pair."""
    by_name = {h.name: h for h in cfg.hosts}
    src, tgt = by_name[source], by_name[target]
    return rep.preflight(
        source=source, target=target, model=QWEN, entry=_model(),
        source_roots=rep.model_roots_for(src, cfg),
        target_roots=rep.model_roots_for(tgt, cfg),
        overwrite=False, free_bytes=2_000 * GB, target_has_model=False,
        ssh_ok=True, ssh_error=None,
        ssh_target=rep.endpoint_for(tgt, cfg).ssh_target,
        local=rep.local_side(cfg))


def _operands(argv: list[str]) -> tuple[str, str]:
    """The two operands of the rsync argv: source spec, destination spec."""
    return argv[-2], argv[-1]


def test_a_loopback_entry_is_the_only_marker_and_nothing_else_is():
    """The M5 lists `http://localhost:8000`; a LAN or TB address is not a marker."""
    assert rep.is_loopback_address("http://localhost:8000")
    assert rep.is_loopback_address("localhost:8000")
    assert rep.is_loopback_address("http://127.0.0.1:8000")
    assert rep.is_loopback_address("http://[::1]:8000")
    # The node RIPPLE runs on is *not* identified by its routable addresses —
    # that reading is what produced the remote→remote argv in the first place.
    assert not rep.is_loopback_address("http://192.0.2.11:8000")
    assert not rep.is_loopback_address("http://203.0.113.1:8000")
    assert not rep.is_loopback_address("")
    assert not rep.is_loopback_address(None)


def test_the_live_configuration_names_the_entry_host_as_this_machine(tmp_path):
    cfg = _live(tmp_path)
    assert rep.local_host_name(cfg) == "alpha"
    assert rep.host_is_local(cfg.hosts[0], cfg) is True
    assert rep.host_is_local(cfg.hosts[1], cfg) is False
    # The M5's address is configured and still ignored — visibly, not silently.
    local = rep.local_side(cfg)
    assert local == rep.LocalSide(name="alpha",
                                  ignored_address="user@203.0.113.1")
    assert rep.ssh_target_for(cfg.hosts[0], cfg) == ""
    assert rep.ssh_target_for(cfg.hosts[1], cfg) == "user@203.0.113.2"


def test_the_forward_copy_has_one_remote_spec_and_one_local_path(tmp_path):
    """M5 → M3: the M5's `replication.address` is configured, ignored and named."""
    cfg = _live(tmp_path)
    src = rep.endpoint_for(cfg.hosts[0], cfg)
    tgt = rep.endpoint_for(cfg.hosts[1], cfg)
    assert src.ssh_target == ""                        # this machine: no SSH
    assert tgt.ssh_target == "user@203.0.113.2"

    plan = _plan_for(cfg, "alpha", "beta")
    assert plan.ok, plan.errors

    argv = rep.SshTransport(rsync_command="rsync")._rsync_argv(
        src, tgt, plan.source_path, plan.staging_path, progress_flag="--progress")
    src_spec, dst_spec = _operands(argv)
    remote = [s for s in (src_spec, dst_spec) if ":" in s]
    assert len(remote) == 1 and remote[0] == dst_spec  # rsync takes at most one
    assert src_spec == plan.source_path.rstrip("/") + "/"   # a plain path
    assert dst_spec.startswith("user@203.0.113.2:")

    warned = " | ".join(plan.warnings)
    assert "alpha" in warned and "user@203.0.113.1" in warned
    assert "ignored" in warned and "source" in warned


def test_the_reverse_copy_has_a_local_destination(tmp_path):
    """M3 → M5: the destination is a plain path, the source is the remote one."""
    cfg = _live(tmp_path)
    src = rep.endpoint_for(cfg.hosts[1], cfg)
    tgt = rep.endpoint_for(cfg.hosts[0], cfg)
    assert src.ssh_target == "user@203.0.113.2"
    assert tgt.ssh_target == ""                        # this machine: no SSH

    plan = _plan_for(cfg, "beta", "alpha")
    # An empty ssh_target for the target is not a refusal: it is the local side.
    assert plan.ok, plan.errors

    argv = rep.SshTransport(rsync_command="rsync")._rsync_argv(
        src, tgt, plan.source_path, plan.staging_path, progress_flag="--progress")
    src_spec, dst_spec = _operands(argv)
    remote = [s for s in (src_spec, dst_spec) if ":" in s]
    assert len(remote) == 1 and remote[0] == src_spec
    assert dst_spec == plan.staging_path.rstrip("/") + "/"  # a plain path

    warned = " | ".join(plan.warnings)
    assert "user@203.0.113.1" in warned and "ignored" in warned
    assert "target" in warned


def test_no_loopback_address_refuses_the_plan_and_names_the_fix(tmp_path):
    """Without a local host the two sides are indistinguishable: no plan at all."""
    cfg = _live(tmp_path, LIVE_SHAPE.replace("      - http://localhost:8000\n", ""))
    assert rep.local_host_name(cfg) == ""
    assert rep.local_side(cfg) == rep.LocalSide()

    plan = _plan_for(cfg, "alpha", "beta")
    assert plan.ok is False
    errors = " ".join(plan.errors)
    assert "no host is identifiable as this machine" in errors
    assert "hosts[].addresses" in errors and "loopback" in errors
    assert "http://localhost:8000" in errors      # what to write, not only what is wrong
    # No path was derived from a side that could not be identified as local.
    assert plan.warnings == []


def test_a_copy_between_two_remote_hosts_is_refused():
    """A third node as "this machine" makes both sides remote — also no argv."""
    plan = _plan(local=rep.LocalSide(name="M4-gamma"))
    assert plan.ok is False
    errors = " ".join(plan.errors)
    assert "neither A nor B is this machine (M4-gamma)" in errors
    assert "both" in errors and "remote" in errors


async def test_a_plan_without_a_local_host_never_reaches_rsync(
        stub_factory, ripple_factory, tmp_path):
    """The refusal is in the plan, so rsync is never even asked: no argv exists."""
    a = stub_factory()
    b = stub_factory()
    cfg = _cfg(tmp_path, a, b)
    # The stub servers answer on loopback, which would mark A as this machine.
    # The fleet's own addresses are what a real deployment has, and they say
    # nothing about where RIPPLE runs.
    cfg.hosts[0].addresses = ["http://192.0.2.11:8000"]
    cfg.hosts[1].addresses = ["http://192.0.2.12:8000"]
    harness = await ripple_factory(cfg)
    fake = FakeTransport(source_models=[_model()])
    harness.ripple.replication_factory = lambda cfg: fake

    plan = await harness.client.get(
        f"/admin/replication/plan?source=A&target=B&model={QWEN}", headers=ADMIN)
    body = plan.json()
    assert body["ok"] is False
    assert "hosts[].addresses" in " ".join(body["errors"])
    assert body["warnings"] == []

    refused = await harness.client.post("/admin/nodes/B/replicate",
                                        json={"source": "A", "model": QWEN},
                                        headers=ADMIN)
    assert refused.status_code == 409
    assert "loopback" in refused.json()["error"]["message"]
    assert fake.copy_started.is_set() is False
    assert fake.mkdirs == []                    # no mkdir, no rsync, no argv
