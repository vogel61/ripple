"""The start-up update check (issue #123): newer, equal, older, unreachable, off.

The issue is a hint, so the tests are about the *four* answers the hint can have
and about the one thing it must never do: hold the start-up, or make noise when
it has nothing to say. Each case gets its own test:

* newer   — the header offers the release, in the page's language;
* equal   — nothing, because there is nothing to do;
* older   — nothing, which is handover §5: a GitHub checkout ahead of the last
            public release must never be told to move backwards;
* unreachable — `unknown`, no log line, nothing in the header;
* disabled — no fetch at all, `disabled`, nothing in the header.

`ripple/update.py` is pure apart from one injected `fetch`, so all of this runs
without a network. The two tests that go through the proxy use `TestClient` and
a stub fetcher; the header test runs the real `dashboard.js` under node, because
"the sentence is in the configured language" is a property of the catalog, not
of a Python string.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import shutil
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from ripple import __version__, update
from ripple.config import Config, ConfigError, UpdateConfig, load_config
from ripple.proxy import Ripple

from tests.support import closed_port, host_config, make_config

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "ripple" / "static"
I18N = STATIC / "i18n"
NODE = shutil.which("node")

RELEASE_URL = "https://github.com/vogel61/ripple/releases/tag/v0.2.0"


def newer_tag() -> str:
    """A release tag that is newer than the tree, whatever the tree says.

    Hardcoding one is how this suite went red on the 0.2.0 bump: `v0.2.0` was
    "newer" while `__version__` said 0.1.1 and became "equal" the moment the
    release was cut — the test was asserting yesterday's version, not the
    property. The two tests that go through the real `Ripple` app derive it from
    `__version__`; the pure-function tests below pass their current version
    explicitly and stay fixed on purpose.
    """
    major, minor = (int(part) for part in __version__.split(".")[:2])
    return f"v{major}.{minor + 1}.0"


def payload(tag: str, *, draft: bool = False, html_url: str | None = RELEASE_URL) -> dict:
    """The shape the GitHub releases API answers with, cut to what we read.

    Observed on the real endpoint (20.11.2026): `tag_name`, `html_url`, `name`,
    `body`, `draft`, `prerelease`, `published_at`, `assets`, `assets_url`,
    `author`, `created_at`, `id`, `node_id`, `tarball_url`, `target_commitish`,
    `updated_at`, `upload_url`, `url`, `zipball_url`. Only `tag_name`,
    `html_url` and `draft` matter here; the rest is kept out of the stub so a
    reader sees what the code actually depends on.
    """
    return {"tag_name": tag, "html_url": html_url, "draft": draft,
            "prerelease": False, "name": f"RIPPLE {tag}"}


def fetcher(result):
    """A `fetch(url, timeout)` stub: returns `result`, or raises it."""
    calls: list[tuple[str, float]] = []

    async def _fetch(url: str, timeout: float):
        calls.append((url, timeout))
        if isinstance(result, Exception):
            raise result
        return result

    _fetch.calls = calls          # type: ignore[attr-defined]
    return _fetch


# -- the five answers -------------------------------------------------------
async def test_a_newer_release_is_newer():
    """`v0.2.0` against 0.1.1: the state names the version and the page."""
    state = await update.check("https://example.invalid/latest", "0.1.1",
                               fetch=fetcher(payload("v0.2.0")))
    assert state.state == "newer"
    assert state.latest == "0.2.0"        # the `v` is the header's job, not the tag's
    assert state.url == RELEASE_URL
    assert isinstance(state.checked_at, float)


async def test_an_equal_release_is_current():
    """The running version *is* the newest release: nothing to offer."""
    state = await update.check("https://example.invalid/latest", "0.1.1",
                               fetch=fetcher(payload("v0.1.1")))
    assert state.state == "current"
    assert state.latest == "0.1.1"


async def test_an_older_release_is_current_and_never_points_backwards():
    """Handover §5: a local GitHub build newer than the last release.

    The newest published release is *older* than what runs here. An offer would
    send the operator backwards, so the state is `current` — the same word as
    equal, because the page's reaction is the same: nothing.
    """
    state = await update.check("https://example.invalid/latest", "0.3.0",
                               fetch=fetcher(payload("v0.2.0")))
    assert state.state == "current"
    assert state.latest == "0.2.0"


async def test_an_unreachable_api_is_unknown_and_logs_nothing(caplog):
    """No network is not an error: `unknown`, and not one line in the log."""
    with caplog.at_level(logging.WARNING):
        state = await update.check(
            "https://example.invalid/latest", "0.1.1",
            fetch=fetcher(httpx.ConnectError("no route to host")))
    assert state.state == "unknown"
    assert state.latest is None
    assert caplog.records == []


async def test_a_disabled_check_fetches_nothing():
    """`update.check: false` is the off switch — not even a DNS lookup."""
    stub = fetcher(payload("v0.2.0"))
    state = await update.check("https://example.invalid/latest", "0.1.1",
                               enabled=False, fetch=stub)
    assert state.state == "disabled"
    assert stub.calls == []       # type: ignore[attr-defined]
    assert state.checked_at is None


async def test_a_timeout_is_unknown_too():
    """The one timeout the check has is a hint's timeout, not an outage."""
    state = await update.check("https://example.invalid/latest", "0.1.1",
                               fetch=fetcher(httpx.ReadTimeout("slow")))
    assert state.state == "unknown"


