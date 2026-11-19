"""The pictures in `docs/images/` — one run, one fleet, one manifest (issue #104).

Nineteen of the twenty pictures used to be cut by hand, one at a time, with a
clip and a scale that lived nowhere but in someone's shell history. They were
all a day old and showed a dashboard that no longer existed. The rule now is the
repository's rule: the result is the script.

These tests hold the three promises that make that work, and none of them needs
a browser:

- **every picture the documents show is produced by a crop** — the names are
  read out of `docs/manual-en.md` and the two install guides, so a new picture
  in a document fails here until the tool grows a crop for it;
- **the payloads are a documented test fleet** — documentation addresses,
  `node-a`/`node-b`/`node-c`, invented model names, and no identifier from a
  production installation anywhere in the shim;
- **the manifest matches the files** — every PNG on disk is listed with its
  real pixel size and byte count, so a picture older than the manifest is
  visible instead of arguable.
"""

from __future__ import annotations

import json
import pathlib
import re
import struct
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import mockup_fleet as fleet  # noqa: E402
from tools import mockup_snapshot as ms  # noqa: E402

IMAGES = ROOT / "docs" / "images"
MANIFEST = IMAGES / "MANIFEST.md"

# The picture the README carries is not a crop: it is `--screenshot`, the whole
# Fleet tab at 2x, and it has its own path in the tool.
README_PICTURE = "readme-fleet.png"

# The four the install guides carry are crops like the manual's, but they hang
# off install.md / install-de.md rather than the manual.
INSTALL_DOCS = ("install.md", "install-de.md")


def _references(doc: pathlib.Path) -> list[str]:
    text = doc.read_text(encoding="utf-8")
    return sorted(set(re.findall(r"images/([A-Za-z0-9._-]+\.png)", text)))


def _crop_names() -> set[str]:
    return {crop.name + ".png" for crop in ms.CROPS}


