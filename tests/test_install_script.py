"""`install.sh` — the one prerequisite nobody installs (issue #178).

macOS ships no Python since 12.3, so the documented `python3 -m venv` fails with
`command not found` on a fresh machine. The script's job is to say what is
missing and print a line that can be copied — these tests run the real script,
with a PATH that has no interpreter in it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.sh"


def _run(path: str) -> subprocess.CompletedProcess:
    env = {"PATH": path, "HOME": os.environ.get("HOME", "/tmp")}
    return subprocess.run(["/bin/bash", str(SCRIPT), "--check"], cwd=str(ROOT),
                          env=env, capture_output=True, text=True, timeout=120)


def _path_without_python(tmp_path: Path) -> str:
    """The tools the script itself needs, and deliberately no python3."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("dirname", "cat", "uname", "sed"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    return str(bin_dir)


def test_without_python_it_prints_the_two_lines_to_copy(tmp_path):
    result = _run(_path_without_python(tmp_path))

    assert result.returncode == 1, (result.stdout, result.stderr)
    message = result.stdout + result.stderr
    # The command lines, not advice: both ways to get a python3 on macOS.
    assert "xcode-select --install" in message, message
    assert "brew install python@3.13" in message, message
    assert "no python3 found" in message, message
    # and the honest statement that this is the missing piece
    assert "3.9" in message, message


def test_with_python_it_reports_and_writes_nothing(tmp_path):
    before = set(p.name for p in ROOT.iterdir())
    result = _run(os.environ.get("PATH", "/usr/bin:/bin"))

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "check only" in result.stdout, result.stdout
    assert "python:" in result.stdout, result.stdout
    assert set(p.name for p in ROOT.iterdir()) == before, "the check wrote into the repo"


def test_the_double_click_wrapper_only_calls_the_script():
    wrapper = (ROOT / "Install.command").read_text(encoding="utf-8")

    assert wrapper.startswith("#!/usr/bin/env bash"), wrapper
    assert "install.sh" in wrapper, wrapper
    assert (ROOT / "install.sh").stat().st_mode & 0o111, "install.sh is not executable"
    assert (ROOT / "Install.command").stat().st_mode & 0o111, "Install.command is not executable"