async def test_a_404_no_release_yet_is_unknown():
    """A repository without a release answers 404; that is "no answer", not 0.0.0."""
    request = httpx.Request("GET", "https://example.invalid/latest")
    response = httpx.Response(404, request=request)
    state = await update.check(
        "https://example.invalid/latest", "0.1.1",
        fetch=fetcher(httpx.HTTPStatusError("404", request=request, response=response)))
    assert state.state == "unknown"


async def test_a_payload_without_a_tag_is_unknown():
    """A 200 with no `tag_name` (a draft, a changed API) is not a version."""
    state = await update.check("https://example.invalid/latest", "0.1.1",
                               fetch=fetcher({"message": "Not Found"}))
    assert state.state == "unknown"


# -- the comparison ---------------------------------------------------------
def test_the_comparison_is_semantic_not_a_string_compare():
    """`0.10.0 > 0.9.0` — the case a string compare gets backwards."""
    assert update.is_newer("0.10.0", "0.9.0")
    assert not update.is_newer("0.9.0", "0.10.0")
    assert update.parse_version("0.10.0") > update.parse_version("0.9.0")
    # The same number, written differently: equal, not newer.
    assert not update.is_newer("1.2", "1.2.0")
    assert not update.is_newer("v1.2.0", "1.2")
    assert update.parse_version("v0.1.1") == (0, 1, 1)


@pytest.mark.parametrize("tag", ["v1.2.0-rc1", "1.2.0+build.7", "v1.2.0-rc1+abc"])
def test_a_pre_release_or_build_suffix_is_ignored_for_the_order(tag):
    """`1.2.0-rc1` and `1.2.0+sha` both read as 1.2.0.

    A pre-release of the same number is therefore *not* newer than the release:
    the offer names a release, and `ripple update` would install one. Build
    metadata never orders anything.
    """
    assert update.parse_version(tag) == (1, 2)
    assert not update.is_newer(tag, "1.2.0")
    assert update.is_newer(tag, "1.1.9")           # it is still a 1.2


@pytest.mark.parametrize("tag", ["nightly", "", "  ", "v", "main", "release-candidate"])
def test_a_tag_nobody_can_parse_reads_as_zero(tag):
    """Junk must not crash a start-up and must never look newer than a version."""
    parsed = update.parse_version(tag)              # must not raise
    assert isinstance(parsed, tuple)
    assert parsed == (0,)
    assert not update.is_newer(tag, "0.1.1")


# -- the config -------------------------------------------------------------
def _write(tmp_path: pathlib.Path, extra: str = "") -> pathlib.Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "listen_host: 127.0.0.1\n"
        f"listen_port: {closed_port()}\n"
        "log_level: warning\n"
        "hosts:\n"
        "  - name: alpha\n"
        "    addresses: [http://127.0.0.1:8000]\n"
        "    weight: 1\n"
        "discovery: {interval_seconds: 3600, timeout_seconds: 2}\n"
        f"stats: {{file: {tmp_path / 'host_stats.json'}, persist: false}}\n"
        f"fleet: {{file: {tmp_path / 'runtime.yaml'}}}\n"
        f"placement: {{maintenance_file: {tmp_path / 'maint.yaml'}, "
        f"model_policy_file: {tmp_path / 'models.yaml'}, "
        f"loading: {{calibration_file: {tmp_path / 'cal.yaml'}}}}}\n"
        + extra, encoding="utf-8")
    return path


