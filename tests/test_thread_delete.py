"""Deleting a thread from the page (issue #35).

A thread holds a slot on its host for `affinity.ttl_seconds` (1800 s) after its
last request, and until now nothing on the page could end that early — the
operator could only watch the countdown. The issue adds one control per thread
item and the route behind it:

- `DELETE /admin/threads/{key}` behind the admin key, `{key}` being the `label`
  the page shows (`/healthz → threads.registry[].key`), resolved against the
  registry and never re-derived from a hash;
- unknown key → 404, a request in flight → 409 naming host and count, otherwise
  the thread is dropped, `stats.threads_deleted` counts it and one log line says
  which thread, which host and who;
- one button per thread item: off without the admin key (with the reason), off
  while a request is in flight (with `threads.delete_active`), one confirmation
  before the call — and a cancelled confirmation sends nothing at all.

The last group runs the real `dashboard.js` under a DOM stub with a fetch
recorder: a click is measured as the requests it caused, not as a promise.
"""

from __future__ import annotations

import json
import re
import logging
import pathlib
import re
import shutil
import subprocess

import pytest

from ripple.registry import LABEL_CHARS, ThreadRegistry
from ripple.config import AffinityConfig
from ripple.stats import Stats

from tests.support import (StubModel, bearer, chat_body, host_config, keyed_config,
                           stub_model_loaded)

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "ripple" / "static"
I18N = STATIC / "i18n"
NODE = shutil.which("node")

GB = 1_000_000_000
ADMIN = bearer("test-admin-key")

# A session id longer than the visible label, so the page and the route cannot
# agree by accident: `label = key[:LABEL_CHARS]`.
LONG_SID = "sess-" + "0123456789abcdef" * 2
LONG_KEY = "sid:" + LONG_SID


@pytest.fixture(scope="module")
def js() -> str:
    """The page's script, read once — the click's route is a string in it."""
    return (STATIC / "dashboard.js").read_text(encoding="utf-8")


def _en() -> dict:
    return json.loads((I18N / "en.json").read_text(encoding="utf-8"))


def _de() -> dict:
    return json.loads((I18N / "de.json").read_text(encoding="utf-8"))


# -- the route --------------------------------------------------------------
async def _live_thread(harness, *, prompt: str = "hello there", sid: str = LONG_SID):
    """One real request that opens a thread, and the row the page would show."""
    await harness.ok(chat_body("m1", prompt, turns=2),
                     headers={**bearer(_key(harness)), "X-Session-Id": sid})
    rows = (await harness.healthz())["threads"]["registry"]
    return rows[0]


def _key(harness) -> str:
    """A client key that exists for this instance (created on first use)."""
    store = harness.ripple.keys
    for known in store.keys:
        if known.name == "hermes":
            return known.key
    return store.create("hermes", "interactive").key


