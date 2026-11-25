"""The manual travels with the release, not only with the build machine (#148).

`dist/ripple-manual-en.pdf` is generated and stays out of git; an asset hangs on
the release instead, which is the only place a reader can link it. These tests
pin the two properties that make that dependable: the upload names the file and
`--clobber` (a corrected manual must replace the old one, not fail on the name),
and a missing manual stops the release with the command that builds it.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _module():
    spec = importlib.util.spec_from_file_location(
        "github_release", ROOT / "tools" / "github_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_upload_names_the_manual_and_clobbers(tmp_path, monkeypatch):
    mod = _module()
    pdf = tmp_path / "ripple-manual-en.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    calls: list[tuple[str, ...]] = []

    def fake_sh(*args: str):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(mod, "sh", fake_sh)
    mod.upload_pdf("v0.2.1", pdf)

    assert len(calls) == 1
    argv = calls[0]
    assert argv[:3] == ("gh", "release", "upload")
    assert argv[3] == "v0.2.1"
    assert argv[4].endswith("ripple-manual-en.pdf")
    assert "--clobber" in argv


def test_the_notes_point_at_the_latest_manual_not_at_this_tag():
    """A stranger who finds an old release wants the current manual (#148).

    `releases/download/<tag>/…` would pin him to the version he happens to be
    reading about; `releases/latest/download/…` resolves to the newest release
    that is neither draft nor prerelease, so the URL never needs to change.
    """
    mod = _module()
    assert "/releases/latest/download/" in mod.MANUAL_URL
    assert mod.MANUAL_URL.endswith("/" + mod.MANUAL_ASSET)
    assert "releases/download/v" not in mod.MANUAL_URL
    assert mod.MANUAL_URL in mod.MANUAL_LINE


def test_a_missing_manual_stops_with_the_build_command(tmp_path, monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod, "sh", lambda *a: pytest.fail("must not call gh"))
    with pytest.raises(SystemExit) as exc:
        mod.upload_pdf("v0.2.1", tmp_path / "nope.pdf")
    assert "pdf_manual.py" in str(exc.value)


def test_a_failed_upload_is_not_reported_as_success(tmp_path, monkeypatch):
    mod = _module()
    pdf = tmp_path / "ripple-manual-en.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(mod, "sh", lambda *a: subprocess.CompletedProcess(a, 1, "", "boom"))
    with pytest.raises(SystemExit) as exc:
        mod.upload_pdf("v0.2.1", pdf)
    assert "boom" in str(exc.value)

# --- the notes cite the internal board, the reader is on GitHub (#188) ------

def test_a_mapped_reference_points_at_the_github_number():
    mod = _module()
    text = "**The admin key** (issue #158) is shown once."
    out, dropped = mod.translate_refs(text, {158: 13})
    assert out == "**The admin key** (#13) is shown once."
    assert dropped == []


def test_an_unmapped_reference_is_dropped_not_left_dangling():
    mod = _module()
    text = "**The weight question** (issue #169) explains nothing."
    out, dropped = mod.translate_refs(text, {158: 13})
    assert out == "**The weight question** explains nothing."
    assert dropped == [169]


def test_a_list_of_issues_is_translated_as_a_whole():
    mod = _module()
    out, _ = mod.translate_refs("Both (issues #157, #158) are fixed.",
                                {157: 16, 158: 13})
    assert out == "Both (#16, #13) are fixed."


def test_a_list_keeps_only_the_numbers_that_have_a_mirror():
    mod = _module()
    out, dropped = mod.translate_refs("Both (issues #157, #169) are fixed.", {157: 16})
    assert out == "Both (#16) are fixed."
    assert dropped == [169]


def test_prose_that_mentions_an_issue_keeps_its_shape():
    mod = _module()
    out, _ = mod.translate_refs("see issue #157 for why", {157: 16})
    assert out == "see #16 for why"


def test_a_release_candidate_is_a_prerelease():
    """The word that keeps a candidate out of `latest` (25.11.2026).

    `latest` is what the manual link in every final release resolves to, so a
    candidate that took that slot would hand its manual to everyone clicking the
    link in 0.2.3's notes.
    """
    mod = _module()
    for version in ("0.2.4rc1", "1.0.0b2", "2.0.0-alpha", "0.3.0-rc.1", "0.1.0dev"):
        assert mod.is_prerelease(version), version
    for version in ("0.2.3", "0.2.4", "1.0.0", "1.3"):
        assert not mod.is_prerelease(version), version


def test_a_candidate_links_its_own_manual():
    """`latest/download` skips pre-releases — so a candidate links its tag."""
    mod = _module()
    candidate = mod.manual_line("0.2.4rc1")
    assert "/releases/download/v0.2.4rc1/" in candidate, candidate
    assert "release candidate" in candidate, candidate
    final = mod.manual_line("0.2.4")
    assert "/releases/latest/download/" in final, final


def test_a_release_without_a_map_is_said_out_loud():
    """A version whose map is missing keeps the internal numbers — and the tool
    says so, instead of pretending the notes are ready."""
    mod = _module()
    assert mod.load_map("0.0.0-nope") == {}


def test_the_map_of_this_release_is_readable():
    mod = _module()
    mapping = mod.load_map("0.2.3")
    assert mapping.get(158) == 13
    assert mapping.get(187) == 45
    assert len(mapping) == 34

def test_a_bare_number_in_prose_is_translated_too():
    mod = _module()
    out, _ = mod.translate_refs("the behaviour change in #157 was a defect", {157: 16})
    assert out == "the behaviour change in #16 was a defect"


def test_translating_twice_changes_nothing_the_second_time():
    """The tool regenerates the notes from CHANGELOG.md on every run; the
    translation must not drift if it is applied to its own output."""
    mod = _module()
    once, _ = mod.translate_refs("**X** (issue #158), see #157 and (#43).",
                                 {158: 13, 157: 16})
    twice, dropped = mod.translate_refs(once, {158: 13, 157: 16})
    assert once == twice
    assert dropped == []