def test_the_check_is_on_by_default(tmp_path):
    """An existing config file keeps loading and gets the hint: no new key needed."""
    cfg = load_config(_write(tmp_path))
    assert cfg.update.check is True
    assert cfg.update.url == UpdateConfig().url
    assert cfg.update.timeout_seconds == pytest.approx(3.0)
    # And `/config` says so — the setting in force, next to the result in /healthz.
    assert cfg.summary()["update"]["check"] is True


def test_the_off_switch_and_a_custom_source_load(tmp_path):
    """`check: false` and a fork's own releases URL are both plain config."""
    cfg = load_config(_write(tmp_path, "update:\n  check: false\n"
                                      "  url: https://example.invalid/latest\n"
                                      "  timeout_seconds: 1.5\n"))
    assert cfg.update.check is False
    assert cfg.update.url == "https://example.invalid/latest"
    assert cfg.update.timeout_seconds == pytest.approx(1.5)


def test_a_zero_timeout_is_rejected(tmp_path):
    """A timeout of 0 would fail before the socket opened — a typo, not a wish."""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "update:\n  timeout_seconds: 0\n"))


# -- through the proxy ------------------------------------------------------
def _config(*, check: bool = True) -> Config:
    cfg = make_config([host_config("alpha",
                                   f"http://127.0.0.1:{closed_port()}")])
    # A URL that is not the default, so `make_config`'s "no network in tests"
    # default does not switch the check off behind this test's back.
    cfg.update = UpdateConfig(check=check, url="https://example.invalid/latest",
                              timeout_seconds=0.5)
    return cfg