async def test_deleting_a_thread_frees_its_slot_and_is_counted(
        stub_factory, ripple_factory, tmp_path, caplog):
    """The acceptance: the slot is gone from `/healthz` after one DELETE, the
    counter moved and one line in the log says which thread and which host."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml")))
    row = await _live_thread(harness)
    assert harness.ripple.registry.slots_on_host("A") == 1
    assert harness.ripple.stats.threads_deleted == 0

    caplog.set_level(logging.WARNING, logger="ripple")
    resp = await harness.client.delete("/admin/threads/" + row["key"], headers=ADMIN)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": row["key"], "removed": True, "host": "A",
                           "slot_freed": True}
    health = await harness.healthz()
    assert health["threads"]["registry"] == []
    assert health["threads"]["active"] == 0
    assert harness.ripple.registry.slots_on_host("A") == 0
    assert harness.ripple.stats.threads_deleted == 1
    assert (await harness.stats())["threads_deleted"] == 1

    lines = [r.getMessage() for r in caplog.records if r.name == "ripple"
             and r.getMessage().startswith("thread_deleted ")]
    assert len(lines) == 1, lines
    assert f"key={row['key']}" in lines[0]
    assert "host=A" in lines[0]
    assert "turns=" in lines[0] and "idle=" in lines[0] and "by=127.0.0.1" in lines[0]
    # the count is a whole number of seconds, not a float
    assert re.search(r"idle=\d+s", lines[0]), lines[0]


async def test_the_key_in_the_path_is_the_label_the_page_shows(
        stub_factory, ripple_factory, tmp_path):
    """The page sends the label; the registry key is longer. The route
    resolves the label against the registry instead of re-deriving the key."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml")))
    row = await _live_thread(harness)
    assert LABEL_CHARS == 24, "issue #68: the label must hold a whole HHMMSS stamp"
    assert row["key"] == LONG_KEY[:LABEL_CHARS] != LONG_KEY
    assert LONG_KEY in harness.ripple.registry.threads

    resp = await harness.client.delete("/admin/threads/" + row["key"], headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == LONG_KEY[:LABEL_CHARS]
    assert harness.ripple.registry.threads == {}

    # the whole key is accepted too, for an operator with curl and /stats
    await _live_thread(harness, prompt="second conversation")
    resp = await harness.client.delete("/admin/threads/" + LONG_KEY, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert harness.ripple.registry.threads == {}


async def test_an_unknown_key_is_404_with_an_error_object(
        stub_factory, ripple_factory, tmp_path):
    """No traceback, no silent 200: an id that is not in the registry is a 404
    with the shape every other refusal of the service uses."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml")))
    row = await _live_thread(harness)

    resp = await harness.client.delete("/admin/threads/deadbeefdeadbeef", headers=ADMIN)
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert set(body) == {"error"} and isinstance(body["error"]["message"], str)
    assert "deadbeefdeadbeef" in body["error"]["message"]
    # the real thread is untouched
    assert row["key"] in [r["key"] for r in
                          (await harness.healthz())["threads"]["registry"]]

    # and the second delete of the same thread is the same 404
    assert (await harness.client.delete("/admin/threads/" + row["key"],
                                        headers=ADMIN)).status_code == 200
    again = await harness.client.delete("/admin/threads/" + row["key"], headers=ADMIN)
    assert again.status_code == 404


async def test_a_thread_with_a_request_in_flight_is_refused_with_409(
        stub_factory, ripple_factory, tmp_path):
    """A running request owns its slot. The answer names the host and the count
    and the thread stays — a silent drop would cut the slot from under it."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml")))
    registry: ThreadRegistry = harness.ripple.registry
    thread = registry.register(key="sid:busy-session", model="m1", host="A",
                               text="hello", holds_slot=True, source="header")
    registry.enter(thread)
    registry.enter(thread)          # two requests on the same conversation

    resp = await harness.client.delete("/admin/threads/sid:busy-session",
                                       headers=ADMIN)
    assert resp.status_code == 409, resp.text
    message = resp.json()["error"]["message"]
    assert "A" in message                      # the host
    assert "2" in message                      # the count
    assert "sid:busy-session" in message
    assert registry.threads.get("sid:busy-session") is thread
    assert harness.ripple.stats.threads_deleted == 0

    # once the requests are done the same call goes through
    registry.leave(thread)
    registry.leave(thread)
    assert (await harness.client.delete("/admin/threads/sid:busy-session",
                                        headers=ADMIN)).status_code == 200
    assert registry.threads == {}


async def test_the_route_is_behind_the_admin_key(stub_factory, ripple_factory, tmp_path):
    """No key, no delete — the button is off for the same reason the route is."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml")))
    row = await _live_thread(harness)

    resp = await harness.client.delete("/admin/threads/" + row["key"])
    assert resp.status_code == 401, resp.text
    assert harness.ripple.registry.threads, "the thread was dropped without a key"
    assert harness.ripple.stats.threads_deleted == 0


async def test_healthz_carries_the_active_count_of_each_thread(
        stub_factory, ripple_factory, tmp_path):
    """`threads.registry[].active` is what lets the page switch the control off
    before the click; `/stats → threads` keeps the shape it had."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml")))
    row = await _live_thread(harness)
    assert row["active"] == 0

    thread = harness.ripple.registry.threads[LONG_KEY]
    harness.ripple.registry.enter(thread)
    row = (await harness.healthz())["threads"]["registry"][0]
    assert row["active"] == 1
    assert "active" not in (await harness.stats())["threads"][0]


def test_the_counter_is_in_the_stats_payload():
    """One field, exported like its neighbours: `threads_deleted`."""
    stats = Stats()
    assert stats.threads_deleted == 0
    stats.threads_deleted += 1
    assert stats.as_dict()["threads_deleted"] == 1


