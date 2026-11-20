/* Runs `ripple/static/dashboard.js` against a DOM stub and prints what it
 * rendered, as JSON. Called by `tests/test_dashboard.py`.
 *
 * Why this exists: the page's render functions are pure string builders, so the
 * honest way to check the markup (thead cells against tbody cells, the memory
 * bar's segments, the tooltip on every number) is to execute the real
 * JavaScript with a fixed set of numbers and measure the output — not to look
 * at a screenshot.
 *
 * The stub records every write to `innerHTML`, which is what makes "no full
 * reload" measurable: a tick with unchanged data must write nothing at all, and
 * `location` is a proxy that throws if the page so much as reads it, so an
 * accidental navigation cannot pass unnoticed.
 *
 * Since issue #34 the stub also builds a tiny real DOM from the template the
 * page parses in `apply()`: the deadline ticker may only rewrite the text of
 * one element, so "the same node before and after a tick" has to be a node
 * identity and not a string comparison. `clockMs` is the clock the page reads
 * (`Date.now`), so a test can let a second — or an hour — pass.
 *
 * Usage: node dashboard_dom_stub.js <dashboard.js> <fixture.json>
 * Fixture: {"healthz": {...}, "stats": {...}, "config": {...}, "loading": {...}}
 */
'use strict';

const fs = require('fs');
const vm = require('vm');

const jsPath = process.argv[2];
const fixturePath = process.argv[3];
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

/* ── the clock ────────────────────────────────────────────────────────────── */
/* The instant the page starts at, in milliseconds. The fixture's `server_time`
 * is the same instant in seconds, so the page's offset starts at 0 and a test
 * can move the browser's clock on its own. */
let clockMs = 1763000000000;

/* ── a very small DOM ─────────────────────────────────────────────────────── */
/* Only what `dashboard.js` touches: elements with attributes and children, text
 * nodes, `textContent`, `outerHTML` for the reconciler's equality check. */
const VOID = new Set(['br', 'img', 'input', 'hr', 'meta', 'link']);
let textWrites = 0;   // every `textContent =` on an element, i.e. the ticker's writes

function textNode(v) {
  return {
    nodeType: 3, nodeValue: String(v), childNodes: [],
    get textContent() { return this.nodeValue; },
    set textContent(x) { textWrites++; this.nodeValue = String(x); },
  };
}

function serialise(n) {
  if (!n) return '';
  if (n.nodeType === 3) return String(n.nodeValue);
  const name = String(n.tagName).toLowerCase();
  const attrs = (n.attributes || []).map(a => ' ' + a.name + '="' + a.value + '"').join('');
  return '<' + name + attrs + '>' + (n.childNodes || []).map(serialise).join('') +
         '</' + name + '>';
}

function makeEl(tag) {
  const el = {
    tag: tag || 'div', tagName: String(tag || 'div').toUpperCase(), nodeType: 1,
    _value: '', writes: 0, childNodes: [], attributes: [],
    /* `value` the way the browser has it: an input carries its own, a select
       reports the selected option (#71's site and role fields are selects). */
    get value() {
      if (this.tagName === 'SELECT') {
        const opts = (this.childNodes || []).filter(
          c => c.nodeType === 1 && c.tagName === 'OPTION');
        const sel = opts.filter(o => o.hasAttribute('selected'))[0];
        return sel ? (sel.getAttribute('value') || '') : '';
      }
      return this._value;
    },
    set value(v) { this._value = String(v); },
    getAttribute(k) {
      const a = this.attributes.find(x => x.name === k);
      return a ? a.value : null;
    },
    setAttribute(k, v) {
      const a = this.attributes.find(x => x.name === k);
      if (a) a.value = String(v); else this.attributes.push({name: String(k), value: String(v)});
    },
    removeAttribute(k) { this.attributes = this.attributes.filter(x => x.name !== k); },
    hasAttribute(k) { return this.attributes.some(x => x.name === k); },
    appendChild(n) { this.childNodes.push(n); return n; },
    removeChild(n) { const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); return n; },
    replaceChild(n, old) { const i = this.childNodes.indexOf(old); if (i >= 0) this.childNodes[i] = n; return old; },
    /* `applyNode()` asks whether the focused field is inside the container it
       is about to patch (#71) — that is the difference between "the tick left
       the form alone" and "the tick rebuilt it". */
    contains(n) {
      if (!n) return false;
      if (n === this) return true;
      return (this.childNodes || []).some(c => c.nodeType === 1 && c.contains && c.contains(n));
    },
    get textContent() { return this.childNodes.map(c => c.textContent).join(''); },
    set textContent(v) { textWrites++; this.childNodes = [textNode(v)]; },
    /* `el.id` reflects the attribute, like the browser's `HTMLElement.id` —
       the node form's input handler reads it (#71). */
    get id() { return this.getAttribute('id') || ''; },
    set id(v) { this.setAttribute('id', String(v)); },
    get outerHTML() { return serialise(this); },
  };
  if (tag === 'template') el.content = {childNodes: []};
  Object.defineProperty(el, 'innerHTML', {
    get() { return el.value; },
    set(v) {
      el.writes++;
      el.value = String(v);
      if (tag === 'template') el.content.childNodes = parseHTML(el.value);
    },
  });
  return el;
}

/* Tags, quoted attributes and text — the subset the page emits. `<` and `>`
 * inside text are escaped by the page, so a tag ends at the first `>`. */
const TOKEN = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=\/>]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
const ATTR = /([^\s=\/>]+)(?:="([^"]*)")?/g;

