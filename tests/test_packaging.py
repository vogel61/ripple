"""The distribution ships what the dashboard needs (issue #60).

`ripple/static` is not an importable package: the proxy serves it straight from
the package directory, so the catalogs, the stylesheet and the wordmark font
reach a user only if the *wheel* carries them as package data. A pyproject.toml
that looks right while the wheel ships an empty `static/` fails in the worst
way — the proxy starts, the page renders, and every label is blank.

So this file builds the real wheel and looks inside it. The build is offline
(`--no-deps --no-build-isolation`, no index) and happens in `tmp_path`; the test
never installs anything into the running interpreter.

The build needs a PEP 517 backend with `[project]` support (setuptools >= 77,
the floor pyproject.toml declares). The project venv is deliberately free of
build tooling, so the backend is looked up: this interpreter first, then any
`python3*` on `PATH`. Only when no interpreter has one does the test skip — a
build that fails *with* a usable backend is a failure, not a skip.
"""

from __future__ import annotations

import glob
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

import pytest

import ripple

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "ripple" / "static"

#: The languages the selector offers — one catalog each.
LANGUAGES = ("cs", "de", "el", "en", "es", "fr", "hr", "hu", "it", "pl", "pt",
             "ru", "sk", "sl", "tr", "zh")

#: `[build-system] requires` in pyproject.toml; a PEP 639 `license` string and
#: `static/**/*` package-data patterns need at least this setuptools.
MIN_SETUPTOOLS = (77, 0)

#: Everything the runtime reads from the package directory, relative to
#: `ripple/static`. Derived from the tree, so a new catalog or asset is covered
#: without touching this file.
STATIC_FILES = sorted(
    "ripple/static/" + path.relative_to(STATIC).as_posix()
    for path in STATIC.rglob("*")
    if path.is_file()
)