# -- the catalogs -----------------------------------------------------------
def test_the_four_ids_say_what_the_issue_asks_for():
    """The wording of the four new ids, English and German, exactly."""
    en, de = _en(), _de()
    assert en["threads.delete"] == "delete"
    assert de["threads.delete"] == "löschen"
    assert en["threads.delete_confirm"] == (
        "Delete this thread? Its prefix cache is gone for good.")
    assert de["threads.delete_confirm"] == (
        "Diesen Thread löschen? Sein Prefix-Cache ist endgültig weg.")
    assert en["threads.delete_active"] == "request in flight"
    assert de["threads.delete_active"] == "Anfrage läuft"
    assert en["tip.th_delete"] == (
        "Delete — drops the thread and frees its slot on this node. The prefix "
        "cache cannot be recreated; the next request pays the prefill.")
    assert de["tip.th_delete"] == (
        "Löschen — verwirft den Thread und gibt seinen Slot auf diesem Knoten "
        "frei. Der Prefix-Cache ist nicht wiederherstellbar; die nächste Anfrage "
        "zahlt den Prefill.")


def test_every_catalog_carries_all_four_ids():
    """Every catalog on disk, the same four ids, no English left in the drafts.

    The count is not written down here: it is the number of languages the page
    offers (`LANGS`), so a new catalog is covered the moment it lands instead of
    breaking a test that knew the number eight.
    """
    files = sorted(I18N.glob("*.json"))
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    offered = re.findall(r"\{code: '([a-z]{2})'", js)
    assert sorted(p.stem for p in files) == sorted(offered), \
        ([p.stem for p in files], offered)
    en = _en()
    ids = ("threads.delete", "threads.delete_active", "threads.delete_confirm",
           "tip.th_delete")
    for path in files:
        cat = json.loads(path.read_text(encoding="utf-8"))
        for i in ids:
            assert i in cat, (path.name, i)
            assert cat[i].strip(), (path.name, i)
        if path.stem != "en":
            # The drafts are translations, not copies: `delete` alone is short
            # enough to coincide, the other three are sentences.
            for i in ids[1:]:
                assert cat[i] != en[i], (path.name, i)


