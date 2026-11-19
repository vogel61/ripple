"""The `build` field of /healthz: which commit the running process is.

The operator asked "is the new build live?" while the answer said `0.1.0` — the
package version, unchanged since the first commit. These tests pin the two things
that matter: the field reports the commit the tree is on, and it degrades to None
instead of raising when there is no git (a packaged install, a test container).
"""

from __future__ import annotations

import subprocess

import pytest

from ripple import build as buildmod
from ripple.build import build_info


@pytest.fixture(autouse=True)
def _fresh_cache():
    build_info.cache_clear()
    yield
    build_info.cache_clear()


def test_the_build_names_the_commit_the_tree_is_on():
    expected = subprocess.run(["git", "-C", str(buildmod.REPO), "rev-parse",
                               "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    info = build_info()
    assert info["commit"] == expected
    assert isinstance(info["date"], str) and len(info["date"]) == 10
    assert isinstance(info["dirty"], bool)


def test_a_missing_git_is_not_an_error(monkeypatch):
    monkeypatch.setattr(buildmod, "_git", lambda *args: None)
    assert build_info() == {"commit": None, "date": None, "dirty": None}


def test_a_git_that_hangs_or_fails_is_not_an_error(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    # `_git` swallows it; the point is that nothing above it raises
    monkeypatch.setattr(buildmod.subprocess, "run", boom)
    assert build_info()["commit"] is None


async def test_healthz_reports_the_build(ripple_factory, stub_factory):
    from tests.support import StubModel, host_config, make_config

    a = stub_factory(models=[StubModel(id="m1", estimated_size=1_000_000_000)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    body = await harness.healthz()

    assert body["version"]                     # unchanged, still reported
    assert set(body["build"]) == {"commit", "date", "dirty"}
    assert body["build"]["commit"]


async def test_the_dashboard_header_shows_the_build(ripple_factory, stub_factory):
    from tests.support import StubModel, host_config, make_config

    a = stub_factory(models=[StubModel(id="m1", estimated_size=1_000_000_000)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    page = (await harness.client.get("/")).text
    js = (await harness.client.get("/static/dashboard.js")).text

    # Since issue #2 the shell only links the behaviour; the footer line
    # `verbunden · v0.1.0 · <commit> (date) · up …` is built in the asset.
    assert "/static/dashboard.js" in page
    assert "h.build" in js and "b.commit" in js
    assert "b.dirty" in js