def png_size(path: pathlib.Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "%s is not a PNG" % path.name
    return struct.unpack(">II", head[16:24])


# ── every picture a document shows comes out of the tool ────────────────────
def test_every_picture_the_manual_shows_is_a_crop():
    manual = ROOT / "docs" / "manual-en.md"
    if not manual.is_file():
        pytest.skip("the manual is not in this tree")
    referenced = _references(manual)
    assert referenced, "the manual references no pictures at all"
    produced = _crop_names() | {README_PICTURE}
    missing = [name for name in referenced if name not in produced]
    assert not missing, "the manual shows pictures no crop produces: %s" % missing


def test_every_picture_the_install_guides_show_is_a_crop():
    produced = _crop_names() | {README_PICTURE}
    for name in INSTALL_DOCS:
        doc = ROOT / "docs" / name
        if not doc.is_file():
            continue
        missing = [p for p in _references(doc) if p not in produced]
        assert not missing, "%s shows pictures no crop produces: %s" % (name, missing)


def test_the_fifteen_manual_crops_are_the_ones_the_manual_names():
    # The issue names fifteen; the tool must produce exactly those plus the
    # four install views. A crop nobody shows is as much a drift as a picture
    # nobody produces.
    manual = ROOT / "docs" / "manual-en.md"
    if not manual.is_file():
        pytest.skip("the manual is not in this tree")
    manual_crops = {c.name for c in ms.CROPS if c.name.startswith("manual-")}
    referenced = {n[:-4] for n in _references(manual)}
    assert manual_crops == referenced


def test_crop_names_are_unique_and_readme_is_not_a_crop():
    names = [c.name for c in ms.CROPS]
    assert len(names) == len(set(names))
    assert "readme-fleet" not in names


def test_every_crop_names_a_tab_the_page_has_and_says_what_it_shows():
    tabs = {slug for slug, _, _ in ms.TABS} | {"keys"}
    for crop in ms.CROPS:
        assert crop.tab in tabs, "%s: unknown tab %r" % (crop.name, crop.tab)
        assert crop.what and len(crop.what) > 10, "%s: no description" % crop.name
        assert crop.mode in ("detail", "full")
        if crop.mode == "detail":
            assert crop.selector, "%s: a detail crop needs an element" % crop.name
        else:
            assert crop.selector is None, "%s: a full view is the viewport" % crop.name


# ── the fleet the pictures show ─────────────────────────────────────────────
def _payloads() -> str:
    return json.dumps(fleet.endpoints(1_763_000_000.0, commit="1e89c89"),
                      ensure_ascii=False)


def test_the_payloads_carry_no_production_identifier():
    blob = _payloads()
    for word in ("M5-alpha", "M3-beta", "DeepSeek", "Qwen", "MarkItDown", "GLM",
                 "10.11.12", "10.30.1", "192.168.77"):
        assert word not in blob, "%r is a production identifier" % word
    # …and the addresses that are there are documentation addresses.
    addresses = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", blob)
    assert addresses
    for address in addresses:
        assert (address.startswith("192.0.2.") or address.startswith("198.51.100.")
                or address == "127.0.0.1"), address


def test_the_fleet_is_the_one_the_manual_describes():
    health = fleet.healthz(1_763_000_000.0)
    assert [h["name"] for h in health["hosts"]] == ["node-a", "node-b"]
    assert {h["role"] for h in health["hosts"]} == {"entry", "worker"}
    assert fleet.node_preview()["name"] == "node-c"
    # node-c is previewed, not part of the fleet: two cards, not three.
    assert len(health["hosts"]) == 2


def test_the_memory_bar_adds_up_to_the_models_it_sits_above():
    """The measured number is the sum of the loaded models' `actual_size`."""
    health = fleet.healthz(1_763_000_000.0)
    for host in health["hosts"]:
        loaded = [row for model in health["models"] for row in model["hosts"]
                  if row["host"] == host["name"] and row["loaded"]]
        assert sum(row["actual_size"] or 0 for row in loaded) == \
            host["used_bytes_measured"], host["name"]
        assert sum(row["estimated_size"] or 0 for row in loaded) == \
            host["used_bytes"], host["name"]


def test_the_history_carries_several_threads_including_an_fp8_one():
    decisions = fleet.stats(1_763_000_000.0)["decisions"]
    threads = {d["thread"] for d in decisions}
    assert len(decisions) >= 10
    assert len(threads) >= 4
    assert any(t.endswith("#fp8") for t in threads)
    # oldest first: the page reverses a copy for display (issue #101).
    stamps = [d["ts"] for d in decisions]
    assert stamps == sorted(stamps)


def test_one_thread_is_busy_and_one_model_is_not_loaded():
    health = fleet.healthz(1_763_000_000.0)
    registry = health["threads"]["registry"]
    assert len(registry) == 6
    assert sum(1 for t in registry if t.get("active")) == 1
    assert any(not x["loaded"] for m in health["models"] for x in m["hosts"])


def test_the_nodes_tab_has_a_scan_a_preview_and_a_drain_timeout():
    scan = fleet.discover()
    assert scan["count"] == len(scan["found"]) == 2
    assert any(f["known"] for f in scan["found"])
    assert fleet.node_preview()["step"] is None
    timeout = fleet.drain_timeout()["error"]
    assert timeout["code"] == "drain_timeout"
    assert timeout["inflight"] == 2


def test_ops_has_a_maintenance_window():
    window = fleet.maintenance_set(1_763_000_000.0)["maintenance"]
    assert window["until"] and window["minutes_left"] == 24.0


def test_the_fleet_card_has_a_series_with_real_samples():
    health = fleet.healthz(1_763_000_000.0)
    series = health["hosts"][0]["series"][0]
    assert series["model"] in health["hosts"][0]["metrics"]
    samples = [v for v in series["total"] if v is not None]
    assert len(samples) >= 5, "the line needs several measured points"
    assert any(v == 0.0 for v in series["total"]), "the empty tail is a real zero"


def test_the_shim_answers_the_three_endpoints_and_the_admin_calls():
    script = fleet.shim_script(1_763_000_000.0, commit="1e89c89")
    for path in ("/healthz", "/stats", "/config", "/admin/keys",
                 "/admin/discover", "dry_run"):
        assert path in script, path
    assert "window.confirm" in script


# ── the files and the manifest ─────────────────────────────────────────────
def _manifest_rows() -> dict[str, tuple[int, int, int]]:
    text = MANIFEST.read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        m = re.match(r"\| `([^`]+)` \| (\d+) × (\d+) \| (\d+) \|", line)
        if m:
            rows[m.group(1)] = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return rows


def test_the_manifest_names_the_command_the_commit_and_the_date():
    text = MANIFEST.read_text(encoding="utf-8")
    assert "mockup_snapshot.py" in text
    assert "--manual-crops" in text
    assert re.search(r"\*\*Commit\*\* \| `[0-9a-f]{7,}`", text)
    assert re.search(r"\*\*Date\*\* \| \d{4}-\d\d-\d\dT", text)


def test_every_picture_on_disk_is_in_the_manifest_with_its_real_size():
    rows = _manifest_rows()
    on_disk = {p.name for p in IMAGES.glob("*.png")}
    assert on_disk, "no pictures at all"
    assert on_disk == set(rows), "manifest and directory disagree: %s" % (
        on_disk ^ set(rows))
    for name, (width, height, size) in rows.items():
        path = IMAGES / name
        assert path.stat().st_size == size, "%s: byte count moved" % name
        assert png_size(path) == (width, height), "%s: pixels moved" % name


def test_every_picture_is_non_empty_and_has_plausible_dimensions():
    for path in sorted(IMAGES.glob("*.png")):
        width, height = png_size(path)
        assert path.stat().st_size > 2000, "%s is a stub" % path.name
        assert width >= 600 and height >= 80, "%s is %d x %d" % (path.name, width, height)
        if path.name == README_PICTURE:
            assert width == 2880, "the README picture is the Fleet tab at 2x"
        elif path.name.startswith("manual-") and path.name in (
                "manual-fleet.png", "manual-models.png", "manual-threads.png",
                "manual-history.png", "manual-ops.png", "manual-nodes.png",
                "manual-keys.png", "dashboard-fleet.png", "dashboard-fleet-de.png",
                "dashboard-models.png", "dashboard-models-de.png"):
            assert width == 1440, "%s is a full view, 1440 wide" % path.name
        else:
            assert width % 2 == 0 and height % 2 == 0, (
                "%s is a detail crop at 2x" % path.name)