# -- the click, executed ----------------------------------------------------
THREAD_HARNESS = r"""
'use strict';
/* Runs `dashboard.js` under a DOM stub with a fetch recorder and a settable
 * `window.confirm`. What is measured: the requests a click causes (none for a
 * cancelled confirmation), the path it sends, the re-render after success and
 * the line the block shows after a refusal.
 *
 * Usage: node <this file> <dashboard.js> <fixture.json>
 */
const fs = require('fs'), vm = require('vm');
const jsPath = process.argv[2], fixturePath = process.argv[3];
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

const requests = [];
let confirmAnswer = false;
let confirms = 0;
let deleteMode = 'ok';
const KEY = fixture.thread_key;

function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [], html: ''};
  Object.defineProperty(el, 'innerHTML', {
    get() { return el.html; }, set(v) { el.html = String(v); },
  });
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {}; el.removeChild = () => {}; el.replaceChild = () => {};
  el.setAttribute = () => {}; el.getAttribute = () => null; el.removeAttribute = () => {};
  el.addEventListener = () => {};
  return el;
}
const elements = new Map();
const document = {
  body: makeEl('body'),
  documentElement: makeEl('html'),
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

let adminKey = 'test-admin-key';
const errorBody = msg => ({error: {message: msg}});
async function fetchStub(url, init) {
  const method = (init && init.method) || 'GET';
  requests.push(method + ' ' + url);
  const path = String(url).split('?')[0];
  if (path.indexOf('/admin/threads/') === 0) {
    if (deleteMode === 'conflict') {
      const body = errorBody('thread ' + KEY + ' has 1 request(s) in flight on M5-alpha');
      return {ok: false, status: 409, text: async () => JSON.stringify(body)};
    }
    if (deleteMode === 'denied') {
      const body = errorBody('admin key required');
      return {ok: false, status: 401, text: async () => JSON.stringify(body)};
    }
    /* The fake backend forgets the thread, like the real one does: the tick
       after the delete is what makes the item disappear. */
    fixture.healthz.threads.registry = [];
    const body = {key: KEY, removed: true, host: 'M5-alpha', slot_freed: true};
    return {ok: true, status: 200, json: async () => body,
            text: async () => JSON.stringify(body)};
  }
  const body = fixture[path.replace(/^\//, '')];
  if (body === undefined) throw new Error('no fixture for ' + url);
  return {ok: true, status: 200, json: async () => body,
          text: async () => JSON.stringify(body)};
}

const sandbox = {
  Date,
  document,
  location: new Proxy({}, {get() { throw new Error('the page must not navigate'); }}),
  window: {confirm: () => { confirms++; return confirmAnswer; },
           addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: k => (k === 'ripple_admin' ? adminKey : null),
                   setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: fn => { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout: () => {},
  console,
};
sandbox.globalThis = sandbox;

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;

  const btn = {getAttribute: k => (k === 'data-key' ? KEY : null)};
  const threadsHtml = () => (elements.get('threads') || {}).__html || '';
  const out = {boot: requests.filter(r => r.indexOf('/static/i18n/') === -1)};
  requests.length = 0;

  /* A cancelled confirmation is a click that sends nothing at all. */
  confirmAnswer = false;
  const beforeConfirms = confirms;
  await api.threadDelete(btn);
  out.cancel = {confirms: confirms - beforeConfirms, requests: requests.slice()};

  /* Confirmed: one DELETE to the label the page shows, then the tick that
     shows the block without the thread. */
  requests.length = 0;
  confirmAnswer = true;
  await api.threadDelete(btn);
  out.ok = {requests: requests.slice(), html: threadsHtml()};

  /* The route says 409: the block says why and the thread stays on screen. */
  fixture.healthz.threads.registry = [JSON.parse(JSON.stringify(fixture.row))];
  await api.tick();
  requests.length = 0;
  deleteMode = 'conflict';
  await api.threadDelete(btn);
  out.conflict = {requests: requests.slice(), html: threadsHtml()};

  /* A key the server does not accept is the same sentence as no key at all. */
  deleteMode = 'denied';
  requests.length = 0;
  await api.threadDelete(btn);
  out.denied = {requests: requests.slice(), html: threadsHtml()};

  /* Without a key the click is inert — no confirmation, no request. */
  adminKey = null;
  deleteMode = 'ok';
  requests.length = 0;
  const markConfirms = confirms;
  await api.threadDelete(btn);
  out.without_key = {confirms: confirms - markConfirms, requests: requests.slice(),
                     html: threadsHtml()};

  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


def _run_harness(tmp_path) -> dict:
    if NODE is None:
        pytest.skip("node is not installed — the click measurement needs it")
    from tests.test_dashboard import _catalogs, _config, _healthz, _stats

    health = _healthz()
    row = dict(health["threads"]["registry"][0])
    fixture = {"healthz": health, "stats": _stats(), "config": _config(),
               "thread_key": row["key"], "row": row, **_catalogs()}
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    harness = tmp_path / "thread_harness.js"
    harness.write_text(THREAD_HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness), str(STATIC / "dashboard.js"), str(path)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def click(tmp_path_factory) -> dict:
    return _run_harness(tmp_path_factory.mktemp("thread-delete"))


def test_a_cancelled_confirmation_sends_nothing(click):
    """One question before the call — and a `no` is a click without a request."""
    assert click["cancel"]["confirms"] == 1
    assert click["cancel"]["requests"] == []


def test_a_confirmed_click_deletes_the_thread_and_refreshes(click):
    """One DELETE to the label, then the same tick as after every other admin
    action: the block no longer lists the thread, without a page reload."""
    reqs = click["ok"]["requests"]
    assert reqs[0] == "DELETE /admin/threads/4b17eecc4cf0a8a5"
    assert reqs[1:] == ["GET /healthz", "GET /stats", "GET /config"]
    assert "4b17eecc4cf0a8a5" not in click["ok"]["html"]
    assert _en()["threads.none"] in click["ok"]["html"]


def test_a_refusal_is_said_in_the_block(click):
    """A 409 leaves the thread on screen and prints the server's reason; a 401 is
    the same sentence as a missing key. A silent refusal would look like a click
    that did nothing."""
    assert click["conflict"]["requests"][0] == "DELETE /admin/threads/4b17eecc4cf0a8a5"
    assert "1 request(s) in flight on M5-alpha" in click["conflict"]["html"]
    assert "4b17eecc4cf0a8a5" in click["conflict"]["html"]      # still listed
    assert _en()["key.help"] in click["denied"]["html"]


def test_without_the_key_the_click_asks_nothing(click):
    """No key: no confirmation and no request — the button is disabled for the
    same reason the route answers 401."""
    assert click["without_key"]["confirms"] == 0
    assert click["without_key"]["requests"] == []
    assert _en()["key.help"] in click["without_key"]["html"]


def test_the_page_sends_the_label_it_shows(js):
    """The path is built from the item's own `data-key`, escaped, and encoded —
    never re-derived from the thread id."""
    assert "'/admin/threads/' + encodeURIComponent(key)" in js
    assert "adminApi('DELETE', '/admin/threads/'" in js
    assert "t('threads.delete_confirm')" in js
    assert "window.confirm(t('threads.delete_confirm'))" in js