function parseHTML(html) {
  const root = {childNodes: []};
  const stack = [root];
  let m;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(html)) !== null) {
    const close = m[1], tag = m[2], attrText = m[3], selfClose = m[4], text = m[5];
    if (text !== undefined) {
      stack[stack.length - 1].childNodes.push(textNode(text));
      continue;
    }
    const name = String(tag).toLowerCase();
    if (close) {
      for (let i = stack.length - 1; i > 0; i--) {
        if (String(stack[i].tagName).toLowerCase() === name) { stack.length = i; break; }
      }
      continue;
    }
    const el = makeEl(name);
    if (attrText) {
      ATTR.lastIndex = 0;
      let a;
      while ((a = ATTR.exec(attrText)) !== null) {
        if (!a[1]) continue;
        el.setAttribute(a[1], a[2] === undefined ? '' : a[2]);
        /* A freshly parsed `<input value="…">` has that value as a property —
           the browser does the same, and the node form is measured through it
           (#71). Only at parse time: a later `setAttribute` from `sync()` must
           not overwrite what the operator typed. */
        if (name === 'input' && a[1] === 'value') el.value = a[2] === undefined ? '' : a[2];
      }
    }
    stack[stack.length - 1].childNodes.push(el);
    if (!selfClose && !VOID.has(name)) stack.push(el);
  }
  return root.childNodes;
}

function clone(n) {
  if (!n) return n;
  if (n.nodeType === 3) return textNode(n.nodeValue);
  const el = makeEl(String(n.tagName).toLowerCase());
  for (const a of n.attributes) el.setAttribute(a.name, a.value);
  for (const c of n.childNodes) el.appendChild(clone(c));
  /* `importNode` of an input takes the attribute, not what the operator typed
     into the live node — which is exactly why a replaced field loses it. */
  if (el.tagName === 'INPUT') el.value = el.getAttribute('value') || '';
  return el;
}

const elements = new Map();
let templates = 0;   // `apply()` only parses a template once it has decided to patch

/* ── die Shell (#56) ─────────────────────────────────────────────────────── */
/* Die Tab-Leiste steht in `ripple/dashboard.py`, nicht in `dashboard.js`. Damit
 * `paintTabs()` gegen die Knöpfe und Panels läuft, die der Browser bekommt,
 * liefert der Aufrufer die Shell mit (`fixture.shell`); sie wird hier mit
 * demselben Parser gelesen wie die Render-Ausgabe. Eine Kopie der Leiste im
 * Stub wäre genau die zweite Wahrheit, die dieser Test vermeiden soll. */
const shellIndex = {};   // CSS-Selektor → [Knoten]
const shellIds = {};     // id → Knoten aus der Shell (die Felder des Knoten-Tabs)
function indexShell(nodes) {
  const push = (sel, n) => { (shellIndex[sel] = shellIndex[sel] || []).push(n); };
  const walk = n => {
    if (!n || n.nodeType !== 1) return;
    const role = n.getAttribute('role');
    if (role) push('[role="' + role + '"]', n);
    for (const a of ['data-i18n', 'data-i18n-html', 'data-i18n-aria']) {
      if (n.getAttribute(a) !== null) push('[' + a + ']', n);
    }
    const id = n.getAttribute('id');
    if (id) shellIds[id] = n;
    for (const c of n.childNodes) walk(c);
  };
  (nodes || []).forEach(walk);
}

/* Eine id im Baum suchen — `getElementById` im Browser findet auch, was die
   Seite selbst gerendert hat (die Felder der Knoten-Vorschau, #71). */
function byId(root, id) {
  if (!root || root.nodeType !== 1) return null;
  if (root.getAttribute && root.getAttribute('id') === id) return root;
  for (const c of root.childNodes || []) {
    const hit = byId(c, id);
    if (hit) return hit;
  }
  return null;
}

function walk(n, out) {
  if (!n || n.nodeType !== 1) return;
  if (n.getAttribute('data-deadline') !== null) out.push(n);
  for (const c of n.childNodes) walk(c, out);
}