def _wait_for_state(client: TestClient, want: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    health = client.get("/healthz").json()
    while time.monotonic() < deadline:
        health = client.get("/healthz").json()
        if health["update"]["state"] == want:
            return health
        time.sleep(0.05)
    return health


def test_healthz_carries_the_newer_release(monkeypatch):
    """One source for the page: `/healthz` → `update`, next to `version`."""
    async def fake_fetch(url: str, timeout: float):
        return payload(newer_tag())

    monkeypatch.setattr(update, "fetch_latest", fake_fetch)
    with TestClient(Ripple(_config()).app) as client:
        health = _wait_for_state(client, "newer")
    assert health["update"] == {"state": "newer", "latest": newer_tag()[1:],
                                "url": RELEASE_URL,
                                "checked_at": health["update"]["checked_at"]}
    assert health["version"] == __version__


def test_a_disabled_check_reads_disabled_and_never_fetches(monkeypatch):
    """The switch is visible in the payload and the fetch never happens."""
    async def boom(url: str, timeout: float):     # pragma: no cover - must not run
        raise AssertionError("the check ran although update.check is false")

    monkeypatch.setattr(update, "fetch_latest", boom)
    with TestClient(Ripple(_config(check=False)).app) as client:
        health = client.get("/healthz").json()
        assert health["update"]["state"] == "disabled"
        assert health["update"]["latest"] is None
        time.sleep(0.2)
        assert client.get("/healthz").json()["update"]["state"] == "disabled"


def test_the_check_does_not_delay_startup(monkeypatch):
    """The #117 lesson, applied to the check: the page answers first.

    The stub takes 3 s — longer than any start-up may. The app must be up, and
    `/healthz` must answer `unknown`, long before the answer arrives; then the
    state flips on its own, without a second start.
    """
    async def slow_fetch(url: str, timeout: float):
        await asyncio.sleep(3.0)
        return payload(newer_tag())

    monkeypatch.setattr(update, "fetch_latest", slow_fetch)
    app = Ripple(_config()).app
    started = time.monotonic()
    with TestClient(app) as client:
        startup = time.monotonic() - started
        assert startup < 1.5, f"startup waited {startup:.2f} s for the update check"
        assert client.get("/healthz").json()["update"]["state"] == "unknown"
        assert client.get("/").status_code == 200
        health = _wait_for_state(client, "newer", timeout=10.0)
    assert health["update"]["state"] == "newer"


def test_an_unreachable_api_leaves_the_header_empty_and_the_log_quiet(monkeypatch, caplog):
    """No network: `unknown` in the payload, no warning, no exception."""
    async def broken(url: str, timeout: float):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(update, "fetch_latest", broken)
    with caplog.at_level(logging.WARNING):
        with TestClient(Ripple(_config()).app) as client:
            time.sleep(0.2)
            health = client.get("/healthz").json()
    assert health["update"]["state"] == "unknown"
    assert health["update"]["latest"] is None
    # Not "no log line at all" — the fleet has its own opinions about an
    # unreachable host. The *check* is silent: nothing about a release.
    noisy = [r for r in caplog.records
             if "release" in r.getMessage().lower() or "ripple update" in r.getMessage()]
    assert noisy == []


# -- the header, in the page's language -------------------------------------
HARNESS = r"""
/* Runs the real `dashboard.js` under a DOM stub and asks `renderUpdate()` for
 * the four states in two languages. Called by tests/test_update.py.
 *
 * Usage: node <this file> <dashboard.js> <i18n-dir> <json>
 *   json: {"states": [...], "langs": ["de", "en"], "payload": {...}}
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsPath = process.argv[2];
const i18nDir = process.argv[3];
const opts = JSON.parse(process.argv[4] || '{}');

const elements = new Map();
function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [],
              textContent: '', innerHTML: ''};
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {};
  el.removeChild = () => {};
  el.replaceChild = () => {};
  el.setAttribute = (k, v) => { el.attributes[k] = String(v); };
  el.getAttribute = k => (k in el.attributes ? el.attributes[k] : null);
  el.removeAttribute = () => {};
  el.addEventListener = () => {};
  el.classList = {add() {}, remove() {}};
  return el;
}

const document = {
  documentElement: makeEl('html'),
  body: makeEl('body'),
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};

const store = new Map();
const localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};

const warnings = [];
const consoleStub = {warn: (...a) => warnings.push(a.join(' ')),
                     log: () => {}, error: (...a) => warnings.push(a.join(' '))};

function fetchStub(url) {
  if (!String(url).startsWith('/static/i18n/')) {
    return Promise.reject(new Error('unexpected fetch: ' + url));
  }
  const file = path.join(i18nDir, String(url).replace(/^\/static\/i18n\//, ''));
  const body = JSON.parse(fs.readFileSync(file, 'utf8'));
  return Promise.resolve({ok: true, status: 200, json: async () => body});
}

class FrozenDate extends Date {
  constructor(...args) { if (args.length) { super(...args); } else { super(1763000000000); } }
  static now() { return 1763000000000; }
}

const sandbox = {
  Date: FrozenDate, document, localStorage,
  navigator: {languages: ['en-GB'], language: 'en-GB'},
  window: {confirm: () => false, addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {},
  console: consoleStub,
};
sandbox.globalThis = sandbox;

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;

  const out = {};
  for (const code of opts.langs) {
    api.applyLang(code);
    out[code] = {};
    for (const state of opts.states) {
      out[code][state] = api.renderUpdate({update: Object.assign({state: state},
                                                                opts.payload)});
    }
    /* A payload without an `update` key at all (an older proxy) is the same as
       `unknown`: no element, no exception. */
    out[code].missing = api.renderUpdate({});
  }
  out.warnings = warnings.slice();
  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def header(tmp_path_factory) -> dict:
    if NODE is None:
        pytest.skip("node is not installed — the header text needs the real catalog")
    script = tmp_path_factory.mktemp("update-header") / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    opts = json.dumps({"langs": ["de", "en"],
                       "states": ["newer", "current", "unknown", "disabled"],
                       "payload": {"latest": "0.2.0", "url": RELEASE_URL}})
    proc = subprocess.run([NODE, str(script), str(STATIC / "dashboard.js"),
                           str(I18N), opts],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_header_offers_the_release_in_german(header):
    """The sentence is the catalog's, not a string typed into the script."""
    html = header["de"]["newer"]
    assert "Update v0.2.0 ist verfügbar" in html
    assert "<code>ripple update</code>" in html
    assert f'href="{RELEASE_URL}"' in html
    assert 'class="update-link"' in html


def test_the_header_offers_the_release_in_english(header):
    html = header["en"]["newer"]
    assert "Update v0.2.0 is available — run" in html
    assert "<code>ripple update</code>" in html
    assert f'href="{RELEASE_URL}"' in html
    assert html != header["de"]["newer"]


def test_every_other_state_draws_nothing(header):
    """`current`, `unknown`, `disabled` and a missing key: the header stays clean."""
    for code in ("de", "en"):
        for state in ("current", "unknown", "disabled", "missing"):
            assert header[code][state] == "", (code, state, header[code][state])
    assert header["warnings"] == []


def test_the_tooltip_names_the_command(header):
    """The hint says what `ripple update` does; the tooltip is the one layer."""
    for code in ("de", "en"):
        html = header[code]["newer"]
        assert 'data-tip="' in html
        assert "ripple update" in html
