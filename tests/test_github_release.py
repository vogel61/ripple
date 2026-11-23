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