const document = {
  body: makeEl('body'),
  documentElement: makeEl('html'),
  /* `applyNode()` reads it (#71): null is "no field has the focus", which is
     what a tick sees most of the time. A test that wants the other case sets
     it to the field the operator is typing in. */
  activeElement: null,
  getElementById(id) {
    if (elements.has(id)) return elements.get(id);
    if (shellIds[id]) { elements.set(id, shellIds[id]); return shellIds[id]; }
    /* What the page itself rendered (the fields of the node preview, #71): the
       browser finds them, so the stub has to as well. Not cached — a patch may
       replace the node, and the measurement is exactly about that. */
    for (const root of elements.values()) {
      const hit = byId(root, id);
      if (hit) return hit;
    }
    elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  /* Only the deadline selector and the shell's own nodes are answered: the
     ticker asks for the first, `fillShell()`/`paintTabs()` for the second.
     Everything else is empty on purpose. */
  querySelectorAll(sel) {
    if (sel !== '[data-deadline]') return (shellIndex[sel] || []).slice();
    const out = [];
    for (const el of elements.values()) walk(el, out);
    walk(this.body, out);
    walk(this.documentElement, out);
    return out;
  },
  addEventListener() {},
  createElement(tag) {
    if (tag === 'template') templates++;
    return makeEl(tag);
  },
  importNode(node) { return clone(node); },
};

if (fixture.shell) indexShell(parseHTML(fixture.shell));

/* Der URL-Hash ist der Zustand der Tabs (#56): er wird gelesen, geschrieben und
   vom Test von Hand verstellt (Reload, Zurück-Knopf, unbekannter Hash). Jeder
   andere Zugriff auf `location` zählt als Navigation und wirft. */
/* `location.hash` trägt das `#` (so wie im Browser): `#models`, nicht `models`. */
let hashValue = String(fixture.hash || '');
const hashWrites = [];
let locationReads = 0;
const location = new Proxy({}, {
  get(_t, prop) {
    if (prop === 'hash') return hashValue;
    locationReads++;
    throw new Error('the dashboard must never navigate (read location.' + String(prop) + ')');
  },
  set(_t, prop, value) {
    if (prop === 'hash') {
      hashValue = '#' + String(value).replace(/^#/, '');
      hashWrites.push(hashValue);
      return true;
    }
    locationReads++;
    throw new Error('the dashboard must never navigate (write location.' + String(prop) + ')');
  },
});

/* `history.replaceState` schreibt den Hash, ohne einen Verlaufseintrag
   anzulegen — das ist der Weg, den die Seite nimmt, und hier der einzige, den
   sie nehmen darf. */
const history = {
  replaceState(_state, _title, url) {
    if (url === undefined || url === null) return;
    hashValue = String(url);
    hashWrites.push(hashValue);
  },
};

/* `sessionStorage['ripple_admin']` is the switch the page reads for the two
   operator actions. Flipping it between renders is how the stub measures both
   states of the maintenance control: without a key (disabled, with a reason)
   and with one (live buttons). */
let adminKey = null;

/* Every admin call the page makes, with its body: the node tab is measured
   through what it actually sent, not through what it meant to send (#71). */
const calls = [];

async function fetchStub(url, init) {
  const key = String(url).replace(/^\//, '');
  const method = (init && init.method) || 'GET';
  calls.push({method: method, path: key,
              body: init && init.body ? JSON.parse(init.body) : null});
  /* A fixture may name a status per (method, path) — the node tab needs the
     refusals as well as the successes: a drain that runs out of time answers
     409, a node from `config.yaml` answers 409 too, and both are states the
     page has to render (issue #71). */
  const table = fixture.responses || {};
  let entry = table[method + ' ' + key] !== undefined ? table[method + ' ' + key]
                                                      : table[key];
  /* A queued answer (#78): the same (method, path) refuses once with 409 and
     succeeds on the confirmed repeat, so the fixture hands the answers out in
     order. The last one repeats. */
  if (entry && Array.isArray(entry.queue)) {
    entry = entry.queue.length > 1 ? entry.queue.shift() : entry.queue[0];
  }
  if (entry !== undefined) {
    const status = typeof entry.status === 'number' ? entry.status : 200;
    const body = entry.body !== undefined ? entry.body : entry;
    return {
      ok: status < 400, status,
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  }
  if (!(key in fixture)) throw new Error('no fixture for ' + key);
  const body = fixture[key];
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

class FrozenDate extends Date {
  constructor(...args) { if (args.length) { super(...args); } else { super(clockMs); } }
  static now() { return clockMs; }
}

/* Every id the page asked for and did not find. The page falls back to English
   and says so — here that is a measurement, not noise: an id that is only ever
   built at runtime (`t('models.policy_' + level)`) is invisible to a static
   scan, and this is where it shows up. */
const i18nMisses = [];
const consoleStub = {
  log: (...a) => console.log(...a),
  warn: (...a) => { i18nMisses.push(a.join(' ')); },
  error: (...a) => { i18nMisses.push(a.join(' ')); },
};

/* Die Handler, die die Seite an `window` hängt: `hashchange` ist der Weg vom
   Zurück-Knopf in die Seite und wird unten von Hand ausgelöst. */
const winHandlers = {};
const windowStub = {
  /* The node tab asks before it writes (#71). A fixture that wants the
     confirmation answered sets `confirm: true`; the default stays "no", so no
     existing test can start writing by accident. */
  confirm: () => !!fixture.confirm, location,
  addEventListener(type, fn) { (winHandlers[type] = winHandlers[type] || []).push(fn); },
  removeEventListener() {},
};

const sandbox = {
  Date: FrozenDate,
  document,
  location,
  history,
  window: windowStub,
  sessionStorage: {getItem: k => (k === 'ripple_admin' ? adminKey : null),
                   setItem: () => {}, removeItem: () => {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: (fn) => { if (typeof fn === 'function') return 0; return 0; },
  clearTimeout: () => {},
  console: consoleStub,
};
sandbox.globalThis = sandbox;

/* `apply()` sets `host.__html` before it decides to patch, so the string is
   what was rendered into that container and the template counter says whether
   the DOM was touched at all. */
function rendered() {
  const out = {};
  for (const [id, el] of elements) out[id] = el.__html === undefined ? '' : el.__html;
  return out;
}

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});

  /* The script's first tick() is a promise; give it a turn to settle. */
  await new Promise(r => setTimeout(r, 100));
  const api = sandbox.RIPPLE;
  if (!api) throw new Error('dashboard.js did not export RIPPLE');

  const first = rendered();
  const firstTemplates = templates;

  /* The render functions are exported, so the caller can measure them directly
     (memory bar geometry, table shape, empty states) without a second run. */
  const idle = JSON.parse(JSON.stringify(fixture.healthz));
  idle.threads = {active: 0, target_share: {}, actual_share: {}, registry: []};
  idle.models.forEach(m => m.hosts.forEach(x => { x.threads = 0; }));
  /* Requests but no slot: the case the design's "kein Ist-Anteil" rule is for. */
  const idleStats = JSON.parse(JSON.stringify(fixture.stats));
  const noMeasured = JSON.parse(JSON.stringify(fixture.healthz));
  noMeasured.hosts.forEach(h => { h.used_bytes_measured = null; });

  const parts = {
    fleet: api.renderFleet(fixture.healthz),
    kpis: api.renderKpis(fixture.healthz, fixture.stats),
    models: api.renderModels(fixture.healthz),
    dist: api.renderDist(fixture.healthz, fixture.stats),
    dist_empty: api.renderDist(idle, idleStats),
    threads: api.renderThreads(fixture.healthz),
    load: api.renderLoad(fixture.healthz),
    /* Der Leerlauf-Umschalter (#39): derselbe Block mit ausgeblendetem Leerlauf,
       danach wieder zurück, damit die anderen Teile unberührt bleiben. */
    load_idle_hidden: (api.loadSetHideIdle(true), api.renderLoad(fixture.healthz)),
    log: api.renderLog(fixture.stats),
    params: api.renderParams(fixture.config),
    fleet_unmeasured: api.renderFleet(noMeasured),
  };
  api.loadSetHideIdle(false);    // back to the default view for the other parts

  /* The model policy (#11): the fixture says which (host, model) pair carries
     which level; the stub copies it into /healthz the way the backend does —
     `policy` on the row, `dispatch` false for both levels. */
  const pol = fixture.policy || {};
  if (Object.keys(pol).length) {
    const view = JSON.parse(JSON.stringify(fixture.healthz));
    view.models.forEach(m => m.hosts.forEach(x => {
      const level = (pol[x.host] || {})[m.id];
      if (level) { x.policy = level; x.dispatch = false; }
    }));
    parts.models_policy = api.renderModels(view);
  }

  /* The replication panel (#13): the empty state (what a fresh page shows), a
     plan with numbers, a refused plan and a running job. `replState` is the
     panel's own memory — the stub sets it the way a click would. */
  parts.replication = api.renderReplication(fixture.healthz);
  const repl = fixture.replication || {};
  if (repl.plan) {
    api.replState.plan = repl.plan;
    api.replState.source = repl.plan.source;
    api.replState.target = repl.plan.target;
    api.replState.model = repl.plan.model;
    parts.replication_plan = api.renderReplication(fixture.healthz);
    api.replState.job = repl.job || null;
    parts.replication_job = api.renderReplication(fixture.healthz);
    api.replState.plan = null;
    api.replState.job = null;
    api.replState.source = '';
    api.replState.target = '';
    api.replState.model = '';
  }

  /* The maintenance control (#10) in all three states it can be in: without a
     key, with a key, and with a key on a node that has a window — once while
     requests are still running, once with the node drained. The windows come
     from the fixture so the test owns the numbers it measures. */
  adminKey = 'test-admin-key';
  parts.fleet_admin = api.renderFleet(fixture.healthz);
  parts.models_admin = api.renderModels(fixture.healthz);
  /* The ledger block (#33) with a key: the reset control is live. */
  parts.load_admin = api.renderLoad(fixture.healthz);
  /* The thread block (#35) with a key: the delete control is live — and off
     again as soon as the thread has a request in flight, because the route
     refuses that one with 409. */
  parts.threads_admin = api.renderThreads(fixture.healthz);
  const busy = JSON.parse(JSON.stringify(fixture.healthz));
  busy.threads.registry[0].active = 1;
  parts.threads_active = api.renderThreads(busy);

  const maint = fixture.maintenance || {};
  for (const [name, state] of Object.entries(maint)) {
    const view = JSON.parse(JSON.stringify(fixture.healthz));
    view.hosts[0].state = 'maintenance';
    view.hosts[0].inflight = state.inflight;
    view.hosts[0].maintenance = state.window;
    parts['fleet_' + name] = api.renderFleet(view);
  }
  adminKey = null;

  /* Second tick, same data: nothing may be written — that is what "no flicker"
     means for a 2 s poll. snapTime/conn are excluded by the caller because a
     clock and a timestamp legitimately change. */
  await api.tick();
  await new Promise(r => setTimeout(r, 20));
  const unchangedTemplates = templates - firstTemplates;

  /* Third tick, one new decision at the end of the ring buffer: exactly the log
     container may be written, and the container element itself must stay the
     same object (it is never replaced, only its rows are reconciled). */
  const logEl = document.getElementById('log');
  const beforeThird = rendered();
  const beforeThirdTemplates = templates;
  fixture.stats.decisions = fixture.stats.decisions.concat([{
    time: '13:45:00', model: 'DeepSeek-V4.1-Flash-1017-oQ4e-mtp', host: 'alpha',
    reason: 'thread', prompt_chars: 1219, duration_ms: 999.0, status: 200,
  }]);
  await api.tick();
  await new Promise(r => setTimeout(r, 20));
  const afterThird = rendered();
  const changed = Object.keys(afterThird).filter(id => afterThird[id] !== (beforeThird[id] || ''));
  const changedTemplates = templates - beforeThirdTemplates;
  const logAfter = afterThird.log || '';

  /* The collapse row: open by default since the Models tab exists (#56) — the
     tab's job is to show what the fleet has. Captured in all three states, so a
     test can hold the default, the collapse and the reopen against each other.
     Done after the tick measurements, because it patches on purpose. */
  parts.models_default = document.getElementById('models').__html;
  api.setDim(false);
  parts.models_collapsed = document.getElementById('models').__html;
  api.setDim(true);
  parts.models_expanded = document.getElementById('models').__html;

  /* A language switch must change the words, not the markup (issue #17): the
     same render functions, run in the other language on the same payload, have
     to produce the same elements with the same classes. Compared as a skeleton —
     text nodes and the readable attributes removed — so a number that changes
     from 1.234,5 to 1,234.5 is not a difference but a moved column is. Both
     sides are rendered here, side by side, so a fixture change between the two
     cannot show up as a language difference. */
  const skeleton = h => String(h)
    .replace(/>[^<]*</g, '><')
    .replace(/\s(?:title|data-tip|aria-label|placeholder|value)="[^"]*"/g, '');
  const draw = () => ({
    fleet: api.renderFleet(fixture.healthz),
    kpis: api.renderKpis(fixture.healthz, fixture.stats),
    models: api.renderModels(fixture.healthz),
    dist: api.renderDist(fixture.healthz, idleStats),
    threads: api.renderThreads(fixture.healthz),
    load: api.renderLoad(fixture.healthz),
    log: api.renderLog(fixture.stats),
    params: api.renderParams(fixture.config),
  });
  const numCells = h => Array.from(String(h).matchAll(/<td>([-+]?\d[\d.,]*)<\/td>/g))
    .map(m => m[1]);
  const baseLang = api.lang;
  const here = draw();
  const otherLang = baseLang === 'de' ? 'en' : 'de';
  api.chooseLang(otherLang);
  const there = draw();
  const langNumbers = {[baseLang]: numCells(here.params), [otherLang]: numCells(there.params)};
  const langShape = Object.keys(here).filter(k => skeleton(here[k]) !== skeleton(there[k]));
  const langText = Object.keys(here).filter(k => here[k] !== there[k]).length;
  api.chooseLang(api.lang === 'de' ? 'en' : 'de');   /* back, so the caller's
                                                        other measurements and
                                                        the fixture stay put */

  /* ── Tabs (#56) ────────────────────────────────────────────────────────────
   * Der Hash ist die Wahrheit, nicht ein gespeicherter Zustand: gemessen wird
   * gegen die Knöpfe und Panels aus der mitgelieferten Shell (`fixture.shell`).
   * Ein Tick darf den Tab nicht verlieren (der 2-s-Patch ersetzt Knoten), ein
   * Sprachwechsel auch nicht (die Wörter wechseln, der Tab bleibt). */
  const tabSnapshot = () => ({
    active: api.activeTab,
    hash: hashValue,
    writes: hashWrites.slice(),
    buttons: (shellIndex['[role="tab"]'] || []).map(b => ({
      id: b.getAttribute('data-tab'),
      selected: b.getAttribute('aria-selected'),
      tabindex: b.getAttribute('tabindex'),
      text: b.textContent,
    })),
    panels: (shellIndex['[role="tabpanel"]'] || []).map(p => ({
      id: p.getAttribute('data-tab'),
      hidden: p.hasAttribute('hidden'),
    })),
    view: api.tabView(api.activeTab),
  });

  const tabs = {catalog: api.TABS.slice(), default_tab: api.DEFAULT_TAB,
                boot: tabSnapshot()};

  api.setTab('models');
  tabs.after_models = tabSnapshot();

  /* Ein Tick mit denselben Daten: der Tab bleibt, die Leiste auch. */
  await api.tick();
  await new Promise(r => setTimeout(r, 20));
  tabs.after_tick = tabSnapshot();

  /* Der Weg vom Hash in die Seite — das ist der Reload, der Zurück-Knopf und
     die von Hand getippte Adresse. */
  hashValue = '#history';
  api.applyHash();
  tabs.from_hash = tabSnapshot();

  /* Ein Hash, den es nicht gibt: `Flotte`, und der Hash bleibt stehen, statt
     überschrieben zu werden. */
  hashValue = '#gibtsnicht';
  api.applyHash();
  tabs.unknown_hash = tabSnapshot();

  /* Der Hash ändert sich ohne Reload — Zurück-Knopf, Lesezeichen, getippte
     Adresse. Der `hashchange`-Handler der Seite muss den Tab nachziehen. */
  hashValue = '#ops';
  (winHandlers['hashchange'] || []).forEach(fn => fn({type: 'hashchange'}));
  tabs.after_hashchange = tabSnapshot();

  /* Sprachwechsel auf `Modelle`: die Wörter wechseln, der Tab nicht. */
  hashValue = '#models';
  api.applyHash();
  const tabLang = api.lang;
  api.chooseLang(tabLang === 'de' ? 'en' : 'de');
  tabs.after_lang = tabSnapshot();
  api.chooseLang(tabLang);

  /* Zurück auf `Flotte`, damit die folgenden Messungen den Ausgangszustand
     sehen. */
  api.setTab('fleet');
  tabs.reset = tabSnapshot();
  /* Jeder Tab, aus der reinen Sicht: genau einer ist gewählt und sichtbar. */
  tabs.view_of = {};
  for (const id of api.TABS) tabs.view_of[id] = api.tabView(id);

  /* ── Deadlines (#34) ──────────────────────────────────────────────────────
   * Two numbers on the page are deadlines. The page writes them once, as text
   * inside `[data-deadline]`, and the 1 s ticker only rewrites that text. What
   * is measured here: the text after a real render, the same DOM node one
   * minute later, and what the page says when a deadline has passed. */
  const deadlines = {
    load: api.deadlineText('load', 142),
    load_short: api.deadlineText('load', 42),
    load_zero: api.deadlineText('load', 0),
    load_over: api.deadlineText('load', -95),
    thread: api.deadlineText('thread', 1620),
    thread_short: api.deadlineText('thread', 42),
    thread_zero: api.deadlineText('thread', 0),
    thread_over: api.deadlineText('thread', -3),
    countdown: [api.countdown(142), api.countdown(59), api.countdown(0)],
  };

  /* A real render with a loading entry on the first node: the countdown in the
     card and in the thread row, and the node identity the ticker has to keep. */
  fixture.healthz.hosts[0].loading = fixture.loading;
  await api.tick();
  await new Promise(r => setTimeout(r, 20));
  parts.fleet_loading = document.getElementById('fleet').__html;
  parts.threads_loading = document.getElementById('threads').__html;

  const before = document.querySelectorAll('[data-deadline]').map(el => ({
    el, text: el.textContent,
    at: el.getAttribute('data-deadline'), kind: el.getAttribute('data-deadline-kind'),
  }));
  const writesBefore = textWrites, templatesBefore = templates;
  clockMs += 60000;                       // one minute later
  api.paintDeadlines();
  const after = document.querySelectorAll('[data-deadline]');
  const ticker = {
    count: before.length,
    same_nodes: after.length === before.length && after.every((el, i) => el === before[i].el),
    before: before.map(b => b.text),
    after: after.map(el => el.textContent),
    kinds: before.map(b => b.kind),
    deadlines: before.map(b => b.at),
    text_writes: textWrites - writesBefore,
    templates: templates - templatesBefore,
  };

  /* A re-render replaces the nodes; the ticker has to find the new ones. The
     load moved on by 30 s, so the countdown restarts from the payload. */
  fixture.healthz.hosts[0].loading = Object.assign({}, fixture.loading,
    {started_at: fixture.loading.started_at + 30});
  await api.tick();
  await new Promise(r => setTimeout(r, 20));
  const rerendered = document.querySelectorAll('[data-deadline]');
  const afterRender = {
    count: rerendered.length,
    texts: rerendered.map(el => el.textContent),
  };
  clockMs += 10000;
  api.paintDeadlines();
  afterRender.texts_after_paint = document.querySelectorAll('[data-deadline]')
    .map(el => el.textContent);

  /* A browser on another machine: the server's clock is two hours ahead of the
     browser's. The countdown has to follow the server, so the same payload
     still reads the same remaining time. */
  const skew = JSON.parse(JSON.stringify(fixture.healthz));
  const serverNow = fixture.healthz.server_time;
  skew.server_time = serverNow + 7200;
  skew.hosts[0].loading = {model: 'X', size_gb: 1, started_at: skew.server_time - 60,
                           eta_seconds: 202, source: 'test', seen_loading: true,
                           idle_unloaded: false};
  api.anchorClock(skew);
  const skewNow = api.pageNow();
  const skewFleet = api.renderFleet(skew);
  const skewText = (skewFleet.match(/data-deadline-kind="load">([^<]*)</) || [])[1];
  const skewDeadline = (skewFleet.match(/data-deadline="([^"]*)"/) || [])[1];
  api.anchorClock(fixture.healthz);       // back to the fixture's clock

  /* The same two numbers in German: the text is a catalog entry, not a format
     the script knows. */
  api.chooseLang('de');
  const deadlinesDe = {
    load: api.deadlineText('load', 142),
    load_zero: api.deadlineText('load', 0),
    thread: api.deadlineText('thread', 1620),
    thread_zero: api.deadlineText('thread', 0),
  };
  api.chooseLang(baseLang);

  /* ── Knoten (#71) ──────────────────────────────────────────────────────────
   * Der Tab wird über die echten Funktionen gefahren: Adresse in das Shell-Feld,
   * `nodePreview()` (fetch auf /admin/nodes?dry_run=true), dann ein Tick, der die
   * Karte ändert — gemessen wird, ob das getippte Gewicht denselben Knoten und
   * seinen Wert behält. Danach die Suche, der Drain-Dialog und seine drei
   * Ausgänge (entfernt, Frist abgelaufen, Knoten aus config.yaml).
   *
   * Warum das die eigentliche Frage des Issues ist: `apply()` ersetzt Knoten,
   * und ein Feld, das dabei neu entsteht, verliert Text und Cursor. Die drei
   * Adressfelder stehen deshalb in der Shell, die vier Felder der Vorschau in
   * `nodeState` — hier wird beides gemessen, nicht behauptet. */
  adminKey = 'test-admin-key';
  const nodes = {};

  /* Zwei Knoten mit Herkunft: einer aus der Datei, einer aus dem Overlay, und
     auf dem zweiten läuft Arbeit — die Zahlen des Drain-Dialogs. */
  const nv = JSON.parse(JSON.stringify(fixture.healthz));
  nv.hosts[0].source = 'config';
  nv.hosts[1].source = 'overlay';
  nv.hosts[1].inflight = 2;
  nv.models.forEach(m => m.hosts.forEach(x => {
    if (x.host === nv.hosts[1].name) x.inflight = 1;
  }));
  Object.assign(fixture.healthz, nv);
  await api.tick();
  await new Promise(r => setTimeout(r, 20));

  nodes.list = api.renderNodeList(fixture.healthz);
  nodes.empty_list = api.renderNodeList({hosts: []});

  /* 7.1 Vorschau: die Adresse steht im Shell-Feld, ein Aufruf, keine Schrift. */
  document.getElementById('nodeAddress').value = 'http://192.0.2.40:8000';
  let mark = calls.length;
  await api.nodePreview();
  nodes.preview_calls = calls.slice(mark).map(c => c.method + ' ' + c.path);
  nodes.preview = api.renderNodePreview(fixture.config);
  nodes.preview_html = document.getElementById('nodePreview').__html;
  nodes.draft = JSON.parse(JSON.stringify(api.nodeState.draft));

  /* Das getippte Gewicht: erst in den Zustand (wie der input-Handler), dann ein
     Tick, der die Karte wirklich ändert — der Knopf geht während des Sendens
     aus. `sync()` läuft also, und das Feld muss trotzdem dasselbe bleiben. */
  const weight = byId(document.getElementById('nodePreview'), 'nodeWeight');
  nodes.weight_default = weight ? weight.value : null;
  if (weight) weight.value = '2.5';
  api.nodeInputChanged(weight);
  nodes.draft_weight = api.nodeState.draft.weight;
  api.nodeState.busy = true;
  api.paintNodes(fixture.healthz, fixture.config);
  api.nodeState.busy = false;
  const weightAfter = byId(document.getElementById('nodePreview'), 'nodeWeight');
  nodes.field_same_node = weightAfter === weight;
  nodes.field_value_kept = weightAfter ? weightAfter.value : null;
  nodes.field_value_attribute = weightAfter ? weightAfter.getAttribute('value') : null;
  nodes.card_changed = document.getElementById('nodePreview').__html !== nodes.preview_html;

  /* Ein fokussiertes Feld wird gar nicht erst gepatcht (#71). */
  document.activeElement = weightAfter;
  const htmlBeforeFocus = document.getElementById('nodePreview').__html;
  api.nodeState.draft.name = 'B-umbenannt';
  api.paintNodes(fixture.healthz, fixture.config);
  nodes.focus_deferred = document.getElementById('nodePreview').__html === htmlBeforeFocus;
  document.activeElement = null;
  api.paintNodes(fixture.healthz, fixture.config);
  nodes.focus_released_html = document.getElementById('nodePreview').__html;

  /* Der Name wurde „getippt": der Wert steht am Feld, der Handler zieht ihn in
     den Zustand — genau der Weg, den `nodeReadDraft()` beim Klick liest. */
  const nameEl = byId(document.getElementById('nodePreview'), 'nodeName');
  if (nameEl) nameEl.value = 'B-umbenannt';
  api.nodeInputChanged(nameEl);
  nodes.draft_name = api.nodeState.draft.name;

  /* Die zwei Sonderfälle der Vorschau, als reine Renderfunktion: ein
     widersprochener Standort und eine unlesbare Modellliste. */
  const keepPreview = api.nodeState.preview;
  api.nodeState.preview = fixture.node_preview_mismatch;
  nodes.preview_mismatch = api.renderNodePreview(fixture.config);
  api.nodeState.preview = fixture.node_preview_partial;
  nodes.preview_partial = api.renderNodePreview(fixture.config);
  api.nodeState.preview = keepPreview;

  /* Die Bestätigung: was in den Feldern steht, geht so hinaus. */
  mark = calls.length;
  await api.nodeAdd();
  nodes.add_calls = calls.slice(mark).map(c => c.method + ' ' + c.path);
  nodes.add_body = (calls.slice(mark).filter(
    c => c.method === 'POST' && c.path === 'admin/nodes')[0] || {}).body || null;
  nodes.add_msg = api.renderNodeMsg();
  nodes.preview_after_add = document.getElementById('nodePreview').__html;

  /* Ohne Rückfrage kein Schreiben: derselbe Klick, `confirm` sagt nein. */
  const markNoConfirm = calls.length;
  fixture.confirm = false;
  await api.nodePreview();
  await api.nodeAdd();
  nodes.no_confirm_calls = calls.slice(markNoConfirm)
    .map(c => c.method + ' ' + c.path)
    .filter(p => p.indexOf('POST admin/nodes') === 0 && p.indexOf('dry_run') === -1);
  fixture.confirm = true;

  /* 7.2 Suche: ein Fund mit Vorschau, ein Fund, der schon in der Flotte ist. */
  document.getElementById('nodeCidr').value = '127.0.0.1/32';
  mark = calls.length;
  await api.nodeScan();
  nodes.scan_calls = calls.slice(mark).map(c => c.method + ' ' + c.path);
  nodes.scan_body = (calls.slice(mark).filter(
    c => c.path === 'admin/discover')[0] || {}).body || null;
  nodes.found = api.renderNodeFound();
  nodes.found_empty = (api.nodeState.found = {cidr: '127.0.0.1/32', count: 0, found: []},
                       api.renderNodeFound());

  /* 7.3 Der Drain-Dialog: was in Flug ist und wie lange gewartet wird. */
  api.nodeAskRemove(nv.hosts[1].name);
  nodes.drain = api.renderNodeDrain();
  nodes.drain_html = document.getElementById('nodeDrain').__html;

  /* Die Frist läuft ab: 409, nichts entfernt, der Dialog bleibt stehen. */
  await api.nodeDrainConfirm();
  nodes.drain_timeout_msg = api.renderNodeMsg();
  nodes.drain_after_timeout = api.renderNodeDrain();

  /* Zweiter Versuch mit Antwort: entfernt, der Dialog schließt. */
  fixture.responses['DELETE admin/nodes/' + nv.hosts[1].name] =
    {status: 200, body: {removed: true, node: nv.hosts[1].name, waited_seconds: 1.25,
                         threads_released: 2, inflight: 0}};
  await api.nodeDrainConfirm();
  nodes.drain_removed_msg = api.renderNodeMsg();
  nodes.drain_closed = api.renderNodeDrain() === '';

  /* Ein Knoten aus config.yaml: der Dialog darf ihn nicht entfernen. */
  api.nodeAskRemove(nv.hosts[0].name);
  await api.nodeDrainConfirm();
  nodes.config_msg = api.renderNodeMsg();
  nodes.config_dialog_closed = api.renderNodeDrain() === '';

  /* Ohne Schlüssel ist der Knopf aus und sagt warum — mit einer Vorschau im
     Kasten, denn ohne Vorschau gibt es keinen Knopf zu begründen. */
  adminKey = null;
  nodes.list_without_key = api.renderNodeList(fixture.healthz);
  api.nodeState.preview = keepPreview;
  nodes.preview_without_key = api.renderNodePreview(fixture.config);
  api.nodeState.preview = null;

  /* ── 6.5: Herkunft, Feld, Zurücksetzen (#78) ───────────────────────────────
   * Gemessen wird, was die Seite baut und schickt: der Pfad in der URL, der
   * Rumpf der PATCH-Anfrage, die zweite Anfrage nach der Rückfrage — und der
   * Text, der nach einer Ablehnung neben dem Feld steht. */
  const params = {};
  if (fixture.config_live) {
    /* Derselbe Payload in den fünf Sprachen, an denen sich die Spaltenbreiten
       entscheiden (Griechisch, Polnisch, Tschechisch, Deutsch, Englisch). Das
       Rendern tauscht die Sprache der Seite — deshalb steht es hier, nach den
       Messungen an einem Tick, und stellt die Sprache am Ende zurück. */
    const paramsLang = api.lang;
    parts.params_live = {};
    for (const code of ['en', 'de', 'pl', 'cs', 'el']) {
      api.chooseLang(code);
      parts.params_live[code] = api.renderParams(fixture.config_live);
    }
    api.chooseLang(paramsLang);

    /* The payload of an older server, before this block replaces `/config` with
       the one that carries `editable` and `overrides`. */
    const legacyCfg = JSON.parse(JSON.stringify(fixture.config));
    const live = JSON.parse(JSON.stringify(fixture.config_live));
    fixture.config = live;
    adminKey = 'test-admin-key';
    await api.tick();
    params.block = document.getElementById('params').__html;

    /* Ein gültiger Wert: eine PATCH mit `confirm: false`, danach liest der
       bestehende Takt `/config` neu — kein zweiter Auffrischweg. */
    let mark = calls.length;
    params.saved = await api.paramsSubmit('affinity.ttl_seconds', 900);
    params.save_calls = calls.slice(mark)
      .filter(c => c.method !== 'GET').map(c => c.method + ' ' + c.path);
    params.save_body = (calls.slice(mark).filter(c => c.method === 'PATCH')[0] || {}).body;
    live.overrides['affinity.ttl_seconds'] = 900;
    await api.tick();
    params.after_save = document.getElementById('params').__html;

    /* Der Wert, den der Lader ablehnt: seine Meldung steht neben dem Feld. */
    mark = calls.length;
    params.refused = await api.paramsSubmit('affinity.ttl_seconds', -5);
    params.refused_html = document.getElementById('params').__html;

    /* Das Gewicht: 409, die Zahl der Threads, die umziehen würden — und erst
       die zweite Anfrage trägt `confirm: true`. */
    const weightKey = 'PATCH admin/config/hosts.alpha.weight';
    const weightQueue = JSON.parse(JSON.stringify(fixture.responses[weightKey]));
    params.question = api.paramsWeightQuestion(3, 2.5);

    fixture.responses[weightKey] = JSON.parse(JSON.stringify(weightQueue));
    fixture.confirm = false;
    mark = calls.length;
    params.declined = await api.paramsSubmit('hosts.alpha.weight', 2.5);
    params.declined_calls = calls.slice(mark)
      .filter(c => c.method === 'PATCH').map(c => c.path + ' ' + JSON.stringify(c.body));

    fixture.responses[weightKey] = JSON.parse(JSON.stringify(weightQueue));
    fixture.confirm = true;
    mark = calls.length;
    params.confirmed = await api.paramsSubmit('hosts.alpha.weight', 2.5);
    params.confirmed_calls = calls.slice(mark)
      .filter(c => c.method === 'PATCH').map(c => c.path + ' ' + JSON.stringify(c.body));

    /* Zurücksetzen: eine DELETE ohne Rumpf. */
    mark = calls.length;
    params.reset = await api.paramsReset('affinity.ttl_seconds');
    params.reset_calls = calls.slice(mark)
      .filter(c => c.method === 'DELETE').map(c => c.method + ' ' + c.path);

    /* Ohne Schlüssel: die Felder sind aus und der Satz der anderen
       Betreiberblöcke steht über der Tabelle. */
    adminKey = null;
    params.no_key = api.renderParams(live);

    /* Ein älterer Server: kein `editable`, kein `overrides`. */
    params.legacy = api.renderParams(legacyCfg);
    adminKey = 'test-admin-key';
  }

  process.stdout.write(JSON.stringify({
    containers: first,
    unchanged_templates: unchangedTemplates,
    changed_containers: changed,
    changed_templates: changedTemplates,
    log_has_new_row: logAfter.indexOf('13:45:00') !== -1,
    log_element_reused: document.getElementById('log') === logEl,
    location_reads: locationReads,
    lang_shape_diffs: langShape,
    lang_parts: Object.keys(there).length,
    lang_parts_with_new_text: langText,
    lang_numbers: langNumbers,
    i18n_misses: i18nMisses,
    tabs,
    deadlines,
    deadlines_de: deadlinesDe,
    ticker,
    after_render: afterRender,
    skew: {now: skewNow, text: skewText, deadline: skewDeadline},
    nodes,
    params,
    parts,
  }));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