def _setuptools_version(python: str) -> str:
    """The setuptools version `python` can import, or "" when it has none."""
    try:
        proc = subprocess.run(
            [python, "-c", "import setuptools; print(setuptools.__version__)"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _is_usable(version: str) -> bool:
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= MIN_SETUPTOOLS


def _candidate_interpreters() -> list:
    """This interpreter first, then every `python3*` reachable through `PATH`.

    Not `shutil.which("python3")`: that is one interpreter, and it is exactly the
    one without build tooling. The scan is bounded to the executables a `python3*`
    name matches and de-duplicated by resolved path.
    """
    candidates = [sys.executable]
    for name in ("python3", "python3.13", "python3.12", "python3.11", "python3.10",
                 "python3.9"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            candidates.extend(sorted(glob.glob(os.path.join(entry, "python3*"))))

    seen, unique = set(), []
    for candidate in candidates:
        if "-config" in os.path.basename(candidate):     # python3.11-config, not an interpreter
            continue
        try:
            real = os.path.realpath(candidate)
        except OSError:
            continue
        if real not in seen and os.access(candidate, os.X_OK):
            seen.add(real)
            unique.append(candidate)
    return unique


def _build_wheel(python: str, workdir: pathlib.Path) -> tuple:
    """Build a wheel from a copy of the project; return (wheel, completed proc).

    The copy keeps the build out of the worktree — `pip wheel .` writes `build/`
    and `*.egg-info/` next to the sources, which would dirty the tree and race
    other test runs.
    """
    src = workdir / "src"
    dist = workdir / "dist"
    src.mkdir(parents=True)
    dist.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, src / name)
    shutil.copytree(ROOT / "ripple", src / "ripple",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    env = dict(os.environ,
               PIP_NO_INDEX="1",
               PIP_DISABLE_PIP_VERSION_CHECK="1",
               PIP_CONFIG_FILE=os.devnull)
    proc = subprocess.run(
        [python, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation",
         "--no-cache-dir", "-w", str(dist)],
        cwd=str(src), env=env, capture_output=True, text=True, timeout=600,
    )
    wheels = sorted(dist.glob("ripple_proxy-*.whl"))
    return (wheels[0] if len(wheels) == 1 else None), proc


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> pathlib.Path:
    """The wheel, built once for the whole module."""
    workdir = tmp_path_factory.mktemp("wheel")
    skipped = []
    for python in _candidate_interpreters():
        version = _setuptools_version(python)
        if not _is_usable(version):
            skipped.append(f"{python}: setuptools {version or 'missing'}")
            continue
        built, proc = _build_wheel(python, workdir)
        if built is None:
            pytest.fail(
                f"`{python} -m pip wheel .` did not produce exactly one wheel "
                f"(exit {proc.returncode})\n--- stdout ---\n{proc.stdout}"
                f"\n--- stderr ---\n{proc.stderr}"
            )
        return built
    floor = "%d.%d" % MIN_SETUPTOOLS
    pytest.skip(f"no offline PEP 517 backend with setuptools >= {floor} "
                f"(tried {len(skipped)} interpreters: {'; '.join(skipped)})")


@pytest.fixture(scope="module")
def wheel_names(wheel: pathlib.Path) -> set:
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


@pytest.fixture(scope="module")
def metadata(wheel: pathlib.Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        assert len(names) == 1, names
        return archive.read(names[0]).decode("utf-8")


def test_wheel_ships_every_dashboard_asset(wheel_names: set) -> None:
    """The 16 catalogs, the CSS, the JS and the font are in the wheel."""
    catalogs = sorted(path.name for path in (STATIC / "i18n").glob("*.json"))
    assert len(catalogs) == 16, catalogs
    assert tuple(sorted(path.stem for path in (STATIC / "i18n").glob("*.json"))) \
        == tuple(sorted(LANGUAGES))

    missing = [name for name in STATIC_FILES if name not in wheel_names]
    assert not missing, f"not in the wheel: {missing}"

    for required in ("ripple/static/dashboard.css", "ripple/static/dashboard.js",
                     "ripple/static/fonts/rubik-microbe-wordmark.woff2"):
        assert required in wheel_names, required


def test_wheel_metadata_declares_python_floor_and_dependencies(metadata: str) -> None:
    lines = metadata.splitlines()
    assert "Name: ripple-proxy" in lines
    assert "Requires-Python: >=3.9" in lines

    runtime = [line for line in lines
               if line.startswith("Requires-Dist:") and "extra ==" not in line]
    for dependency in ("fastapi>=0.110", "uvicorn>=0.27", "httpx>=0.27", "PyYAML>=6.0"):
        assert f"Requires-Dist: {dependency}" in runtime, (dependency, runtime)
    assert len(runtime) == 4, runtime
    # Test tooling is an extra: an inference node must not pull pytest in.
    assert not any("pytest" in line for line in runtime)
    assert any('extra == "dev"' in line and "pytest>=8.0" in line for line in lines)
    assert any('extra == "dev"' in line and "pytest-asyncio>=0.23" in line
               for line in lines)


def test_wheel_declares_the_ripple_console_script(wheel: pathlib.Path,
                                                metadata: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        entry = [n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")]
        assert len(entry) == 1, entry
        text = archive.read(entry[0]).decode("utf-8")
    assert "[console_scripts]" in text
    assert "ripple = ripple.__main__:main" in text

    # The version comes from ripple/__init__.py through [tool.setuptools.dynamic];
    # a duplicated string in pyproject.toml would show up here.
    assert f"Version: {ripple.__version__}" in metadata.splitlines()


def test_pyproject_declares_entry_point_and_python_floor() -> None:
    """The source of truth itself — the wheel checks above are the effect."""
    try:
        import tomllib
    except ModuleNotFoundError:                       # Python 3.9 / 3.10
        pytest.skip("tomllib is 3.11+; the wheel metadata tests cover this on 3.9")

    with open(ROOT / "pyproject.toml", "rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["requires-python"] == ">=3.9"
    assert project["scripts"] == {"ripple": "ripple.__main__:main"}
    assert project["name"] == "ripple-proxy"
    assert project["dynamic"] == ["version"]
    assert "version" not in project
