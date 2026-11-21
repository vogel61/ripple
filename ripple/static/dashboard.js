/* RIPPLE dashboard — behaviour (FA-22).
 *
 * One file, no framework, no build step, no CDN. It reads `/healthz`, `/stats`
 * and `/config` and patches the DOM every two seconds. It never reloads the
 * page: the decision log keeps its scroll position, the tooltips keep working
 * and nothing flickers (see `apply()` at the bottom).
 *
 * The design is `docs/dashboard-mockup.html`, the reasoning behind it
 * `docs/dashboard-design.md`. Two rules from there show up everywhere below:
 *
 *   - Every number is wrapped in `T(key, …)`, so it carries its definition and
 *     the endpoint + field it came from. A number without a source is not a
 *     number, it is a rumour.
 *   - Two memory bases are never mixed: the card shows `used_bytes_measured`
 *     (sum of `actual_size`), the guard decides on `used_bytes` (the estimate).
 *     Fill, occupancy and reserve come from the measurement; the dashed hard
 *     threshold and the bar's colour come from the estimate.
 */
'use strict';

const $ = id => document.getElementById(id);

/* ── Sprachen (issue #17) ────────────────────────────────────────────────── */
/* `ripple/static/i18n/en.json` ist die Quelle: jede Beschriftung, jeder Satz
   und jeder Tooltip steht dort unter einer flachen id, `de.json` hat genau
   denselben Schlüsselsatz. Beide Kataloge kommen beim Start mit der Seite
   (FA-22: nichts aus dem Netz), danach kostet ein Tick weiter genau
   /healthz, /stats und /config.

   Reihenfolge der Sprache: eine Wahl im GUI (`localStorage['ripple_lang']`),
   sonst `navigator.languages` — der erste unterstützte Eintrag gewinnt —,
   sonst Englisch. Eine fehlende id fällt auf Englisch zurück und wird einmal
   in der Konsole genannt; sie wird nie zu einem leeren String. */
const LANGS = [
  /* `locale` ist die Intl-Kennung: Deutsch bleibt de-AT (was die Seite vor
     diesem Issue fest verdrahtet hatte), Englisch ist en-US — die Vorgabe von
     Intl für „en“, mit Punkt als Tausendertrenner und 12-Stunden-Uhr. */
  {code: 'de', label: 'Deutsch', flag: '\u{1F1E9}\u{1F1EA}', locale: 'de-AT'},
  {code: 'en', label: 'English', flag: '\u{1F1EC}\u{1F1E7}', locale: 'en-US'},
  /* tr/fr: Entwürfe, kein Muttersprachler hat sie gelesen (docs/dashboard-design.md §10).
     `locale` ist die Intl-Kennung: Türkisch gruppiert mit Punkt und trennt Dezimalen mit
     Komma, Französisch gruppiert mit schmalem Leerzeichen — beides die CLDR-Vorgabe,
     also kein Eintrag in GROUP_DOT. */
  {code: 'tr', label: 'Türkçe', flag: '\u{1F1F9}\u{1F1F7}', locale: 'tr-TR'},
  {code: 'fr', label: 'Français', flag: '\u{1F1EB}\u{1F1F7}', locale: 'fr-FR'},
  {code: 'it', label: 'Italiano', flag: '\u{1F1EE}\u{1F1F9}', locale: 'it-IT'},
  {code: 'es', label: 'Español', flag: '\u{1F1EA}\u{1F1F8}', locale: 'es-ES'},
  {code: 'pt', label: 'Português', flag: '\u{1F1F5}\u{1F1F9}', locale: 'pt-PT'},
  {code: 'ru', label: 'Русский', flag: '\u{1F1F7}\u{1F1FA}', locale: 'ru-RU'},
  /* Die neun Sprachen von 2026-11-16: Entwuerfe, maschinell erstellt, kein
     Muttersprachler hat sie gelesen (docs/dashboard-design.md §10). Die
     `locale` ist die Intl-Kennung; Zahlen und Zeiten kommen aus `Intl`, nur das
     Deutsche hat mit GROUP_DOT eine eigene Regel. */
  {code: 'zh', label: '中文', flag: '\u{1F1E8}\u{1F1F3}', locale: 'zh-CN'},
  {code: 'pl', label: 'Polski', flag: '\u{1F1F5}\u{1F1F1}', locale: 'pl-PL'},
  {code: 'hu', label: 'Magyar', flag: '\u{1F1ED}\u{1F1FA}', locale: 'hu-HU'},
  {code: 'cs', label: 'Čeština', flag: '\u{1F1E8}\u{1F1FF}', locale: 'cs-CZ'},
  {code: 'sk', label: 'Slovenčina', flag: '\u{1F1F8}\u{1F1F0}', locale: 'sk-SK'},
  {code: 'sl', label: 'Slovenščina', flag: '\u{1F1F8}\u{1F1EE}', locale: 'sl-SI'},
  {code: 'hr', label: 'Hrvatski', flag: '\u{1F1ED}\u{1F1F7}', locale: 'hr-HR'},
  {code: 'el', label: 'Ελληνικά', flag: '\u{1F1EC}\u{1F1F7}', locale: 'el-GR'},
];
const FALLBACK = 'en';
const LANG_KEY = 'ripple_lang';
const catalogs = {};          // code → {id: text}
const i18nWarned = new Set();
let lang = FALLBACK;

const supported = code => {
  const base = String(code || '').toLowerCase().split('-')[0];
  return LANGS.some(l => l.code === base) ? base : null;
};
const langInfo = () => LANGS.find(l => l.code === lang) || LANGS[1];
const locale = () => langInfo().locale;

/* `{name}`-Platzhalter; ein fehlender Wert bleibt als `{name}` stehen, damit
   die Lücke sichtbar ist statt zu einem halben Satz zu werden. */
const fill = (s, params) => String(s).replace(/\{(\w+)\}/g, (m, k) =>
  (params[k] === undefined || params[k] === null) ? m : String(params[k]));

function t(id, params) {
  const cat = catalogs[lang] || {};
  let s = cat[id];
  if (s === undefined) {
    s = (catalogs[FALLBACK] || {})[id];
    if (s === undefined) {
      if (!i18nWarned.has(id)) {
        i18nWarned.add(id);
        console.warn('i18n: unknown message id "' + id + '"');
      }
      return id;                      // never an empty string
    }
    if (!i18nWarned.has(id)) {
      i18nWarned.add(id);
      console.warn('i18n: no "' + lang + '" text for "' + id + '", using English');
    }
  }
  return params ? fill(s, params) : s;
}

/* Beide Kataloge beim Start; ein fehlender Katalog wird genannt und die Seite
   läuft mit ids weiter, statt leer zu bleiben. */
async function loadCatalogs() {
  await Promise.all(LANGS.map(async l => {
    try {
      const r = await fetch('/static/i18n/' + l.code + '.json');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      catalogs[l.code] = await r.json();
    } catch (e) {
      catalogs[l.code] = catalogs[l.code] || {};
      console.warn('i18n: catalog ' + l.code + ' not loaded — ' + (e && e.message ? e.message : e));
    }
  }));
}

/* Die Shell trägt ids, nicht Text: `data-i18n` (Text) und `data-i18n-html`
   (die wenigen Absätze mit <b>/<code>). Beides wird beim Start und nach jedem
   Sprachwechsel gefüllt — eine HTML, keine Kopie je Sprache. */
const shellNodes = attr => (typeof document.querySelectorAll === 'function'
  ? Array.from(document.querySelectorAll('[' + attr + ']')) : []);

function fillShell() {
  for (const el of shellNodes('data-i18n')) el.textContent = t(el.getAttribute('data-i18n'));
  for (const el of shellNodes('data-i18n-html')) el.innerHTML = t(el.getAttribute('data-i18n-html'));
  for (const el of shellNodes('data-i18n-aria')) el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria')));
  if (document.documentElement && document.documentElement.setAttribute) {
    document.documentElement.setAttribute('lang', lang);
  }
}

const storedLang = () => {
  try { return supported(localStorage.getItem(LANG_KEY)); } catch (e) { return null; }
};
const systemLang = () => {
  const nav = typeof navigator === 'undefined' ? null : navigator;
  const list = (nav && (nav.languages && nav.languages.length ? nav.languages : [nav.language])) || [];
  for (const tag of list) { const c = supported(tag); if (c) return c; }
  return null;
};

/* Ein Sprachwechsel füllt die Shell neu und rendert den letzten Datensatz
   erneut — derselbe Weg wie ein Tick, also dieselbe DOM-Form. */
function applyLang(code) {
  lang = supported(code) || FALLBACK;
  fillShell();
  /* Der Chip im Kopf trägt seinen Tooltip aus dem Katalog und seinen Zustand
     im Attribut: `fillShell()` schreibt das Wort, `adminPaint()` den Rest
     (#130). */
  adminPaint();
  if (last) render(last.h, last.s, last.c);
  const sel = $('langSel');
  if (sel && sel.value !== lang) sel.value = lang;
}

function chooseLang(code) {
  const c = supported(code) || FALLBACK;
  try { localStorage.setItem(LANG_KEY, c); } catch (e) { /* privater Modus */ }
  applyLang(c);
}

/* ── Tabs (issue #56, five since #73) ─────────────────────────────────────── */
/* Fünf Tabs, ein Zustand: der URL-Hash. `activeTab` ist nur die zuletzt
   gelesene Antwort, nicht die Wahrheit — gelesen wird der Hash (beim Start und
   bei jedem `hashchange`), und nichts wird aus dem DOM gelesen: der 2-s-Patch
   ersetzt Knoten, ein Tab in einer Klasse am Container wäre nach einem Tick
   weg. Der Hash überlebt Reload und Sprachwechsel, weil der Browser ihn
   behält.

   `#fleet` ist die Vorgabe: kein Hash und jeder unbekannte Hash landen dort.
   Die Nummern 1…6 bleiben in den Überschriften stehen (Anker auf die
   Anforderung), die Namen der Tabs kommen aus dem Katalog (`tab.*`). Seit #73
   sind es fünf: die Thread-Registry ist aus dem Flotten-Tab herausgewachsen.
   Seit #71 ein sechstes, `Knoten` — angehängt, damit die Lesereihenfolge 1–6
   unverändert bleibt und `Betrieb` die Wartungsblöcke 6.1…6.6 behält. */
const TABS = ['fleet', 'models', 'threads', 'history', 'ops', 'nodes'];
const DEFAULT_TAB = 'fleet';
let activeTab = DEFAULT_TAB;

/* Der Hash, gelesen ohne Navigation: `location` kann in einer Testumgebung
   fehlen oder werfen, und ein fehlender Hash ist kein Fehler. */
function hashTab() {
  let raw = '';
  try {
    raw = (typeof location !== 'undefined' && location && location.hash)
      ? String(location.hash) : '';
  } catch (e) { raw = ''; }
  const name = raw.replace(/^#/, '').trim().toLowerCase();
  return TABS.indexOf(name) === -1 ? DEFAULT_TAB : name;
}

/* Was die Leiste und die Panels zeigen sollen — rein, damit die Testharnesse
   es messen können, ohne die Shell zu haben. */
function tabView(name) {
  const active = TABS.indexOf(name) === -1 ? DEFAULT_TAB : name;
  return TABS.map(id => ({id: id, selected: id === active, hidden: id !== active}));
}

/* Der Hash wird geschrieben, nicht der Speicher: `replaceState` legt keinen
   Verlaufseintrag an (der Zurück-Knopf soll die Seite verlassen, nicht durch
   fünf Tabs blättern) und feuert kein `hashchange`. Ohne `history` bleibt der
   Hash der zweite Weg. */
function writeHash(name) {
  try {
    if (typeof history !== 'undefined' && history &&
        typeof history.replaceState === 'function') {
      history.replaceState(null, '', '#' + name);
    } else if (typeof location !== 'undefined' && location) {
      location.hash = name;
    }
  } catch (e) { /* eine Umgebung ohne URL darf die Seite nicht anhalten */ }
}

function paintTabs() {
  const view = tabView(activeTab);
  for (const btn of shellNodes('role="tab"')) {
    const row = view.filter(v => v.id === btn.getAttribute('data-tab'))[0];
    if (!row) continue;
    btn.setAttribute('aria-selected', row.selected ? 'true' : 'false');
    btn.setAttribute('tabindex', row.selected ? '0' : '-1');
  }
  for (const panel of shellNodes('role="tabpanel"')) {
    const row = view.filter(v => v.id === panel.getAttribute('data-tab'))[0];
    if (!row) continue;
    if (row.hidden) panel.setAttribute('hidden', '');
    else panel.removeAttribute('hidden');
  }
  /* `Betrieb` ist ein zugeklapptes `<details>`: als eigener Tab wäre der Inhalt
     eine einzige Zeile. Der Tab klappt ihn auf — genau das, was der Betreiber
     vorher selbst getan hat —, und dessen `toggle`-Ereignis liest dann die
     Regeln ohne Zeile (#16). Ohne Admin-Schlüssel kostet das keinen Request. */
  if (activeTab === 'ops') {
    const ops = $('ops');
    if (ops && ops.open === false) ops.open = true;
  }
}

/* `write: false` für den Weg vom Hash in die Seite: der Hash ist dort schon
   die Wahrheit, und ein unbekannter Hash soll nicht überschrieben werden. */
function setTab(name, opts) {
  const next = TABS.indexOf(name) === -1 ? DEFAULT_TAB : name;
  const changed = next !== activeTab;
  activeTab = next;
  if ((!opts || opts.write !== false) && changed) writeHash(next);
  paintTabs();
  return activeTab;
}

/* Der Hash hat sich geändert (Reload, Zurück-Knopf, von Hand getippt). */
function applyHash() { return setTab(hashTab(), {write: false}); }

/* Ein Wechsel des Hashes ohne Reload — der Zurück-Knopf, ein Lesezeichen, eine
   getippte Adresse — muss den Tab nachziehen, sonst zeigte die Adresse etwas
   anderes als die Seite. `writeHash` schreibt über `replaceState` und feuert
   deshalb kein `hashchange`: keine Schleife. */
if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener('hashchange', applyHash);
}

document.addEventListener('click', ev => {
  const btn = ev.target && ev.target.closest ? ev.target.closest('[role="tab"]') : null;
  if (!btn) return;
  setTab(btn.getAttribute('data-tab'));
});
/* Pfeiltasten wie in jedem Tablist-Muster: sie wechseln den Tab, nicht nur
   den Fokus. Escape bleibt beim Tooltip. */
document.addEventListener('keydown', ev => {
  if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') return;
  const btn = ev.target && ev.target.closest ? ev.target.closest('[role="tab"]') : null;
  if (!btn) return;
  const i = TABS.indexOf(btn.getAttribute('data-tab'));
  if (i === -1) return;
  if (ev.preventDefault) ev.preventDefault();
  const next = TABS[(i + (ev.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
  setTab(next);
  const el = $('tab-' + next);
  if (el && typeof el.focus === 'function') el.focus();
});

const esc = s => String(s === null || s === undefined ? '—' : s)
  .replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
/* Zahlen folgen der Sprache: 1.234,5 auf Deutsch, 1,234.5 auf Englisch. Die
   Formatierer werden je (Sprache, Nachkommastellen) einmal gebaut. */
const numFmts = new Map();
const numFmt = d => {
  const k = lang + ':' + d;
  let f = numFmts.get(k);
  if (!f) {
    f = new Intl.NumberFormat(locale(), {minimumFractionDigits: d, maximumFractionDigits: d});
    numFmts.set(k, f);
  }
  return f;
};
const nf = (v, d = 0) => (v === null || v === undefined || Number.isNaN(Number(v))) ? '—'
  : withGrouping(numFmt(d).format(Number(v)));
/* CLDR gruppiert Deutsch seit 2021 mit einem schmalen Leerzeichen
   (`1 234,5`). Die Betreiber-Dokumente und die Abnahme dieses Issues schreiben
   `1.234,5`; der Punkt bleibt deshalb der Tausendertrenner. Dezimaltrenner und
   Stellenzahl kommen weiter aus Intl — nur das Trennzeichen der Tausender wird
   ersetzt, und nur im Deutschen. */
const GROUP_DOT = {de: /[\u00a0\u202f\u2009 ]/g};
const withGrouping = s => (GROUP_DOT[lang] ? s.replace(GROUP_DOT[lang], '.') : s);
/* Uhrzeiten ebenso: die Momentaufnahme und die Zeitspalte des Entscheidungs-
   logs kommen aus Intl statt aus einer festen Locale. */
const timeFmts = new Map();
const timeFmt = () => {
  let f = timeFmts.get(lang);
  if (!f) {
    f = new Intl.DateTimeFormat(locale(), {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    timeFmts.set(lang, f);
  }
  return f;
};
const clock = d => timeFmt().format(d);
/* `decisions[].ts` ist der Zeitpunkt, `decisions[].time` nur die fertige
   Zeichenkette des Servers; wo `ts` fehlt, bleibt sie stehen. */
const logTime = x => (typeof x.ts === 'number' ? clock(new Date(x.ts * 1000)) : x.time);
const gb = b => (b === null || b === undefined) ? '—' : nf(b / 1e9, 1);
const pct = v => (v === null || v === undefined) ? '—' : nf(v * 100, 1);
const pctP = v => (v === null || v === undefined) ? '—' : nf(v * 100, 1) + ' %';
/* Eine Zahl aus /config in der Sprache der Seite: ganze Zahlen ohne Nachkomma,
   sonst so viele Stellen wie sie hat (höchstens drei). Vorher stand hier der
   rohe JS-Wert — im deutschen Bild also `0.95` statt `0,95`. */
const fmtNum = v => {
  if (typeof v !== 'number' || !isFinite(v)) return v === null || v === undefined ? '—' : String(v);
  const frac = (String(v).split('.')[1] || '').length;
  return nf(v, Number.isInteger(v) ? 0 : Math.min(3, frac));
};
const signed = v => (v > 0 ? '+' : '') + nf(v * 100, 1);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
/* As measured, not rounded: the fit's intercept is the number that matters. */
const num = v => (v === null || v === undefined) ? '—' : String(v);
const sleep = ms => new Promise(r => setTimeout(r, ms));
/* ── Die Uhr der Seite (issue #34) ────────────────────────────────────────── */
/* Zwei Zahlen auf der Seite sind Fristen: die Restzeit eines Ladevorgangs und
   die eines Threads. Beide zählen lokal herunter, aber gegen die Uhr des
   Servers: `server_time` kommt mit jedem /healthz, und `offset = server_time −
   browser_now` wird bei jedem Abruf neu gesetzt. Ein Browser auf einem anderen
   Rechner mit schiefer Uhr zählt damit trotzdem richtig. */
let clockOffset = 0;                       // server_time − browser_now, in Sekunden
const browserNow = () => Date.now() / 1000;
const pageNow = () => browserNow() + clockOffset;

function anchorClock(h) {
  const server = h && h.server_time;
  if (typeof server === 'number' && isFinite(server)) clockOffset = server - browserNow();
  return clockOffset;
}

/* One place fetches `/healthz`: the 2 s tick and the wait after a restart read
   the same payload through the same call, so the page never has two ideas of
   what the service looks like (issue #25). It also re-anchors the clock (#34). */
const getHealth = () => fetch('/healthz').then(r => r.json()).then(h => {
  anchorClock(h);
  return h;
});

/* ── Fristen: eine Zahl, die herunterzählt (issue #34) ────────────────────── */
/* Jedes Element, das eine Frist zeigt, trägt `data-deadline` (absolutes Ende in
   Epoche-Sekunden) und `data-deadline-kind` (wie der Text lautet). Der 1-s-Takt
   schreibt ausschließlich `textContent` dieses Elements: kein Block wird neu
   gerendert, also kein Flackern, kein Scroll-Sprung, kein verlorener Tooltip
   und kein neuer Knoten — nach einem 2-s-Render wird die Liste neu abgefragt,
   damit der Takt die neuen Knoten sieht. */
const pad2 = n => String(n).padStart(2, '0');

/* `m:ss` über einer Minute, `n s` darunter — die Form der Abnahme. */
const countdown = sec => {
  const s = Math.max(0, Math.round(sec));
  return s >= 60 ? Math.floor(s / 60) + ':' + pad2(s % 60) : s + ' s';
};
/* Threads: über einer Minute Minuten, darunter Sekunden. */
const countdownMin = sec => {
  const s = Math.max(0, sec);
  return s >= 60 ? nf(Math.max(1, Math.round(s / 60)), 0) + ' min' : Math.ceil(s) + ' s';
};

function deadlineText(kind, remaining) {
  if (kind === 'thread') {
    /* Bei 0 ist der Thread nicht weg, sondern fällig: die TTL räumt ihn binnen
       30 s ab — die Seite sagt „frei“ statt einer Zahl, die es nicht gibt. */
    return remaining > 0 ? t('threads.remaining', {t: countdownMin(remaining)})
                         : t('threads.free');
  }
  /* Ein Ladevorgang endet durch oMLX' eigenes Signal (docs/loading-state.md),
     nicht durch unsere Arithmetik: bei 0 steht hier „überzogen“, nie „fertig“
     und nie eine negative Zahl. */
  return remaining > 0 ? t('load.remaining', {t: countdown(remaining)})
                       : t('load.overdue', {t: countdown(-remaining)});
}

const deadlineNodes = () => (typeof document.querySelectorAll === 'function'
  ? Array.from(document.querySelectorAll('[data-deadline]')) : []);

function paintDeadlines() {
  const now = pageNow();
  for (const el of deadlineNodes()) {
    const at = Number(el.getAttribute('data-deadline'));
    if (!isFinite(at)) continue;
    const text = deadlineText(el.getAttribute('data-deadline-kind'), at - now);
    if (el.textContent !== text) el.textContent = text;
  }
}

let deadlineTimer = null;
function startDeadlines() {
  if (deadlineTimer !== null) return;
  deadlineTimer = setInterval(paintDeadlines, 1000);
}

/* ── Quellen: was ein Tooltip zitiert ─────────────────────────────────────── */
/* Nur die Quelle bleibt hier stehen — Feldpfade und Endpunkte werden nicht
   übersetzt, sie sind Namen, keine Sätze. Beschriftung und Erklärung kommen
   aus dem Katalog unter `tip.<key>` (`en.json` ist die Quelle, `de.json` die
   Übersetzung). */
const SRC = {
  state:        '/healthz → hosts[].state',
  address:      '/healthz → hosts[].address, addresses[]',
  addr_sw:      '/healthz → hosts[].addr_switches',
  version:      '/healthz → hosts[].version (oMLX /api/cluster/node_id)',
  guard:        '/healthz → hosts[].guard_enabled',
  used:         '/healthz → hosts[].used_bytes_measured (sum of models[].hosts[].actual_size, oMLX /v1/models/status)',
  used_est:     '/healthz → hosts[].used_bytes (oMLX /health → engine_pool.current_model_memory)',
  ceiling:      '/healthz → hosts[].ceiling_bytes (oMLX /health)',
  ratio:        'computed: used_bytes_measured / ceiling_bytes',
  headroom:     'computed: ceiling_bytes − used_bytes_measured',
  hard:         '/healthz → hosts[].hard_threshold × ceiling_bytes',
  inflight:     '/healthz → hosts[].inflight',
  max_inflight: '/healthz → hosts[].max_inflight',
  spill:        '/healthz → hosts[].spill_threshold',
  slots:        '/healthz → hosts[].thread_slots',
  max_threads:  '/healthz → hosts[].max_threads',
  target_share: '/healthz → hosts[].target_share',
  relative_load: '/healthz → hosts[].relative_load',
  rtt:          '/healthz → hosts[].rtt_ms',
  tps:          '/healthz → hosts[].metrics[<model>].tg_tps, /config → hosts[].tg_tps',
  samples:      '/healthz → hosts[].metrics[<model>].tg_samples',
  measured:     '/healthz → hosts[].metrics[<model>].measured_at',
  cache:        '/healthz → hosts[].usage.last (oMLX usage.cached_tokens, prompt_tokens, prompt_tokens_per_second, generation_tokens_per_second)',
  requests:     '/healthz → hosts[].requests_total',
  errors:       '/healthz → hosts[].errors_total',
  models_avail: '/healthz → hosts[].models_available',
  loaded:       '/healthz → models[].hosts[].loaded (oMLX /v1/models/status)',
  size_est:     '/healthz → models[].hosts[].estimated_size (oMLX /v1/models/status)',
  size_act:     '/healthz → models[].hosts[].actual_size (oMLX /v1/models/status)',
  ctx:          '/healthz → models[].hosts[].context_len (oMLX /v1/models)',
  running:      '/healthz → models[].hosts[].inflight',
  threads_m:    '/healthz → models[].hosts[].threads',
  tps_m:        '/healthz → models[].hosts[].tg_tps',
  tps_cap:      '/config → hosts[].tg_tps (AS-6 calibration, a property of the machine)',
  share_t:      '/healthz → threads.target_share',
  share_a:      '/healthz → threads.actual_share',
  delta:        'computed from threads.actual_share − target_share',
  slots_total:  '/healthz → threads.registry, hosts[].max_threads',
  k_req:        '/stats → requests_total',
  k_ok:         '/stats → requests_ok',
  k_err:        '/stats → errors_total',
  k_5xx:        '/stats → http_503_total, http_507_total',
  k_hits:       '/stats → thread_hits',
  k_new:        '/stats → threads_new, threads_evicted',
  k_rebind:     '/stats → threads_rebound',
  k_uptime:     '/healthz → uptime_seconds',
  d_time:       '/stats → decisions[].ts',
  d_model:      '/stats → decisions[].model',
  d_host:       '/stats → decisions[].host',
  d_reason:     '/stats → decisions[].reason',
  d_prompt:     '/stats → decisions[].prompt_chars',
  d_dur:        '/stats → decisions[].duration_ms',
  d_status:     '/stats → decisions[].status',
  d_bar:        'computed from decisions[].duration_ms',
  taken:        'pull of /healthz + /stats, every 2 s',
  d_count:      '/stats → decisions[]',
  d_med:        'computed from /stats → decisions[].duration_ms',
  th_turns:     '/healthz → threads.registry[].turns',
  th_key:       '/healthz → threads.registry[].key_name',
  th_idle:      '/healthz → threads.registry[].idle_seconds',
  th_ttl:       '/healthz → threads.registry[].ttl_seconds',
  loading:      '/healthz → hosts[].loading (model, size_gb, eta_seconds, source)',
  /* Die zwei Fristen (issue #34) zählen lokal herunter: der absolute Endpunkt
     steht als `data-deadline` am Element, die Uhr kommt aus /healthz →
     server_time. */
  load_remaining: '/healthz → hosts[].loading (started_at + eta_seconds, server_time)',
  th_remaining: '/healthz → threads.registry[].ttl_seconds − idle_seconds (server_time)',
  th_delete:    'DELETE /admin/threads/{key} (admin key)',
  loaded_routable: '/healthz → hosts[].models_loaded, models[].hosts[].dispatch',
  not_loaded:   '/healthz → models[].hosts[].loaded',
  maint:        '/healthz → hosts[].maintenance (POST/DELETE /admin/nodes/{name}/maintenance)',
  maint_preset: 'POST /admin/nodes/{name}/maintenance?minutes=',
  maint_until:  '/healthz → hosts[].maintenance.until',
  maint_left:   '/healthz → hosts[].maintenance.minutes_left',
  maint_by:     '/healthz → hosts[].maintenance.by',
  maint_drain:  '/healthz → hosts[].inflight',
  maint_done:   '/healthz → hosts[].inflight',
  policy:       '/healthz → models[].hosts[].policy, dispatch (POST/DELETE /admin/nodes/{name}/models/{model})',
  policy_orphans: 'GET /admin/nodes/{name}/models → models[].offered: false, level (POST/DELETE /admin/nodes/{name}/models/{model})',
  lock:         '/healthz → models[].lock, models[].hosts[].lock',
  restart:      '/healthz → restart (GET/POST /admin/restart)',
  restart_supervised: '/healthz → restart.supervised, restart.supervisor',
  restart_inflight: '/healthz → restart.inflight (sum of hosts[].inflight), restart.hosts',
  restart_delay: '/healthz → restart.delay_seconds',
  /* 2.1 Auslastung je Host (issue #33). Die Zustände sind gemessen: ein Aufruf
     von /admin/api/activity je Knoten und Probe-Takt, jeder Takt genau ein
     Zustand. Die Arbeit kommt aus dem `usage` der Antwort, `client` aus RIPPLEs
     eigener Uhr. */
  load_idle:    '/healthz → load.hosts[].idle (oMLX /admin/api/activity, one tick every 10 s)',
  load_loading: '/healthz → load.hosts[].loading (oMLX /admin/api/activity → is_loading)',
  load_prefill: '/healthz → load.hosts[].prefill (oMLX /admin/api/activity → prefilling)',
  load_decode:  '/healthz → load.hosts[].decode (oMLX /admin/api/activity → generating)',
  load_mixed:   '/healthz → load.hosts[].mixed (prefilling and generating in the same tick)',
  load_requests: '/stats → host_stats.hosts[].requests (counted by RIPPLE)',
  load_prefill_s: '/healthz → load.hosts[].prefill_seconds (oMLX usage.prompt_eval_duration, summed)',
  load_decode_s: '/healthz → load.hosts[].generation_seconds (oMLX usage.generation_duration, summed)',
  load_prompt_tokens: '/healthz → load.hosts[].prompt_tokens (oMLX usage.prompt_tokens, summed)',
  load_completion_tokens: '/healthz → load.hosts[].completion_tokens (oMLX usage.completion_tokens, summed)',
  load_since:   '/healthz → load.since, load.window_seconds',
  load_lead:    '/healthz → load.hosts[].{idle,loading,prefill,decode,mixed} / sampled_seconds',
  load_hide_idle: '/healthz → load.hosts[].sampled_seconds, .idle',
  load_seg:     '/healthz → load.hosts[].{idle,loading,prefill,decode,mixed} / sampled_seconds',
  load_reset:   'POST /admin/stats/reset (admin key)',
  load_no_usage: '/healthz → load.total.requests_without_usage',
  load_cross:   "GET /admin/api/usage?range=today per node → totals (the node's own SQLite)",
  /* 7 Knoten (issue #71). Jede Zahl der Vorschau kommt aus der Antwort des
     Trockenlaufs, der dieselben zwei Aufrufe macht wie das Hinzufügen —
     deshalb steht dort der Endpunkt und nicht oMLX: was die Seite zeigt, ist
     genau das, was der Schreibvorgang danach liest. */
  node_id:      'POST /admin/nodes?dry_run=true → node_id (oMLX /api/cluster/node_id)',
  node_models:  'POST /admin/nodes?dry_run=true → models[] (oMLX /v1/models, /v1/models/status)',
  node_memory:  'POST /admin/nodes?dry_run=true → used_bytes, ceiling_bytes (oMLX /health → engine_pool)',
  node_rtt:     'POST /admin/nodes?dry_run=true → rtt_ms (measured round trip of the /health probe)',
  node_site:    'POST /admin/nodes?dry_run=true → site_derived (config.yaml sites[].networks)',
  node_source:  '/healthz → hosts[].source (config.yaml or the runtime overlay)',
  node_inflight: '/healthz → hosts[].inflight',
  node_drain:   '/healthz → fleet.drain_seconds (config.yaml fleet.drain_seconds)',
  node_scan:    'POST /admin/discover → found[] (ripple discover --scan over the CIDR)',
};

/* Ein Tooltip ist Beschriftung, Erklärung und Quelle — ein Satz. */
const tipText = key => {
  if (!SRC[key]) throw new Error('no source text for ' + key);
  return t('tip.' + key) + '  ' + t('common.source') + ': ' + SRC[key];
};

/* Tooltip: Definition + Quelle. `flip` öffnet nach links, wenn rechts kein Platz ist. */
function T(key, text, flip) {
  return '<span class="tip' + (flip ? ' flip' : '') + '" data-tip="' + esc(tipText(key)) + '">' + text + '</span>';
}
/* Mehrere Kennzahlen in einer Zelle: eine Erklärung, die alle nennt. */
const tipBody = keys => keys.map(tipText).join('\n');
/* `more` ist eine Zeile, die nicht aus der Quellentabelle kommt, sondern aus dem
   Datensatz — die Probenzahl der tok/s-Messung (issue #80). Sie steht im Tip und
   nicht in der Zelle, weil die Zelle in einer Viertel-Karte für *eine* Angabe
   Platz hat. */
const cell = (keys, inner, flip, more, cls) =>
  '<div class="metric tip cell' + (flip ? ' flip' : '') + (cls ? ' ' + cls : '') +
  '" data-tip="' +
  esc(more ? tipBody(keys) + '\n' + more : tipBody(keys)) + '">' + inner + '</div>';
/* Die Phase (issue #18): was der Knoten gerade tut. `pp` steht zwischen der
   Auslieferung und dem ersten Token (Prefill — es kommt noch nichts), `tg`
   strömt Token. Die Phase kommt aus RIPPLEs eigenem Buch über die laufenden
   Anfragen, nicht aus einer zusätzlichen Abfrage an oMLX: der Punkt ist eine
   Aussage über *jetzt*, nicht über den letzten Probe-Intervall. */
/* Phase und Quelle in einem Satz — dieselbe Regel wie bei jeder Kennzahl:
   die Farbe verstärkt die Aussage, sie trägt sie nicht allein. */
const phaseTip = (ph, where) => t('phase.' + ph) + '  ' + t('common.source') + ': /healthz → ' + where;
const chip = (s, phase, tip) => '<span class="state" data-state="' + esc(s) + '"' +
  (phase ? ' data-phase="' + esc(phase) + '"' : '') +
  (tip ? ' data-tip="' + esc(tip) + '"' : '') +
  '><i class="glyph"></i>' + esc(s) + '</span>';

/* ── Tooltip: ein Layer am <body> ─────────────────────────────────────────── */
/* Warum nicht `::after` am Element (was die Seite bis 15.11.2026 tat):
   (a) jeder `overflow:hidden`-Vorfahre schneidet ihn ab — Modelltabelle und
       Entscheidungslog sind genau das;
   (b) er hängt am Hover-Zustand eines Knotens, den der 2-s-Patch ersetzen kann;
   (c) `attr()` in `content` verhält sich nicht in jedem Browser gleich.
   Der Text kommt aus `data-tip` — demselben Attribut, das die Tests prüfen. */
const TIP_GAP = 7;
let tipEl = null, tipOwner = null;

function tipLayer() {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.id = 'tip-layer';
    tipEl.setAttribute('role', 'tooltip');
    document.body.appendChild(tipEl);
  }
  return tipEl;
}

/* Wann der Kasten zuletzt aufging — der Klick-Handler unterscheidet damit
   „Finger hoch" von „zweiter Tipp" (#139). */
let tipShownAt = 0;

function tipHide() {
  tipOwner = null;
  if (tipEl) { tipEl.classList.remove('on'); tipEl.textContent = ''; }
}

function tipPlace(owner, el) {
  const r = owner.getBoundingClientRect();
  const w = el.offsetWidth, h = el.offsetHeight;
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  let x = r.left;
  let y = r.bottom + TIP_GAP;
  if (y + h > vh - 6) y = r.top - TIP_GAP - h;      // kein Platz unten: darüber
  if (x + w > vw - 6) x = r.right - w;              // kein Platz rechts: linksbündig
  el.style.left = Math.round(clamp(x, 6, Math.max(6, vw - w - 6))) + 'px';
  el.style.top = Math.round(clamp(y, 6, Math.max(6, vh - h - 6))) + 'px';
}

function tipShow(owner) {
  const text = owner.getAttribute('data-tip');
  if (!text) return;
  const el = tipLayer();
  el.textContent = text;            // textContent: der Text ist Daten, kein Markup
  el.classList.add('on');
  tipOwner = owner;
  tipShownAt = Date.now();          // siehe den click-Handler: Tipp-Ende erkennen
  tipPlace(owner, el);
}

const tipTarget = ev => (ev.target instanceof Element ? ev.target.closest('[data-tip]') : null);

document.addEventListener('pointerover', ev => {
  const owner = tipTarget(ev);
  if (!owner) { if (tipOwner) tipHide(); return; }
  if (owner !== tipOwner) tipShow(owner);
});
document.addEventListener('pointerout', ev => {
  if (!tipOwner) return;
  const to = ev.relatedTarget;
  if (to instanceof Node && tipOwner.contains(to)) return;
  tipHide();
});
document.addEventListener('focusin', ev => {
  const owner = tipTarget(ev);
  if (owner) tipShow(owner); else tipHide();
});
document.addEventListener('focusout', tipHide);
/* Seit dem 21.11.2026 öffnet auch ein Klick den Kasten (#139). Am Handy gibt es
   kein Hover: `pointerover` feuert beim Antippen zwar, aber dieser Handler hat
   den Kasten im nächsten Moment wieder geschlossen — kein Tooltip war dort
   erreichbar, und ein Icon ohne Tooltip ist stumm. Jetzt zeigt der Klick, statt
   zu verstecken; zwei Ausnahmen: innerhalb von 400 ms nach einem Zeiger-Show
   (das ist das Ende des Tippens, kein zweiter Wunsch) bleibt er stehen, und ein
   zweiter Klick auf denselben Besitzer schließt ihn wie bisher. Ein Klick
   daneben schließt immer. */
document.addEventListener('click', ev => {
  const owner = tipTarget(ev);
  if (!owner) { tipHide(); return; }
  if (tipOwner === owner && Date.now() - tipShownAt < 400) return;
  if (tipOwner === owner) { tipHide(); return; }
  tipShow(owner);
}, true);
document.addEventListener('keydown', ev => { if (ev.key === 'Escape') tipHide(); });
/* Der Kasten ist viewport-fixiert: Scrollen und Größenänderung machen seine
   Position falsch, also verschwindet er statt zu wandern. */
window.addEventListener('scroll', tipHide, true);
window.addEventListener('resize', tipHide);

/* ── Admin-Schlüssel ──────────────────────────────────────────────────────── */
/* Same rule as /keys: sessionStorage for this tab, Bearer on each call, never
   in the URL, never rendered into the page. Calibrate uses it, and the
   maintenance toggle (#10) is the second caller — both go through `adminApi()`.

   `key` is the one exception (#130): the admin chip verifies a key the operator
   just typed, and that key is not in the storage yet — it is only put there
   after the answer. Passing it in keeps the header in one place instead of a
   second `fetch()` that builds its own. */
/* Der Name im `sessionStorage` für die zwei **neuen** Zugriffe (#130): der Chip
   schreibt den Schlüssel nach der Prüfung und löscht ihn beim Abmelden. Das
   Lesen steht weiter als Literal in `adminKey()` — es ist die Zeile, die
   `tests/test_dashboard_loading.py` als Vertrag liest (`getItem('ripple_admin')`),
   und ein zweiter Name für denselben Speicher wäre der Fehler, den die
   Konstante hier verhindert. */
const ADMIN_STORE = 'ripple_admin';
const adminKey = () => sessionStorage.getItem('ripple_admin') || '';
const keyHelp = () => t('key.help');

async function adminApi(method, path, body, given) {
  const key = given === undefined ? adminKey() : given;
  if (!key) throw new Error(keyHelp());
  const init = {
    method,
    headers: {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
  };
  /* Only the replication POST carries a body so far; `undefined` keeps the
     other callers byte-identical to before. */
  if (body !== undefined) init.body = JSON.stringify(body);
  const r = await fetch(path, init);
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) { data = {error: {message: text || ('HTTP ' + r.status)}}; }
  if (!r.ok) {
    const err = new Error((data.error && data.error.message) || ('HTTP ' + r.status));
    err.status = r.status;
    /* The body travels with the error: the restart's 409 carries `inflight` and
       `hosts` beside the message, and the second confirmation names both
       instead of parsing the sentence apart (issue #25). */
    err.data = data;
    throw err;
  }
  return data;
}

/* ── Admin-Modus im Masthead (issue #130) ─────────────────────────────────── */
/* Bis hierher war der Admin-Zustand eine unsichtbare Tatsache: der Schlüssel
   lag in `sessionStorage['ripple_admin']`, und nur die Betreiberblöcke wussten
   davon. Der Chip im Kopf macht ihn sichtbar — und zwar erst, nachdem der
   Server ihn bestätigt hat. Die Reihenfolge ist der Punkt: `checking` →
   `GET /admin/models` → `200` → **erst jetzt** in den Speicher und auf `admin`.
   Ein Schlüssel, den die Seite nur deshalb behauptet, weil ein String in einem
   Storage steht, wäre eine Anzeige ohne Messung.

   Drei Zustände, drei Wörter aus dem Katalog (`admin.readonly`,
   `admin.checking`, `admin.admin`); das Skript setzt beim Wechsel `data-i18n`
   mit, damit `fillShell()` nach einem Sprachwechsel dasselbe Wort schreibt.
   Der Schlüssel selbst wird nie gerendert: er geht als `Authorization: Bearer`
   hinaus, steht in keiner URL und in keinem Log. */
const ADMIN_WORDS = {readonly: 'admin.readonly', checking: 'admin.checking',
                     admin: 'admin.admin'};
let adminState = 'readonly';       // readonly | checking | admin

/* `hidden` als Attribut, nicht als Eigenschaft: der Patch und die Tests lesen
   das Attribut, und ein blosses `el.hidden = false` liesse es stehen. */
function adminHide(el, on) {
  if (!el) return;
  if (on) el.setAttribute('hidden', ''); else el.removeAttribute('hidden');
}

/* Offen heisst: kein `hidden`-Attribut. Über `getAttribute`, nicht über
   `hasAttribute` — die zwei Messharnesse der Seite bauen verschiedene kleine
   DOMs, und nur `getAttribute` gibt es in beiden. */
const adminIsOpen = el => !!el && el.getAttribute('hidden') === null;

function adminPaint() {
  const chip = $('adminChip');
  const word = $('adminWord');
  const ask = $('adminAsk');
  const id = ADMIN_WORDS[adminState] || ADMIN_WORDS.readonly;
  if (word) {
    word.setAttribute('data-i18n', id);
    word.textContent = t(id);
  }
  if (chip) {
    chip.setAttribute('data-state', adminState);
    /* Das Label ist das Wort des Zustands; der Tooltip sagt, was er bedeutet,
       und nennt die Quelle des Schlüssels (`sessionStorage`, nur dieser Tab). */
    chip.setAttribute('data-i18n-aria', id);
    chip.setAttribute('aria-label', t(id));
    chip.setAttribute('data-tip',
                      t(adminState === 'admin' ? 'admin.tip_admin' : 'admin.tip'));
    chip.setAttribute('aria-expanded', adminIsOpen(ask) ? 'true' : 'false');
  }
  /* Abmelden gibt es nur, wenn es etwas abzumelden gibt. */
  adminHide($('adminOut'), adminState !== 'admin');
}

function adminSay(text) {
  const msg = $('adminMsg');
  if (msg) msg.textContent = text || '';
}

/* Das Feld öffnen und schliessen. Schliessen räumt es leer: ein getippter
   Schlüssel, der nicht abgeschickt wurde, bleibt nicht im DOM stehen. */
function adminOpen(on) {
  const ask = $('adminAsk');
  const field = $('adminKey');
  adminHide(ask, !on);
  if (field) {
    field.value = '';
    if (on && typeof field.focus === 'function') field.focus();
  }
  if (on) adminSay('');
  adminPaint();
  return !!on;
}

function adminToggle() {
  if (adminState === 'checking') return false;
  return adminOpen(!adminIsOpen($('adminAsk')));
}

/* Die keyed-Blöcke neu zeichnen, ohne Reload: derselbe Weg wie ein Tick, also
   dieselbe DOM-Form. Der nächste 2-s-Takt bestätigt sie. */
function adminRepaint() {
  if (last) render(last.h, last.s, last.c);
}

/* `GET /admin/models` ist die billigste Frage, die nur ein gültiger Schlüssel
   mit 200 beantwortet: ein Modell-Lock ist lesbar, ohne etwas zu schreiben.
   Sie geht über `adminApi()` — derselbe Header, dieselbe Fehlerform (`status`,
   `message` aus dem Rumpf) wie jeder andere Admin-Aufruf der Seite. */
async function adminSubmit() {
  const field = $('adminKey');
  const key = String((field && field.value) || '').trim();
  if (!key) return false;
  adminState = 'checking';
  adminPaint();
  adminSay('');
  try {
    await adminApi('GET', '/admin/models', undefined, key);
  } catch (e) {
    /* Kein 200, also kein Admin: der Chip bleibt, was er vorher war. Ein 401
       ist die Ablehnung des Schlüssels (`admin.rejected`), alles andere die
       Meldung des Servers, wörtlich. Das Feld bleibt offen und sagt es. */
    adminState = adminKey() ? 'admin' : 'readonly';
    adminPaint();
    adminSay(e && e.status === 401 ? t('admin.rejected')
                                  : (e && e.message ? e.message : String(e)));
    return false;
  }
  /* Erst nach dem 200 in den Speicher: ein falscher Schlüssel landet hier nie. */
  try { sessionStorage.setItem(ADMIN_STORE, key); } catch (e) { /* privater Modus */ }
  adminState = 'admin';
  adminOpen(false);
  adminPaint();
  adminRepaint();
  return true;
}

function adminSignOut() {
  try { sessionStorage.removeItem(ADMIN_STORE); } catch (e) { /* privater Modus */ }
  adminState = 'readonly';
  adminOpen(false);
  adminPaint();
  /* Die Betreiberblöcke lesen den Schlüssel beim Zeichnen — ein Repaint macht
     sie wieder inert, ohne dass ein Tick abgewartet werden muss. */
  adminRepaint();
  return adminState;
}

/* Das Ergebnis der Prüfung, die beim Start ohnehin läuft: `loadFeasibility()`
   liest `GET /admin/models` einmal mit dem Schlüssel aus `/keys` (die
   Machbarkeit der Schlösser, #126). Genau diese Antwort sagt auch, ob der Tab
   Admin ist — deshalb ein Request und nicht zwei. Ohne 200 wird der Schlüssel
   gelöscht: Anzeige und Panels sollen nie auseinanderlaufen, und ein Schlüssel,
   der nicht gilt, hat in diesem Tab nichts zu suchen. */
function adminBooted(ok) {
  if (ok) {
    adminState = 'admin';
    adminPaint();
    return adminState;
  }
  try { sessionStorage.removeItem(ADMIN_STORE); } catch (e) { /* privater Modus */ }
  adminState = 'readonly';
  adminPaint();
  return adminState;
}

document.addEventListener('click', ev => {
  const el = ev.target;
  if (!el || !el.closest) return;
  if (el.closest('#adminChip')) { adminToggle(); return; }
  if (el.closest('#adminOut')) adminSignOut();
});
document.addEventListener('keydown', ev => {
  const el = ev.target;
  if (!el || el.id !== 'adminKey') return;
  if (ev.key === 'Enter') { ev.preventDefault(); adminSubmit(); }
  else if (ev.key === 'Escape') { ev.preventDefault(); adminOpen(false); }
});

/* ── Legende: das Zustandssystem, einmal ausgeschrieben ───────────────────── */
/* Der Zustand selbst ist ein Token (`healthy`, `loading`, …) und bleibt
   unübersetzt; der Satz daneben kommt aus `state.<token>`. Die Reihenfolge ist
   die der Formen, nicht die des Vorfalls: die beiden Betreiber-Zustände stehen
   neben ihrer Gruppe (#77) — `maintenance`/`draining` (offenes Quadrat, Tropfen)
   und `down`/`disabled` (Kreuz, hohles Dreieck) lesen in einer Zeile zusammen.
   Seit #79 hat jeder der sieben seine eigene Form; die Gruppe ist eine
   Verwandtschaft der Bedeutung, nicht mehr eine geteilte Silhouette. */
const LEGEND = ['healthy', 'loading', 'degraded', 'maintenance',
                /* The two operator states of issue #70: a node that is leaving
                   (draining) and one that is out for good (disabled). */
                'draining', 'down', 'disabled'];
function renderLegend() {
  return LEGEND.map(s => '<div class="legend-item">' + chip(s) +
      '<div class="legend-def">' + esc(t('state.' + s)) + '</div></div>').join('') +
    '<div class="legend-note">' + esc(t('legend.note')) + '</div>';
}

/* ── Banner: was die Seite erklärt, bevor man sie liest ───────────────────── */
function renderBanners(h) {
  const caps = h.capabilities || {};
  const out = [];
  if (!caps.eviction_guard) out.push(t('banner.no_guard'));
  if (!caps.identity) out.push(t('banner.no_identity'));
  for (const w of (h.warnings || [])) out.push(w);
  return out.map(b => '<div class="banner">' + esc(b) + '</div>').join('');
}

/* The calibrated capability per node (#30): `tg_tps` from config.yaml, the
   value `ripple/placement.py` decides with — a property of the machine, not of
   the moment. It comes from /config, which the 2 s tick fetches anyway, and is
   kept here instead of travelling in every /healthz row: the number changes
   only with the configuration.
   Since #124 it lives **only in the tooltip** of the tok/s cell, named as a
   property of the machine: inside the cell it read as a measurement of the
   model. */
let capability = new Map();          // host name → calibrated tok/s, or undefined

function rememberCapability(c) {
  capability = new Map(((c && c.hosts) || []).map(x => [x.name, x.tg_tps]));
}

/* Die Rate des Modells mit den meisten Messproben auf diesem Knoten. */
function hostTps(h) {
  const m = h.metrics || {};
  let best = null;
  for (const id of Object.keys(m)) {
    const r = m[id] || {};
    if (r.tg_tps === null || r.tg_tps === undefined) continue;
    if (!best || (r.tg_samples || 0) > (best.tg_samples || 0)) best = Object.assign({model: id}, r);
  }
  return best;
}

/* ── Der Fahrtenschreiber je Knoten (issue #83) ───────────────────────────── */
/* Zwei Linien über das Fenster, das `/healthz` schon liefert: `hosts[].series`
   ist je Modell ein Eintrag, älteste Probe zuerst, `null` für einen Bucket ohne
   Probe. Durchgezogen ist `total` (tok/s über alle Ströme des Knotens),
   gestrichelt `per_thread` (dieselbe Rate durch die Zahl der Ströme). Beide
   kommen fertig aus dem Backend; `total = n · rate` steht in `ripple/stats.py`
   und wird hier nicht ein zweites Mal gerechnet — gezeichnet wird, was der
   Vertrag schickt.

   Warum `<svg>` und nicht `<canvas>`: die Karte ist eine Zeichenkette, und ein
   SVG ist Teil dieser Zeichenkette. Es braucht keinen zweiten Zeichenschritt
   nach dem Patch (kein `getContext`, keine Breitenmessung, kein DPR, kein
   ResizeObserver): `width:100%` und `preserveAspectRatio="none"` folgen der
   Karte von selbst, `vector-effect` hält Strichbreite und Strichmuster in
   Pixeln, damit die gestrichelte Linie auch in einer breiten Karte gestrichelt
   aussieht. */
const SPARK_W = 1000;   /* Einheiten der Zeitachse — die Karte skaliert sie */
const SPARK_H = 34;     /* px, fest: der Flotten-Tab wächst nicht (CSS: 34px) */
const SPARK_PAD = 3;    /* Luft oben und unten für Strich und Spitzen */

const isNum = v => typeof v === 'number' && isFinite(v);

/* Ein Bucket in Einheiten der Zeitachse. Dieselbe Rechnung wie im Pfad, damit
   Fadenkreuz und Linie auf demselben x stehen. */
const sparkX = (i, span) => Math.round((i / Math.max(1, span)) * SPARK_W * 10) / 10;

/* Der Eintrag, den die Karte zeigt: das Modell, dessen Rate in der tok/s-Zelle
   steht. Ohne Messung (`metrics` leer) der erste Eintrag — dann ist die Kurve
   die einzige Auskunft, die es über den Knoten gibt. */
function hostSeries(x) {
  const list = x && Array.isArray(x.series) ? x.series : [];
  if (!list.length) return null;
  const tps = hostTps(x);
  const want = tps ? tps.model : null;
  return list.filter(s => s && s.model === want)[0] || list[0];
}

/* Die zwei Reihen eines Eintrags, auf eine Länge gebracht. Keine Arithmetik:
   `total` und `per_thread` sind, was der Vertrag liefert; ein fehlender oder
   unbrauchbarer Wert wird `null` — und `null` heißt „hier ist keine Probe“. */
function seriesRows(entry) {
  const e = entry || {};
  const total = Array.isArray(e.total) ? e.total : [];
  const per = Array.isArray(e.per_thread) ? e.per_thread : [];
  const len = Math.max(total.length, per.length);
  const at = (arr, i) => (isNum(arr[i]) ? arr[i] : null);
  return {
    total: Array.from({length: len}, (_, i) => at(total, i)),
    per_thread: Array.from({length: len}, (_, i) => at(per, i)),
  };
}

/* One row as a path. A `null` is not a sample — but not every `null` is a gap:
   the probe runs every 10 s (`discovery.interval_seconds`) and a bucket is 5 s
   wide (`stats.BUCKET_SECONDS`), so between two probes there is **always**
   exactly one empty bucket. The pen does not lift there (issue #96) — otherwise
   the path is all `M` and no `L`, and SVG paints nothing for a subpath of one
   point: measured, six `M`, zero `L`, a curve that had been invisible for days.

   One empty bucket is the cadence, not a measurement that went missing — and
   `SPARK_HOLD = 1` sat exactly on that boundary, with no room for the probe's
   own jitter. Measured on alpha, DeepSeek series, 180 buckets on 21.11.:
   88 gaps of length 1 and **one gap of length 2**, from a probe that landed a
   few seconds late and nothing more. At a hold of 1 that single gap tore the
   line: `moves: 2`, seam at x 648 → 664.8, `dy 0` — the line continued at the
   same height, so the break carried no information about the traffic.

   So the hold is 10 s probe / 5 s bucket = 2: the one empty bucket the cadence
   puts there, plus the jitter of one probe. A run of 3 or more empty buckets is
   ≥ 15 s without a probe — a tick that did not happen, a node that was away —
   and there the line still ends, so an absent node cannot look like a busy one.
   Nothing is interpolated and no slot is moved: `x(i)` stays the bucket the
   sample fell into. */
const SPARK_HOLD = 2;

function seriesPath(values, x, y) {
  let d = '', down = false, skipped = 0;
  for (let i = 0; i < values.length; i++) {
    if (!isNum(values[i])) {
      if (down) { skipped++; if (skipped > SPARK_HOLD) down = false; }
      continue;
    }
    d += (down ? 'L' : 'M') + x(i) + ' ' + y(values[i]);
    down = true;
    skipped = 0;
  }
  return d;
}

/* The value the line shows at a bucket — the same rule as the path, and the
   same `hold`. Without that one source the drawing and the number drifted apart
   at the next tuning (#105): `seriesPath()` bridged an empty bucket, the readout
   answered `—` for exactly that bucket — the picture said "there is a line
   here", the number said "there is nothing here".

   Only what the line really crosses is bridged: a run of at most `hold` empty
   buckets **between** two samples. The value is then the sample before it,
   because that is the sample the segment crossing the bucket is drawn from —
   not interpolated, no second computation. `held` tells the reader that the
   number was not measured at this x position. A longer run is a probe that did
   not happen: there the line tears, so `—`. At the edge of the row (before the
   first or after the last sample) no segment is drawn — no number there either,
   instead of inventing one. */
function seriesValueAt(rows, index, hold) {
  const list = Array.isArray(rows) ? rows : [];
  const i = Number(index);
  if (!isFinite(i) || i < 0 || i !== Math.floor(i) || i >= list.length) {
    return {value: null, held: false};
  }
  if (isNum(list[i])) return {value: list[i], held: false};
  const bridge = isNum(hold) && hold >= 0 ? Math.floor(hold) : SPARK_HOLD;
  /* Der ganze Lauf um diesen Bucket, nicht nur die Nachbarn: erst seine Länge
     entscheidet, ob der Stift oben bleibt oder absetzt. */
  let a = i, b = i;
  while (a > 0 && !isNum(list[a - 1])) a--;
  while (b < list.length - 1 && !isNum(list[b + 1])) b++;
  /* Dieselbe Schwelle wie im Pfad: `skipped > SPARK_HOLD` hebt den Stift. */
  if (b - a + 1 > bridge) return {value: null, held: false};
  if (a === 0 || b === list.length - 1) return {value: null, held: false};
  return {value: list[a - 1], held: true};
}

/* Ein Punkt, der allein steht, ist ein Punkt und keine Linie (issue #96): ein
   Knoten mit genau einer Probe sah bisher aus wie ein Knoten ohne jede Probe.
   Der Marker ist ein runder Punkt in **Bildschirm**-Pixeln: `h0.01` ist ein
   Segment von praktisch null Länge, und mit `stroke-linecap:round` plus
   `vector-effect:non-scaling-stroke` malt der Browser daraus eine Scheibe von
   der Breite der Strichstärke — in jeder Kartenbreite rund (gemessen: 5×6 px
   bei 675 px Karte, 6×6 px bei 300 px). Ein `<circle>` wäre unter
   `preserveAspectRatio="none"` eine Ellipse, deren Breite mit der Karte
   schrumpft (bei 300 px ein Haarstrich), und ein Segment der Länge **null**
   malt gar nichts — genau das war der Defekt. */
function seriesDots(values, x, y) {
  let d = '', run = 0, start = 0, skipped = 0;
  /* A run ends by the same rule as the path: one empty bucket is crossed, more
     than `SPARK_HOLD` of them lift the pen. A point the line reaches is no lone
     point — it needs no marker. */
  const close = () => {
    if (run === 1) d += 'M' + x(start) + ' ' + y(values[start]) + 'h0.01';
    run = 0;
  };
  for (let i = 0; i < values.length; i++) {
    if (!isNum(values[i])) {
      if (run) { skipped++; if (skipped > SPARK_HOLD) close(); }
      continue;
    }
    if (!run) start = i;
    skipped = 0;                 /* jede Probe setzt den Zähler zurück */
    run++;
  }
  close();
  return d;
}

/* Geometrie: eine gemeinsame lineare Skala von 0 bis zum höchsten Wert des
   Fensters (beide Reihen), y nach oben. `max` geht mit zurück, damit ein Test
   die gezeichneten Punkte gegen die Zahlen des Vertrags rechnen kann. */
function seriesGeom(entry) {
  const rows = seriesRows(entry);
  const nums = rows.total.concat(rows.per_thread).filter(isNum);
  const max = nums.length ? Math.max.apply(null, nums) : 0;
  const span = Math.max(1, rows.total.length - 1);
  const base = SPARK_H - SPARK_PAD;
  /* Auf zwei Nachkommastellen: ein `d` mit 17 Stellen ist keine Genauigkeit,
     sondern Ballast im 2-s-Patch. */
  const scale = v => Math.round((max > 0 ? base - (v / max) * (base - SPARK_PAD) : base) * 100) / 100;
  const x = i => sparkX(i, span);
  return {
    total: seriesPath(rows.total, x, scale),
    per_thread: seriesPath(rows.per_thread, x, scale),
    dots: {
      total: seriesDots(rows.total, x, scale),
      per_thread: seriesDots(rows.per_thread, x, scale),
    },
    /* `span` geht mit zurück: der Zeiger rechnet seinen Bucket aus genau dieser
       Zahl (Anzahl Buckets − 1) und nicht aus der Kartenbreite. `hold` ebenso:
       der Readout brückt mit derselben Konstante, mit der der Pfad zeichnet. */
    rows: rows, max: max, points: nums.length, n: rows.total.length, span: span,
    hold: SPARK_HOLD,
  };
}

/* Das Fenster als Zahl mit Einheit: `15 min`, oder `90 s`, wenn es keine vollen
   Minuten sind. Die Einheit ist die der Achse, nicht die der Sprache. */
const windowLabel = ws => {
  const s = Number(ws);
  if (!isFinite(s) || s <= 0) return '—';
  return s % 60 === 0 ? nf(s / 60, 0) + ' min' : nf(s, 0) + ' s';
};

/* Der letzte echte Punkt einer Reihe — die Zahl, die im aria-label steht. */
const lastNum = arr => {
  for (let i = arr.length - 1; i >= 0; i--) if (isNum(arr[i])) return arr[i];
  return null;
};

/* ── Der Zeiger auf der Kurve (issue #102) ───────────────────────────────── */
/* Die Fläche ist 34 px hoch und trägt keine y-Achse: eine Skala wäre dort
   kleiner als ihre Beschriftung. Der Zeiger ersetzt sie — er nennt **beide**
   Zahlen des Buckets, auf den er zeigt, und zwar in der Kopfzeile, wo die
   Legende steht. Kein Kasten über der Fläche (er verdeckte genau die Form, nach
   der man fragt), keine neue Höhe, keine neuen Wörter: die zwei ids der Legende
   sind die zwei ids des Readouts.

   Der Zustand liegt in einer Modulvariable und wird von `sparkline()` gelesen,
   nicht nachträglich ins DOM geschrieben. Damit trägt ihn jeder 2-s-Patch von
   selbst — die Zeichenkette ist die Funktion dieses Zustands (dieselbe Regel
   wie `showDim` und `loadHideIdle`). Ein Fadenkreuz, das nur von Hand ins SVG
   gemalt wird, wäre zweimal pro Sekunde weg. */
let sparkHover = null;            /* {key: host name, i: bucket index} oder null */

/* Bildschirm-x → Bucket. Der viewBox (1000 Einheiten) ist auf die Karte
   gestreckt, also sind viewBox-Einheiten keine Pixel: der Bruchteil kommt aus
   dem Rechteck des `<svg>`, der Index aus `span` — gerundet, damit ein Zeiger
   zwischen zwei Buckets den näheren nennt, und geklemmt, damit ein Zeiger am
   Rand nicht in einen Bucket zeigt, den es nicht gibt. */
function sparkIndex(clientX, rect, span) {
  const w = rect && rect.width > 0 ? rect.width : 0;
  if (!w) return 0;
  return Math.round(clamp((clientX - rect.left) / w, 0, 1) * Math.max(1, span));
}

/* A number, or `—`. A bucket without its own sample is not automatically no
   measurement: the probe runs every 10 s into 5 s buckets, every second one is
   empty by design (#96), and the line draws across it. The readout follows the
   line (#105): an empty bucket the pen crosses names the value of the segment —
   the sample before it — marked with `~`. The mark is the tilde and not the
   sample's timestamp: at 12 px `~152.5` is a number, `12:34` would be two
   readings in one column, and with `tabular-nums` the header does not shift by
   four digits when the pointer crosses a gap. A run of three or more empty
   buckets (a lost tick, an absent node) stays `—` — exactly where the line
   tears. `0` would never do: it would be the clock as a result. */
const sparkValue = (rows, i, hold) => {
  const v = seriesValueAt(rows, i, hold);
  return isNum(v.value) ? (v.held ? '~' : '') + nf(v.value, 1) : '—';
};

function sparkReadout(g, i) {
  const hold = g && isNum(g.hold) ? g.hold : SPARK_HOLD;
  return {total: sparkValue(g && g.rows.total, i, hold),
          per_thread: sparkValue(g && g.rows.per_thread, i, hold)};
}

/* Nach einer Zustandsänderung wird die Flotte neu gezeichnet: `apply()`
   vergleicht die Zeichenkette und schreibt nur, was sich geändert hat — das
   `<svg>` bleibt stehen, Fadenkreuz und die zwei Zahlen kommen dazu. */
function sparkRepaint() {
  if (last) apply('fleet', renderFleet(last.h));
}

function sparkHoverSet(key, i) {
  if (sparkHover && sparkHover.key === key && sparkHover.i === i) return sparkHover;
  sparkHover = {key: key, i: i};
  sparkRepaint();
  return sparkHover;
}

function sparkHoverClear() {
  if (!sparkHover) return null;
  sparkHover = null;
  sparkRepaint();
  return null;
}

/* Der Zeiger wird auf Dokument-Ebene gelesen, wie der Tooltip: die Karte wird
   alle 2 s gepatcht, ein Listener am `<svg>` wäre nach dem ersten Patch weg.
   Gerechnet wird gegen die Plotfläche, nicht gegen den ganzen Block — die
   Kopfzeile trägt Legende und Fenster und ist keine Zeitachse. `touch` bleibt
   draußen: auf einem Finger gibt es kein Hovern, und das `aria-label` nennt
   weiterhin die letzten Werte beider Reihen. */
function sparkMove(ev) {
  if (ev.pointerType === 'touch') return;
  const box = ev.target && ev.target.closest ? ev.target.closest('.spark') : null;
  const plot = box ? box.querySelector('.spark-plot') : null;
  if (!plot) return sparkHoverClear();
  const r = plot.getBoundingClientRect();
  if (ev.clientY < r.top || ev.clientY > r.bottom) return sparkHoverClear();
  const card = box.closest('[data-key]');
  if (!card) return sparkHoverClear();
  return sparkHoverSet(card.getAttribute('data-key'),
                       sparkIndex(ev.clientX, r, Number(plot.getAttribute('data-span'))));
}

document.addEventListener('pointermove', sparkMove);
/* Verlässt der Zeiger das Fenster, kommt kein `pointermove` mehr — ohne diese
   zwei Zeilen bliebe der Readout auf dem letzten Bucket stehen. */
document.addEventListener('pointerout', ev => { if (!ev.relatedTarget) sparkHoverClear(); });
window.addEventListener('blur', sparkHoverClear);

/* Der Block: eine Kopfzeile (Einheit links, die zwei Linien in der Mitte, das
   Fenster rechts) und die Zeichnung darunter — 14 px Kopf + 34 px Fläche.

   Der Rahmen steht **immer** (issue #96). Vorher kam für einen Knoten ohne
   `series` ein leerer String zurück: nach einem Neustart verschwand der Block
   und die Karte wurde kürzer — „solche Dynamik ist nicht gut für das GUI". Ein
   Knoten mit 0, mit 1 und mit 200 Proben hat jetzt dieselbe Geometrie; was
   fehlt, ist Tinte, nicht Höhe. Steht keine Probe im Fenster, sagt das eine
   Zeile in der Fläche — in der Sprache der Seite, aus dem Katalog. Die Zeile
   ist absolut positioniert und kann die Höhe deshalb nicht bewegen; sie liegt
   über der 34-px-Fläche, deren unterer Rand 6 px über dem Blockende sitzt
   (`.spark{padding:0 0 6px}`). */
function sparkline(x) {
  const entry = hostSeries(x);
  const g = entry ? seriesGeom(entry) : null;
  const drawn = !!(g && g.points);
  const win = windowLabel(entry ? entry.window_s : null);
  const empty = t('fleet.series_empty');
  const aria = drawn
    ? x.name + ': ' + t('fleet.series_total') + ' ' + nf(lastNum(g.rows.total), 1) +
      ' tok/s · ' + t('fleet.series_per_thread') + ' ' + nf(lastNum(g.rows.per_thread), 1) +
      ' tok/s · ' + win
    : x.name + ': ' + empty;
  /* Steht der Zeiger auf dieser Karte? Dann tragen die zwei Legendenmarken die
     Zahl ihres Buckets, und in der Fläche steht das Fadenkreuz. Ohne Probe im
     Block gibt es nichts zu zeigen — dann bleibt die Legende, wie sie ist. */
  const at = drawn && sparkHover && sparkHover.key === x.name ? sparkHover.i : null;
  const ro = at === null ? null : sparkReadout(g, at);
  const line = (cls, d) => d
    ? '<path class="spark-line ' + cls + '" d="' + d + '" vector-effect="non-scaling-stroke"></path>'
    : '';
  const dot = (cls, d) => d
    ? '<path class="spark-dot ' + cls + '" d="' + d + '" fill="none" stroke-width="5"' +
      ' stroke-linecap="round" vector-effect="non-scaling-stroke"' +
      ' style="stroke:' + (cls === 'total' ? 'var(--load)' : 'var(--fg-3)') + '"></path>'
    : '';
  /* Die Marke behält ihr Muster (durchgezogen/gestrichelt) und bekommt die Zahl
     daneben; die Wörter sind dieselben wie ohne Zeiger, damit der Kopf beim
     Hovern nicht umbricht. `tabular-nums` hält die Breite beim Wandern ruhig. */
  const key = (cls, label, v) =>
    '<span class="spark-key"><span class="sw ' + cls + '"></span>' + esc(label) +
    (v === null ? '' : '<b class="spark-v">' + esc(v) + '</b>') + '</span>';
  const cross = at === null ? '' :
    '<line class="spark-cross" x1="' + sparkX(at, g.span) + '" y1="0" x2="' +
    sparkX(at, g.span) + '" y2="' + SPARK_H + '" vector-effect="non-scaling-stroke"></line>';
  return `
      <div class="spark" style="position:relative" role="img" aria-label="${esc(aria)}">
        <div class="spark-head">
          <span class="spark-unit">tok/s</span>
          <span class="spark-leg">
            ${key('solid', t('fleet.series_total'), ro ? ro.total : null)}
            ${key('dash', t('fleet.series_per_thread'), ro ? ro.per_thread : null)}
          </span>
          <span class="spark-win">${esc(win)}</span>
        </div>
        <svg class="spark-plot" viewBox="0 0 ${SPARK_W} ${SPARK_H}" preserveAspectRatio="none" data-span="${g ? g.span : 1}" aria-hidden="true" focusable="false">${drawn
          ? line('total', g.total) + line('per_thread', g.per_thread) +
            dot('total', g.dots.total) + dot('per_thread', g.dots.per_thread) + cross
          : ''}</svg>${drawn ? '' : `
        <span class="spark-empty" style="position:absolute;left:0;right:0;bottom:6px;height:${SPARK_H}px;display:flex;align-items:center;justify-content:center;font-size:var(--t-xs);color:var(--fg-3);pointer-events:none">${esc(empty)}</span>`}
      </div>`;
}

/* ── Host-Streifen (issue #89) ────────────────────────────────────────────── */
/* Ein Knoten trägt eine Farbe und ein Muster. Beides hängt an seiner Identität
   — `node_id`, wie oMLX sie meldet — und nie am Index in `hosts[]`: sonst
   malte ein Knoten, der den Pool verlässt, alle anderen um, und „M3 ist der
   grüne" wäre nach jedem Neustart falsch. Ein Knoten ohne gemeldete `node_id`
   fällt auf seinen Namen zurück; der ist ebenfalls stabil und ebenfalls nicht
   positional. 16 Plätze sind 4 Töne × 4 Muster: zwei Knoten können sich einen
   Platz teilen — der Name steht weiter im Text —, aber nie einen Ton ohne ein
   anderes Muster. Die Zuordnung ist die ganze Aussage; sie steht in
   docs/dashboard-design.md §3, nicht in einem Katalog (der Streifen braucht
   keinen Text und ist `aria-hidden`). */
const HOST_TONES = 4;
const HOST_PATTERNS = ['solid', 'hatch', 'dots', 'lines'];

function hostSlot(x) {
  const s = String((x && (x.node_id || x.name)) || '');
  let h = 2166136261;                                  /* FNV-1a, 32 Bit */
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) % (HOST_TONES * HOST_PATTERNS.length);
}

function hostStripe(x) {
  if (!x) return '';
  const slot = hostSlot(x);
  const tone = slot % HOST_TONES;
  /* Das Muster wird um den Ton gedreht: zwei Knoten mit demselben Ton bekommen
     dadurch immer ein anderes Muster. Das ist die Invariante, die die
     Graustufen-Prüfung trägt — die Farbe darf fehlen, das Muster nicht. */
  const pattern = HOST_PATTERNS[(Math.floor(slot / HOST_TONES) + tone) % HOST_PATTERNS.length];
  return '<i class="host-stripe" aria-hidden="true"' +
    ' data-host="' + esc(x.node_id || x.name || '') + '"' +
    ' data-slot="' + slot + '"' +
    ' data-tone="' + (tone + 1) + '"' +
    ' data-pattern="' + pattern + '"></i>';
}

/* Name → Knoten der Antwort, damit eine Zeile, die nur den Namen kennt (die
   Thread-Registry, die Verteilung), den Streifen desselben Knotens bekommt. */
const hostsByName = h => new Map(((h && h.hosts) || []).map(x => [x.name, x]));

/* ── 1 Flotte ─────────────────────────────────────────────────────────────── */
/* Zwei Basen, nie gemischt: `used_bytes` = Schätzung (oMLX /health,
   current_model_memory), `used_bytes_measured` = Summe der actual_size
   (oMLX /v1/models/status). Die Karte zeigt die Messung, der Guard rechnet mit
   der Schätzung — deshalb bleibt die Schätzung als zweite Spur sichtbar. */
function memBar(h) {
  const hasMeasured = h.used_bytes_measured !== null && h.used_bytes_measured !== undefined;
  const measured = hasMeasured ? h.used_bytes_measured : h.used_bytes;
  const estimated = h.used_bytes;
  const ceiling = h.ceiling_bytes || 0;
  const hard = h.hard_threshold === null || h.hard_threshold === undefined ? 0.95 : h.hard_threshold;

  const ratio = ceiling ? measured / ceiling : null;
  const ratioEst = ceiling && estimated !== null && estimated !== undefined ? estimated / ceiling : null;
  const headroom = ceiling && measured !== null && measured !== undefined ? ceiling - measured : null;
  /* Der Guard entscheidet auf der Schätzung, nicht auf der Messung. */
  const over = ratioEst !== null && ratioEst > hard;
  const hardPct = clamp(hard * 100, 0, 100);
  const usedPctN = ratio === null ? 0 : clamp(ratio * 100, 0, 100);
  const estPctN = ratioEst === null ? usedPctN : clamp(ratioEst * 100, 0, 100);
  const estWidth = Math.max(0, estPctN - usedPctN);
  const estExcess = estWidth > 0.05;

  const ariaMem = hasMeasured
    ? t('fleet.aria_measured', {measured: gb(measured), ceiling: gb(ceiling), estimated: gb(estimated)})
    : t('fleet.aria_estimate', {measured: gb(measured), ceiling: gb(ceiling)});

  return `
      <div class="mem">
        <div class="mem-top">
          <span class="lbl">${esc(t('fleet.memory'))}</span>
          <span class="mem-right">
            <span class="mem-amount">
              ${T('used', '<b class="num">' + gb(measured) + '</b>')}<span class="unit">GB</span>
              <span class="of">${esc(t('fleet.of'))}</span>
              ${T('ceiling', '<span class="num cap">' + gb(ceiling || null) + '</span>')}<span class="unit">GB</span>
            </span>
            ${hasMeasured ? '<span class="mem-est">' + T('used_est', esc(t('fleet.estimated', {v: gb(estimated)}))) + '</span>' : ''}
          </span>
        </div>
        <div class="bar" role="img" aria-label="${esc(h.name)}: ${esc(ariaMem)}">
          ${hostStripe(h)}
          <span class="fill${estExcess ? ' joint' : ''}${over ? ' over' : ''}" style="width:${usedPctN.toFixed(1)}%"></span>
          ${estExcess ? '<span class="est' + (over ? ' over' : '') + '" style="left:' + usedPctN.toFixed(1) + '%;width:' + estWidth.toFixed(1) + '%"></span>' : ''}
          <span class="danger" style="left:${hardPct.toFixed(1)}%"></span>
          <span class="hard" style="left:${hardPct.toFixed(1)}%"></span>
          <span class="limit"></span>
        </div>
        <div class="mem-foot">
          <span>${T('ratio', '<b style="color:var(--fg-2)">' + pct(ratio) + ' %</b>')} ${esc(t('fleet.used_pct'))}</span>
          <span>${T('headroom', gb(headroom) + ' GB')} ${esc(t('fleet.reserve'))}</span>
          <span>${T('hard', esc(t('fleet.hard_limit', {v: nf(hard * 100, 0)})))}</span>
        </div>
      </div>`;
}

/* Welche Modelle sind auf diesem Knoten gesperrt **und** geladen? Das ist genau
   die Prüfung, mit der `ripple/calibration_api.py` einen Kalibrierlauf ablehnt
   (`host.locked_loaded()`), und die Seite hat sie längst: `models[].hosts[].lock`
   steht für „gesperrt **und hier** geladen", nicht für „gesperrt". Ohne diese
   Zeile war der Knopf genau dann frei, wenn der Klick eine 409 erzeugt — auf M5
   und M3 liegt DeepSeek gesperrt und geladen, auf M3 ohne gebundenen Thread,
   also sah der Knopf dort einladend aus und konnte nichts tun (issue #128). */
function lockedOn(h, name) {
  const out = [];
  for (const m of ((h && h.models) || [])) {
    for (const x of (m.hosts || [])) {
      if (x.host === name && x.lock) out.push(m.id);
    }
  }
  return out.sort();
}

/* Die Reihenfolge der Gründe ist die des Endpunkts (job → thread → lock): der
   Tooltip nennt den Grund, der zuerst absagen würde, damit die Seite nie eine
   andere Ursache verspricht als die Antwort nennt. `force=1` übergeht den
   Thread, nie den Lock — das ist der Unterschied zwischen „warten" und
   „entsperren", und er steht in beiden Tooltips. */
function calButton(x, h) {
  const locked = lockedOn(h, x.name);
  const held = (x.thread_slots || 0) > 0;
  const busy = !!calibBusy[x.name];
  const why = busy ? t('fleet.cal_busy')
            : held ? t('fleet.cal_held')
            : locked.length ? t('fleet.cal_locked', {models: locked.join(', ')})
                            : t('fleet.cal_ready');
  return `<button class="btn" data-node="${esc(x.name)}" onclick="calibrate('${esc(x.name)}')"` +
         `${busy || held || locked.length ? ' disabled' : ''} title="${esc(why)}">` +
         `${esc(t('fleet.calibrate'))}</button>`;
}

/* Das Ende des Ladevorgangs ist absolut: `started_at + eta_seconds` aus
   /healthz. Die Anzeige ist nur der Abstand zu dieser Zahl (#34). */

/* `size_gb` ist bereits Gigabyte (proxy.py rundet `bytes / 1e9`), `gb()` erwartet
   dagegen Bytes — der doppelte Teiler zeigte "0.0 GB" für einen 453-GB-Load
   (#36). Hier also `nf`, nicht `gb`. */
const loadDeadline = L => (typeof L.started_at === 'number' && typeof L.eta_seconds === 'number'
  ? L.started_at + L.eta_seconds : null);

function loadingLine(x) {
  const L = x.loading;
  if (!L) return '';
  const at = loadDeadline(L);
  const left = at === null ? null
    : `<span data-deadline="${esc(at)}" data-deadline-kind="load">` +
      esc(deadlineText('load', at - pageNow())) + '</span>';
  return `<div class="loading tip" data-tip="${esc(tipBody(['loading', 'load_remaining']))}"><span>${t('fleet.loading', {model: esc(L.model)})}</span>` +
         `<span>${nf(L.size_gb, 1)} GB</span><span>${left === null ? '—' : left}</span>` +
         `${L.source ? '<span class="src">' + esc(L.source) + '</span>' : ''}</div>`;
}

/* Ein Preload (issue #119) ist ein Ladevorgang ohne Anfrage: das Schloss
   verspricht Residenz, und der Dienst lädt das Modell, sobald der Knoten gesund
   wird. Der laufende Vorgang steht in `loadingLine` (`source: preload`); hier
   stehen nur die zwei Sätze, die der Ladezustand nicht sagen kann: warum ein
   Preload nicht ging, und wen er verdrängt hat. Beides aus /healthz →
   hosts[].preloads, nichts davon ist hier erfunden. */
function preloadLine(x) {
  const rows = [];
  (x.preloads || []).forEach(p => {
    const displaced = (p.displaced || []).join(', ');
    if (p.state === 'failed') {
      rows.push('<div class="loading preload-fail" data-model="' + esc(p.model) + '">' +
        '<span>' + t('fleet.preload_failed',
                     {model: esc(p.model), reason: esc(p.reason || '—')}) + '</span></div>');
    } else if (displaced) {
      rows.push('<div class="loading preload-evict" data-model="' + esc(p.model) + '">' +
        '<span>' + t('fleet.preload_displaced',
                     {model: esc(p.model), list: esc(displaced)}) + '</span></div>');
    }
  });
  return rows.join('');
}

/* ── Wartung (Betreiberaktion, admin-Schlüssel, issue #10) ─────────────────── */
/* „eine Maschine schonend aus der Betriebsbereitschaft nehmen, z.B. um Updates
   reinzuspielen": der Knoten nimmt keine neue Arbeit mehr an, laufende Anfragen
   laufen zu Ende. Das Backend hält das Fenster (`ripple/maintenance.py`,
   persistiert), die Karte zeigt es nur an — `state`, `maintenance` und
   `inflight` kommen aus /healthz, nichts davon ist hier erfunden. */
const MAINT_PRESETS = [[30, 'maint.preset_30'], [60, 'maint.preset_60'], [null, 'maint.preset_endless']];

/* Ortszeit dieses Rechners, wie `until` sie trägt — ohne Locale-Formatierung,
   damit die Anzeige nicht von der ICU-Version des Browsers abhängt. */
const maintClock = iso => {
  const d = new Date(iso);
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
};

/* Ein Satz über das Fenster, aus der Antwort von GET/POST/DELETE — für das
   Panel im Betriebsblock, nicht für die Karte (die liest /healthz). */
const maintLine = v => {
  const w = v && v.maintenance;
  if (!w) return t('maint.none');
  return (w.until ? t('maint.until', {v: maintClock(w.until)}) : t('maint.endless')) +
    (w.minutes_left === null || w.minutes_left === undefined ? '' : t('maint.line_left', {v: nf(w.minutes_left, 0)})) +
    t('maint.line_by', {v: w.by || 'operator'}) +
    (v.in_pool ? t('maint.in_pool') : t('maint.not_in_pool')) +
    ((v.inflight || 0) > 0 ? t('maint.running', {n: nf(v.inflight)}) : t('maint.nothing_running'));
};

function maintControl(x, keyed) {
  const w = x.maintenance || null;
  /* Ohne Schlüssel ist der Knopf aus und sagt warum — dieselbe Regel wie bei
     Calibrate: der Schlüssel liegt in sessionStorage['ripple_admin']. */
  const dis = keyed ? '' : ' disabled';
  const why = keyed ? '' : ' title="' + esc(keyHelp()) + '"';
  const presets = MAINT_PRESETS.map(([m, id]) =>
    '<button class="btn maint-btn" data-node="' + esc(x.name) + '"' +
    ' onclick="maintenance(\'' + esc(x.name) + '\', ' + (m === null ? 'null' : m) + ')"' +
    dis + why + '>' + T('maint_preset', esc(t(id))) + '</button>').join('');

  /* work_in_progress ist kein Zustand, den RIPPLE hält: es ist `inflight > 0`
     auf einem Knoten, der keine neue Arbeit mehr annimmt. Bei 0 ist die Maschine
     frei und ein Update gefahrlos — dann heißt die Zeile `ready_for_update`. */
  const drain = !w ? ''
    : ((x.inflight || 0) > 0
        ? '<span class="maint-drain">' + T('maint_drain', esc(t('maint.work_in_progress', {n: nf(x.inflight)}))) + '</span>'
        : '<span class="maint-done">' + T('maint_done', esc(t('maint.ready_for_update'))) + '</span>');
  const until = !w ? ''
    : (w.until ? T('maint_until', esc(t('maint.until', {v: maintClock(w.until)})))
               : T('maint_until', esc(t('maint.endless'))));
  const left = w && w.until ? T('maint_left', esc(t('maint.left', {v: nf(w.minutes_left, 0)}))) : '';
  const by = w ? T('maint_by', esc(t('maint.by', {v: w.by || 'operator'}))) : '';
  const end = !w ? ''
    : '<button class="btn maint-end" data-node="' + esc(x.name) + '"' +
      ' onclick="maintenanceClear(\'' + esc(x.name) + '\')"' + dis +
      ' title="' + esc(keyed ? t('maint.end_tip') : keyHelp()) + '">' +
      esc(t('maint.end')) + '</button>';

  return `
      <div class="maint${w ? ' on' : ''}" data-key="maint-${esc(x.name)}">
        <div class="maint-row">
          <span class="maint-lbl">${T('maint', esc(t('maint.label')))}</span>
          ${drain}${until}${left}${by}
          <span class="maint-btns">${presets}${end}</span>
        </div>
        ${keyed ? '' : '<div class="maint-why">' + esc(keyHelp()) + '</div>'}
      </div>`;
}

/* Die relative Last neben dem Gewicht (issue #80, Variante 3). Das Gewicht ist
   die Absicht des Optimierers, die Last die Wirklichkeit: `1 − aktuelle Rate /
   Einzelstrom-Rate`, also der Anteil der Einzelstrom-Rate, der gerade verloren
   geht. Sie steht in derselben Unterzeile wie das Gewicht, weil die Karte bei
   1440 px keinen Platz für eine weitere Zeile hat (der Fahrtenschreiber hat ihn
   verbraucht) — die Zahl steht neben der Zahl, auf die sie sich bezieht.
   Hier steht nur die Zahl: in einer Viertel-Karte sind zwei Wörter in jeder
   Sprache zu breit (griechisch 171 px bei 150 px Platz). Das Wort trägt der
   Tooltip der Zelle (`metric(['slots', …, 'relative_load'])` nennt beide), die
   Erklärung `tip.relative_load`. Die zwei Prozentzahlen sind damit nicht
   selbsterklärend — der Tooltip ist der Ort, an dem das Wort steht.
   `null` heißt **nicht gemessen**, nicht 0: eine fehlende Messung ist keine
   Aussage über die Maschine, eine 0 wäre eine. Der Tooltip geht über denselben
   Layer wie jeder andere (`data-tip`), nicht über `title` — ein natives Title
   neben dem Layer wären zwei Kästen. */
function relLoad(x) {
  if (!isNum(x.relative_load))
    return '<span class="rel none" data-tip="' + esc(t('fleet.not_measured') + '  ' +
      t('common.source') + ': ' + SRC.relative_load) + '">—</span>';
  const v = nf(clamp(x.relative_load, 0, 1) * 100, 0);
  return '<span class="rel">' + esc(t('fleet.relative_load', {v: v})) + '</span>';
}

/* Das Alter der Messung in der Einheit, in der man es liest (issue #80). Rohe
   Sekunden waren die Ursache des Überlaufs: „32018569 s ago" ist in einer
   149,6-px-Zelle fünfstellig und in sieben Sprachen breiter als die Zelle; ein
   Alter von 86 400 s gehört als „1 d" gelesen. Die Grenzen sind die des
   Kalenders (60 s, 60 min, 24 h), nicht gerundet — deshalb `Math.floor` und
   kein Nachkommateil: „1,5 d" wäre eine Genauigkeit, die die Messung nicht hat.
   Die Probenzahl stand bis #80 in derselben Zeile und ist in den Tooltip der
   Zelle gewandert (siehe `metric(…, more)`): zwei Angaben passen hier in keiner
   Sprache, eine schon. `null` heißt **nicht gemessen** — `fleet.no_sample`,
   nicht „0 s". */
function ageText(sec) {
  if (!isNum(sec)) return t('fleet.no_sample');
  if (sec <= 5) return t('fleet.now');
  if (sec < 60) return t('fleet.ago_s', {v: nf(sec, 0)});
  if (sec < 3600) return t('fleet.ago_min', {v: nf(Math.floor(sec / 60), 0)});
  if (sec < 86400) return t('fleet.ago_h', {v: nf(Math.floor(sec / 3600), 0)});
  return t('fleet.ago_d', {v: nf(Math.floor(sec / 86400), 0)});
}

/* ── die Messung des Servers (issue #98) ─────────────────────────────────── */
/* oMLX legt in jede Antwort, was es selbst gemessen hat; `/healthz →
   hosts[].usage` ist die Zusammenfassung je Knoten. Die Zelle zeigt **eine**
   Zahl: wie viele Token der letzten Anfrage aus dem Prefix-Cache kamen, gegen
   die Prompt-Größe — `689 / 694` heißt „der Cache hat getragen", `0 / 691`
   heißt „kalter Prefix". Ein fehlender Wert ist `—` und niemals `0`: eine 0 ist
   eine Aussage über den Cache, ein fehlendes Feld ist keine. Die Quote in der
   Unterzeile ist abgeleitet (`cached / prompt`) und deshalb nicht im Payload;
   die Probenzahl und die Raten stehen im Tooltip. */
function cacheOf(x) {
  const u = (x && x.usage) || null;
  const last = (u && u.last) || {};
  if (!u || !isNum(last.cached_tokens)) return null;
  const prompt = isNum(last.prompt_tokens) && last.prompt_tokens > 0
    ? last.prompt_tokens : null;
  return {
    tokens: last.cached_tokens,
    prompt: prompt,
    share: prompt === null ? null : clamp(last.cached_tokens / prompt, 0, 1),
    samples: isNum(u.samples) ? u.samples : 0,
    pp: last.prompt_tokens_per_second,
    tg: last.generation_tokens_per_second,
  };
}

/* Ist dieses Modell auf diesem Knoten *jetzt* zu bedienen (Issue #116)?
   `dispatch` beantwortet die weitere Frage („würde RIPPLE hierher senden?" — ein
   Kaltstart zählt mit), und genau daraus wurde vorher die Verfügbarkeit
   abgeleitet: ein gelistetes, aber nicht geladenes Modell stand als routbar neben
   `loaded: false`. Das Backend schickt dafür `routable` (Residency, nicht Lock).
   `phase === 'pp'` bleibt zusätzlich routbar: dort läuft bereits eine Anfrage,
   der Kaltstart ist im Gange — das Abzeichen soll nicht bei jedem Laden blinken
   (#18). Der Rückfall gilt nur für Payloads ohne das Feld (Fixtures). */
const routableNow = r => !!r && (r.phase === 'pp' || (r.routable !== undefined
  ? r.routable === true
  : r.dispatch !== false && r.loaded === true));

function renderFleet(h) {
  const keyed = !!adminKey();
  return (h.hosts || []).map(x => {
    const tps = hostTps(x);
    const age = tps && tps.measured_at ? Math.max(0, Math.round(Date.now() / 1000 - tps.measured_at)) : null;
    const cache = cacheOf(x);
    const split = threadSplit(h, x.name);
    const loaded = (x.models_loaded || []).length;
    const routable = (h.models || []).filter(m =>
      (m.hosts || []).some(r => r.host === x.name && routableNow(r))).length;
    // The declared site against the one the used address falls into (issue #67).
    // A location is data, so a contradiction is shown here — the same fact
    // `ripple doctor` reports as one check.
    const siteWarn = x.site_mismatch
      ? '<span class="site-warn">⚠ ' + esc(t('fleet.site_mismatch',
          {declared: x.site, derived: x.site_derived || '?'})) + '</span>'
      : '';
    const metric = (keys, label, value, sub, flip, more, cls) =>
      cell(keys, '<div class="metric-lbl">' + label + '</div>' +
                 '<div class="metric-val num">' + value + '</div>' +
                 '<div class="metric-sub">' + sub + '</div>', flip, more, cls);
    /* Woher der Knoten stammt (Issue #70): aus der handgeschriebenen
       config.yaml oder aus dem Overlay, das die Seite selbst schreibt. Ohne
       diese Marke sieht ein am Page hinzugefügter Knoten aus wie einer, den
       jemand in der Datei vergessen hat. Der Tip nennt seine Quelle wie jeder
       andere (`tipBody` baut `tip.<key>` + Quelle; hier steht der Satz direkt,
       weil es keinen zweiten Satz zu übersetzen gibt). */
    const srcBadge = '<span class="host-src" data-src="' + esc(x.source || 'config') + '"' +
      ' data-tip="' + esc(t('fleet.source_tip') + '  ' + t('common.source') +
                           ': /healthz → hosts[].source') + '">' +
      esc(t(x.source === 'overlay' ? 'fleet.source_overlay' : 'fleet.source_config')) +
      '</span>';

    return `
    <article class="card host" data-key="${esc(x.name)}">
      ${hostStripe(x)}
      <div class="host-head">
        <div class="host-title">
          <h3>${esc(x.name)}</h3>
          ${chip(x.state || x.status || 'unknown', x.phase || null,
                 x.phase ? phaseTip(x.phase, 'hosts[].phase') : '')}
          <span class="role">${esc(x.role)} · ${esc(x.site)}</span>
          ${srcBadge}
          ${siteWarn}
        </div>
        <div class="host-addr">
          <span class="addr">${T('address', esc(x.address))}</span>
          <span class="addr-note">${T('addr_sw', esc(t('fleet.addr_note', {n: nf((x.addresses || []).length),
            m: nf(x.addr_switches)})))}</span>
          ${calButton(x, h)}
        </div>
      </div>
      ${memBar(x)}
      ${loadingLine(x)}
      ${preloadLine(x)}
      <div class="metrics">
        ${metric(['inflight', 'max_inflight', 'spill'], esc(t('fleet.inflight')),
                 nf(x.inflight) + '<span class="den"> / ' + nf(x.max_inflight) + '</span>',
                 esc(t('fleet.spill_from', {v: nf(x.spill_threshold)})))}
        ${metric(['slots', 'max_threads', 'target_share', 'relative_load'], esc(t('fleet.slots')),
                 nf(x.thread_slots) + '<span class="den"> / ' + nf(x.max_threads) + '</span>',
                 esc(t('fleet.target', {v: nf((x.target_share || 0) * 100, 0)})) + ' · ' + relLoad(x),
                 false,
                 /* The split lives in the tooltip (#93): the cell is one figure
                    (slots used / slots available) in a 150 px box, and a second
                    number there breaks the layout in half the languages. */
                 t('fleet.slots_split', {active: nf(split.active), idle: nf(split.idle),
                                         silent: nf(split.silent)}))}
        ${metric(['rtt'], 'RTT',
                 nf(x.rtt_ms, 1) + '<span class="den"> ms</span>',
                 esc(t('fleet.probe', {host: x.name})), true)}
        ${metric(['tps', 'samples', 'measured'], 'tok/s',
                 tps ? nf(tps.tg_tps, 1) : '—',
                 tps ? esc(ageText(age)) : esc(t('fleet.no_sample')), true,
                 tps ? esc(t('fleet.samples', {n: nf(tps.tg_samples)})) : '')}
        ${metric(['cache'], esc(t('fleet.cache')),
                 cache ? nf(cache.tokens) +
                   (cache.prompt === null ? '' : '<span class="den"> / ' + nf(cache.prompt) + '</span>')
                   : '—',
                 cache && cache.share !== null
                   ? esc(t('fleet.cache_hit', {v: nf(cache.share * 100, 0)}))
                   : esc(t('fleet.no_sample')),
                 false,
                 cache ? esc(t('fleet.samples', {n: nf(cache.samples)})) +
                   (isNum(cache.pp) && isNum(cache.tg)
                     ? '\n' + esc(t('fleet.rates', {pp: nf(cache.pp, 1), tg: nf(cache.tg, 1)}))
                     : '')
                   : '',
                 'wide')}
      </div>
      ${sparkline(x)}
      <div class="host-foot">
        <span>${T('version', esc(t('fleet.version', {v: x.version || '—'})))}</span>
        <span>${T('guard', 'Guard ' + esc(x.guard_enabled ? t('common.on') : t('common.off')))}</span>
        <span>${T('models_avail', esc(t('fleet.models', {n: nf(x.models_available)})))}</span>
        <span>${T('loaded_routable', esc(t('fleet.loaded_routable', {loaded: nf(loaded), routable: nf(routable)})))}</span>
        <span>${T('requests', esc(t('fleet.requests', {n: nf(x.requests_total)})))}</span>
        <span>${T('errors', esc(t('fleet.errors', {n: nf(x.errors_total)})))}</span>
      </div>
      ${maintControl(x, keyed)}
    </article>`;
  }).join('');
}

/* ── Kennzahlenleiste ─────────────────────────────────────────────────────── */
function renderKpis(h, s) {
  const kpi = (keys, label, value, sub) =>
    '<div class="kpi tip cell" data-tip="' + esc(tipBody(keys)) + '">' +
      '<div class="kpi-lbl">' + label + '</div>' +
      '<div class="kpi-val num">' + value + '</div>' +
      '<div class="kpi-sub">' + sub + '</div>' +
    '</div>';
  const d = s.decisions || [];
  const overflows = (s.http_503_total || 0) + (s.http_507_total || 0);
  return [
    kpi(['k_req', 'k_ok'], esc(t('kpi.requests')), nf(s.requests_total),
        esc(t('kpi.ok', {n: nf(s.requests_ok)}))),
    kpi(['k_err'], esc(t('kpi.errors')), nf(s.errors_total), esc(t('kpi.since_start'))),
    kpi(['k_5xx'], '503 / 507', nf(s.http_503_total) + ' / ' + nf(s.http_507_total),
        esc(overflows ? t('kpi.overflows', {n: nf(overflows)}) : t('kpi.no_overflows'))),
    kpi(['k_hits'], esc(t('kpi.hits')), nf(s.thread_hits),
        esc(t('kpi.of_requests', {n: nf(s.requests_total)}))),
    kpi(['k_new'], esc(t('kpi.threads_new')), nf(s.threads_new),
        esc(t('kpi.evicted', {n: nf(s.threads_evicted)}))),
    kpi(['k_rebind'], esc(t('kpi.rebind')), nf(s.threads_rebound), esc(t('kpi.rebound'))),
    kpi(['k_uptime'], esc(t('kpi.uptime')), nf((h.uptime_seconds || 0) / 3600, 1) + '<span class="den"> h</span>',
        esc(t('kpi.since_boot'))),
  ].join('');
}

/* ── 2 Modelle ────────────────────────────────────────────────────────────── */
/* Loaded models are the answer to FA-22 point 2 (a matrix model × node with
   residency). The available-but-not-loaded ones are the same matrix with the
   answer "no": they cost disk, not memory, and there are twenty of them. They
   stay in the table, collapsed behind one row that names their number — the
   design hands that content decision to this issue (§6). */
/* Expanded by default: Models is its own tab since #56, so the tab's job is to
   show what the fleet has, not what it has loaded. A click on the divider row
   collapses the twenty dimmed rows when an operator wants only the busy ones. */
let showDim = true;

/* ── Modell-Policy (Betreiberaktion, issue #11) ───────────────────────────── */
/* Der Betreiber will ein Modell aus dem Betrieb nehmen, ohne oMLX anzufassen. Zwei
   Stufen, beide je (Knoten, Modell) und beide reiner RIPPLE-Zustand:
   `disabled` routet nicht mehr hierhin, das Modell bleibt aber in /v1/models,
   solange ein anderer Knoten es anbietet; `hidden` bietet es Clients gar nicht
   mehr an, sobald jeder Knoten es verbirgt. Das Bedienelement sitzt deshalb in
   der Knotenzelle: eine Zeile ist ein (Modell, Knoten)-Paar, ein Zustand. */
/* Der Zustand ist ein Token (er geht so an den Server), das Wort kommt aus
   dem Katalog — `disabled` stand hier vorher als id und wurde deshalb im
   deutschen Bild englisch angezeigt. */
const POLICY_STATES = [['', 'models.policy_active'],
                       ['disabled', 'models.policy_disabled'],
                       ['hidden', 'models.policy_hidden']];

/* Der Tooltip nennt den Zustand, den /healthz meldet, und die Quelle — der
   Zustand selbst ist ein Datum, kein Dekor. */
const policyTip = level => tipBody(['policy']) + '\n' +
  (level ? t('models.policy_tip', {level: level}) : t('models.policy_tip_none'));

function policyControl(m, x, keyed) {
  /* Ein oMLX-Builtin bekommt kein Auswahlfeld (issue #118): es steht in keiner
     Allow-List, `disabled`/`hidden` hätten dort keine Wirkung, und ein Feld, das
     sich nie ändern lässt, sähe aus wie eine Entscheidung, die es nicht gibt.
     Die Zelle bleibt ohne Feld; das Schloss daneben sagt, was das Modell ist. */
  if (m.builtin) return '';
  const level = x.policy || '';
  const opts = POLICY_STATES.map(([value, label]) =>
    '<option value="' + value + '"' + (value === level ? ' selected' : '') + '>' +
    esc(t(label)) +
    '</option>').join('');
  /* Ohne Schlüssel ist das Feld aus und die Tabelle sagt warum (keyHelp()) —
     dieselbe Regel wie bei Calibrate und Wartung: der Schlüssel liegt in
     sessionStorage['ripple_admin']. */
  return '<select class="pol-sel" data-level="' + esc(level) + '"' +
    ' data-node="' + esc(x.host) + '" data-model="' + esc(m.id) + '"' +
    ' data-tip="' + esc(policyTip(level)) + '"' +
    ' aria-label="' + esc(t('models.policy_aria', {model: m.id, host: x.host})) + '"' +
    ' onchange="modelPolicy(this)"' + (keyed ? '' : ' disabled') + '>' + opts + '</select>';
}

function modelRows(m, order, keyed) {
  const hosts = m.hosts.slice().sort((a, b) => (order.get(a.host) ?? 9) - (order.get(b.host) ?? 9));
  return hosts.map((x, i) => {
    const cls = [i === 0 ? 'group-start' : '', x.loaded ? '' : 'dim'].filter(Boolean).join(' ');
    let out = '<tr class="' + cls + '" data-key="' + esc(m.id + '|' + x.host) + '">';
    if (i === 0) {
      /* Die Zelle kürzt lange Namen mit „…“; das Abzeichen „nicht routbar“ wird
         mitgekürzt. Der `title` trägt den Zustand deshalb mit, sonst wäre er
         beim Überfahren eines gekürzten Namens verloren (#19). Über dem Namen
         steht seit #23 das Schloss des Locks — in dieser Zelle, weil der Lock je
         Modell gilt und die Zelle die Zeilen eines Modells zusammenfasst.
         Seit #116 heißt „routbar“ Residency, nicht `dispatch`: ein gelistetes,
         aber nicht geladenes Modell trägt das Abzeichen jetzt auch dann, wenn ein
         Eviction-Lock gesetzt ist. Der Lock verhindert das Entladen — er belegt
         nicht, dass geladen ist; sein Tooltip sagt das (`lock.nowhere`). */
      const notHere = x.dispatch === false || !routableNow(x);
      const state = notHere ? ' · ' + t('models.not_routable') : '';
      out += '<td class="model-cell" rowspan="' + hosts.length + '" title="' + esc(m.id + state) + '">' +
             lockButton(m, keyed) +
             /* Neben dem Schloss, nicht hinter dem Namen: das Schloss steht
                ohnehin über dem Namen und kostet keine Zeilenhöhe — das Zeichen
                hier erbt denselben Trick (#139). Es steht immer da: rot für
                „hier nicht", grün für „hier ja". */
             nrIcon(x) +
             '<span class="model-line"><span class="model-name">' + esc(m.id) + '</span>' +
             '</span></td>';
    }
    out += '<td class="node-cell"><span class="node-name">' + esc(x.host) + '</span>' +
           /* Ein Modell, das ein Preload verdrängt hat (issue #119), bleibt im
              Modell sichtbar: `evicted_by` nennt den Vorgang, der es gekostet
              hat. Der Lock gewinnt die Eviction — verschwinden darf sie nicht. */
           (x.evicted_by
             ? '<span class="evicted tip" data-tip="' +
               esc(t('models.evicted_by', {model: x.evicted_by})) + '">▼</span>'
             : '') +
           policyControl(m, x, keyed) + '</td>';
    /* Der Punkt sagt seit #18 auch, was der Knoten mit dem Modell tut: `pp`
       blinkt orange (Prefill — warten), `tg` blinkt grün (es fließt). Steht
       keine Phase an, bleibt es der ruhige Punkt von vorher. Die Phase hängt an
       beiden Punkten, auch am leeren: ein Modell, das gerade kalt geladen wird,
       ist noch nicht `loaded`, wartet aber schon auf seinen ersten Token — genau
       der Fall, für den die Seite da ist. Der `title` nennt Phase und Quelle,
       damit die Farbe nie der einzige Träger ist. */
    const ph = x.phase || null;
    const phaseAttr = ph ? ' data-phase="' + esc(ph) + '"' : '';
    out += '<td class="num"><span class="dot ' + (x.loaded ? 'on' : 'off') + '"' + phaseAttr +
           ' title="' + esc((x.loaded ? t('models.dot_loaded') : t('models.dot_available')) +
                            (ph ? ' · ' + phaseTip(ph, 'models[].hosts[].phase') : '')) +
           '"></span></td>';
    /* tok/s steht seit dem 21.11.2026 direkt hinter LOADED und in Fettdruck:
       es ist die einzige Zahl der Zeile, die sich im Sekundentakt ändert, und
       der Grund, warum man die Tabelle überhaupt aufschlägt. SIZE und CONTEXT
       sind statisch und stehen deshalb am Ende — die Zeile liest sich von links
       nach rechts als „was läuft, wie schnell, wie viel, wie groß".
       One number, and it is the one measured for *this* (node, model) pair
       (#124). The rate oMLX reports per request and the service smooths
       (alpha 0.3) falls when requests run in parallel — it is not a property
       of the machine. Until a request for this model has finished on this node
       there is no such number, and the cell says so with the same dash the
       rest of the table uses; a measured zero is a number and stays `0,0`.
       An oMLX builtin is never routed (`builtin`, #118), so it never gets a
       measurement and keeps the dash.

       The machine's calibrated capability (`tg_tps` from config.yaml, the
       AS-6 number `placement.py` decides with) is a different base and lives
       only in the tooltip, explicitly labelled as the machine's — it must
       never stand in the cell as if it described this model (#124). */
    const measured = !m.builtin && isNum(x.tg_tps);
    const cap = capability.get(x.host);
    const tip = [
      tipText('tps_m'),
      m.builtin ? t('lock.builtin_tip', {model: m.id})
                : measured ? t('models.tps_measured', {v: nf(x.tg_tps, 1)})
                           : t('models.tps_never'),
      isNum(cap) ? t('tip.tps_cap', {v: nf(cap, 1)}) + '  ' + t('common.source') +
                   ': ' + SRC.tps_cap
                 : t('models.tps_cap_none') + '  ' + t('common.source') + ': ' + SRC.tps_cap,
    ].join('\n');
    out += '<td class="num dtps"><span class="tip" data-tip="' + esc(tip) + '">' +
           (measured ? nf(x.tg_tps, 1) : '—') + '</span></td>';
    out += '<td class="num">' + T('running', nf(x.inflight)) + '</td>';
    out += '<td class="num">' + T('threads_m', nf(x.threads)) + '</td>';
    /* Der Trenner zwischen geschätzter und gemessener Größe stand im dunkelsten
       Linienton und war auf dem Körper kaum zu sehen; er trägt jetzt die Farbe
       der Zahlen neben sich (#19). Nur im Körper — die Kopfzeile schreibt ihn
       als Text. */
    out += '<td class="num">' + T('size_est', gb(x.estimated_size)) +
           '<span style="color:var(--fg)"> / </span>' +
           (x.actual_size === null || x.actual_size === undefined ? '—' : T('size_act', gb(x.actual_size))) + '</td>';
    out += '<td class="num">' + (x.context_len === null || x.context_len === undefined ? '—' : T('ctx', nf(x.context_len))) + '</td>';
    return out + '</tr>';
  }).join('');
}

/* Tabler `route-off` (outline, 24er Raster, Strichstärke 2 wie das Original) —
   als Zeichen, nicht als Bild: `currentColor` nimmt die Farbe aus dem CSS, und
   die Datei braucht keine zweite Anfrage. */
const ROUTE_OFF =
  '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor"' +
  ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"' +
  ' aria-hidden="true" focusable="false">' +
  '<path d="M4 19a2 2 0 1 0 4 0a2 2 0 1 0 -4 0"/>' +
  '<path d="M16 5a2 2 0 1 0 4 0a2 2 0 1 0 -4 0"/>' +
  '<path d="M12 19h4.5c.71 0 1.372 -.212 1.924 -.576m1.545 -2.459a3.5 3.5 0 0 0 -3.469 -3.965h-.499m-4 0h-3.501a3.5 3.5 0 0 1 -2.477 -5.972m2.477 -1.028h3.5"/>' +
  '<path d="M3 3l18 18"/></svg>';

/* Dasselbe Zeichen ohne Strich (`route`), für den Fall, dass der Knoten das
   Modell hier ausliefert. Rot und Grün sind dann zwei Zustände eines Zeichens
   und nicht ein Fehler neben einem Nichts — die Balance, die das Auge sucht. */
const ROUTE_ON =
  '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor"' +
  ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"' +
  ' aria-hidden="true" focusable="false">' +
  '<path d="M3 19a2 2 0 1 0 4 0a2 2 0 0 0 -4 0"/>' +
  '<path d="M19 7a2 2 0 1 0 0 -4a2 2 0 0 0 0 4"/>' +
  '<path d="M11 19h5.5a3.5 3.5 0 0 0 0 -7h-8a3.5 3.5 0 0 1 0 -7h4.5"/></svg>';

/* Das Abzeichen ist seit dem 21.11.2026 ein Zeichen, kein Kasten (#139): „nicht
   routbar" stand als 84 × 35 px großer Chip in einer 100 px breiten Zelle und
   brach auf zwei Zeilen — 21 davon in der Tabelle. Das Icon sagt dasselbe und
   gibt der Zeile ihre Höhe zurück; der Tooltip nennt den Grund, den der Chip
   nie genannt hat. Zwei Ursachen, zwei Sätze: `dispatch === false` heißt, der
   Knoten bietet das Modell nicht an; sonst ist es gelistet, aber nicht geladen,
   und die erste Anfrage zahlt einen Kaltstart. `role="img"` und `aria-label`,
   weil ein Screenreader das Zeichen nicht sieht, den Satz aber hören soll. */
const nrIcon = x => {
  /* Jeder Tooltip der Seite nennt seine Quelle — dieser auch, sonst ist der Satz
     eine Behauptung ohne Adresse (`test_every_tooltip_names_its_source`). */
  const here = !(x.dispatch === false) && routableNow(x);
  const text = (here ? t('models.routable')
                     : t('models.not_routable') + ' · ' +
                       t(x.dispatch === false ? 'models.nr_dispatch' : 'models.nr_resident')) +
               '\n' + t('common.source') + ': /healthz → models[].hosts[].dispatch, .routable';
  return '<span class="nrt' + (here ? ' ok' : '') + ' tip" role="img" aria-label="' + esc(text) +
         '" data-tip="' + esc(text) + '">' + (here ? ROUTE_ON : ROUTE_OFF) + '</span>';
};

function renderModels(h) {
  const keyed = !!adminKey();
  const order = new Map((h.hosts || []).map((x, i) => [x.name, i]));
  const models = (h.models || []).slice().sort((a, b) => {
    const la = a.hosts.some(x => x.loaded) ? 0 : 1;
    const lb = b.hosts.some(x => x.loaded) ? 0 : 1;
    return la !== lb ? la - lb : a.id.localeCompare(b.id);
  });
  const loaded = models.filter(m => m.hosts.some(x => x.loaded));
  const dim = models.filter(m => !m.hosts.some(x => x.loaded));

  let rows = loaded.map(m => modelRows(m, order, keyed)).join('');
  if (dim.length) {
    const label = dim.length === 1 ? t('models.count_one', {n: nf(dim.length)})
                                   : t('models.count_many', {n: nf(dim.length)});
    rows += '<tr class="divider-row toggle" data-toggle="dim" ' +
            'title="' + esc(t('models.dim_tip')) + '">' +
            '<td colspan="8"><span class="tw" aria-hidden="true">' +
            (showDim ? '▾' : '▸') + '</span> ' +
            esc(t('models.dim')) + ' · ' + T('not_loaded', esc(label)) + '</td></tr>';
    if (showDim) rows += dim.map(m => modelRows(m, order, keyed)).join('');
  }

  /* Ohne Schlüssel ist jedes Feld aus; der Satz darüber sagt warum, statt dass
     ein Dutzend tote Auswahlfelder die Frage offen lassen. */
  const why = keyed ? ''
    : '<div class="pol-why">' + esc(keyHelp()) + '</div>';

  /* Acht Spalten in einem festen Raster; die Breiten sind aus den gemessenen
     Textbreiten der Kopfzeilen abgeleitet, damit keine Unterzeile über ihre
     Spalte hinausläuft. Die Knotenspalte trägt seit #11 zusätzlich das
     Policy-Feld und ist deshalb breiter als der längste Knotenname.

     The fixed widths are the measured maxima of the header cells across all
     sixteen catalogs, not estimates: Greek needs 105 px for "φορτωμένο",
     Hungarian 105 for the node and 99 for the context, Russian 93 for "req",
     Turkish 97 for threads. A column that is too narrow does not clip its
     header — the text runs into the neighbouring column, which reads as a
     rendering bug (#53). The last column keeps its 104 px: its subtitle names
     the measurement (`measured here`), and the longest of the sixteen
     translations still needs the room. Since #124 the cell holds one number —
     the machine capability moved into the tooltip. */
  return `
    ${why}
    <table>
      <colgroup>
        <col style="width:auto"><col style="width:108px"><col style="width:108px">
        <col style="width:104px"><col style="width:96px"><col style="width:100px">
        <col style="width:122px"><col style="width:102px">
      </colgroup>
      <thead>
        <tr>
          <th><span class="th-main">${esc(t('models.th_model'))}</span><span class="th-sub">${esc(t('models.th_model_sub'))}</span></th>
          <th><span class="th-main">${esc(t('models.th_node'))}</span><span class="th-sub">${esc(t('models.th_node_sub'))}</span></th>
          <th class="num"><span class="th-main">${T('loaded', esc(t('models.th_loaded')))}</span><span class="th-sub">${esc(t('models.th_loaded_sub'))}</span></th>
          <th class="num"><span class="th-main">${T('tps_m', 'tok/s')}</span><span class="th-sub">${esc(t('models.th_tps_sub'))}</span></th>
          <th class="num"><span class="th-main">${T('running', esc(t('models.th_req')))}</span><span class="th-sub">${esc(t('models.th_req_sub'))}</span></th>
          <th class="num"><span class="th-main">${T('threads_m', esc(t('models.th_threads')))}</span><span class="th-sub">${esc(t('models.th_threads_sub'))}</span></th>
          <th class="num"><span class="th-main">${esc(t('models.th_size'))}</span><span class="th-sub">${esc(t('models.th_size_sub'))}</span></th>
          <th class="num"><span class="th-main">${T('ctx', esc(t('models.th_ctx')))}</span><span class="th-sub">${esc(t('models.th_ctx_sub'))}</span></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

/* ── Regeln ohne Tabellenzeile (issue #16) ────────────────────────────────── */
/* Die Tabelle oben wird aus /healthz gebaut und kennt deshalb nur Modelle, die
   ein Knoten **gerade** anbietet. Die Modelldatei kann aber eine Regel für ein
   Modell halten, das der Knoten nicht mehr führt — nach einer Allow-List-
   Änderung, einem oMLX-Update oder einer gelöschten Modelldatei. Diese Regel
   hätte keine Zeile, wäre also unsichtbar und nur über die Admin-Routen
   zurücknehmbar. Deshalb liest der Betriebsblock `GET /admin/nodes/{n}/models`
   und zeigt jede Regel mit `offered: false`.

   Gelesen wird nur, was auch sichtbar ist: mit Admin-Schlüssel, beim Öffnen von
   `Betrieb` und einmal im ersten Tick nach einer Änderung — nicht in jedem
   2-s-Tick und ohne Schlüssel überhaupt nicht. Ein Admin-Request alle zwei
   Sekunden wäre Last ohne Erkenntnis; ohne Schlüssel wäre er ein 401 im Log. */
const MODEL_FILE = '~/.ripple/models.yaml';

let policyOrphans = null;   // null = noch nicht gelesen, [] = keine Regel ohne Zeile
let policyError = '';       // eine Knotensicht war nicht lesbar
let policyMsg = null;       // die letzte Rückmeldung einer Änderung
let policyStale = false;    // eine Änderung: der nächste Tick liest einmal nach
let policyBusy = false;

const adminPolicyPath = node =>
  '/admin/nodes/' + encodeURIComponent(node) + '/models';

/* Eine Zeile je Regel: Modell, Knoten, wann gesetzt, und dasselbe Bedienelement
   wie in der Tabelle — `in Betrieb` nimmt die Regel zurück. Ein eigenes Feld
   wäre eine zweite Bedienung desselben Zustands. */
function policyOrphanRow(r) {
  const when = r.since ? String(r.since).replace('T', ' ').slice(0, 16) : '—';
  const why = t('policy.orphan_why', {file: MODEL_FILE}) + '  ' + t('common.source') + ': ' +
    'GET /admin/nodes/{name}/models → offered: false';
  return '<tr data-key="' + esc(r.node + '|' + r.model) + '">' +
    '<td><code>' + esc(r.model) + '</code>' +
      '<span class="tip" data-tip="' + esc(why) + '">?</span></td>' +
    '<td>' + esc(r.node) + '</td>' +
    '<td class="num"><span class="tip" data-tip="' + esc(t('policy.orphan_since_tip') + '  ' + t('common.source') + ': GET /admin/nodes/{name}/models → since') + '">' +
      esc(when) + '</span></td>' +
    '<td>' + policyControl({id: r.model}, {host: r.node, policy: r.level}, true) + '</td>' +
    '</tr>';
}

function renderPolicyPanel() {
  const parts = [];
  if (policyMsg) parts.push('<div class="' + policyMsg.cls + '">' + esc(policyMsg.text) + '</div>');
  /* Ohne Schlüssel gibt es nichts zu lesen — also auch keine Anfrage und keine
     Liste, sondern den Satz, der sagt warum. */
  if (!adminKey()) {
    parts.push(esc(t('panel.policy.idle')));
    parts.push('<div class="calib-line">' + t('policy.no_key') + '</div>');
    return parts.join('');
  }
  if (policyError) parts.push('<div class="calib-line bad">' + esc(policyError) + '</div>');
  if (policyOrphans === null) {
    if (!policyError) parts.push('<div class="calib-line">' + t('policy.unread') + '</div>');
    return parts.join('');
  }
  if (!policyOrphans.length) {
    parts.push('<div class="calib-line">' + t('policy.none', {file: MODEL_FILE}) + '</div>');
    return parts.join('');
  }
  const n = policyOrphans.length;
  parts.push('<div class="calib-head"><strong>' + esc(t('policy.head')) + '</strong>' +
    T('policy_orphans', esc(n === 1 ? t('policy.count_one', {n: nf(n)}) : t('policy.count_many', {n: nf(n)}))) + '</div>');
  parts.push('<div class="calib-line">' + t('policy.intro') + '</div>');
  parts.push('<table><thead><tr><th>' + esc(t('policy.th_model')) + '</th><th>' + esc(t('policy.th_node')) +
    '</th><th class="num">' + esc(t('policy.th_since')) + '</th>' +
    '<th>' + esc(t('policy.th_state')) + '</th></tr></thead><tbody>' +
    policyOrphans.map(policyOrphanRow).join('') + '</tbody></table>');
  return parts.join('');
}

function paintPolicy() {
  apply('policyPanel', renderPolicyPanel());
}

/* Eine Anfrage je Knoten, nur die Regeln ohne Zeile bleiben übrig. Ein Knoten,
   der nicht antwortet, kostet seine Zeilen und wird genannt — eine leere Liste
   wäre hier eine Behauptung, die niemand geprüft hat. */
async function loadPolicyOrphans() {
  if (!adminKey() || policyBusy) return policyOrphans;
  /* Öffnet jemand `Betrieb` vor dem ersten /healthz, ist noch kein Knoten
     bekannt: dann liest der nächste Tick, statt dass die Anfrage ausfällt. */
  if (!last) { policyStale = true; return policyOrphans; }
  const hosts = ((last.h && last.h.hosts) || []).map(x => x.name);
  if (!hosts.length) return policyOrphans;
  policyBusy = true;
  try {
    const views = await Promise.all(hosts.map(name =>
      adminApi('GET', adminPolicyPath(name))
        .then(view => ({name, view}), () => ({name, view: null}))));
    const rows = [];
    for (const {name, view} of views) {
      for (const r of ((view && view.models) || [])) {
        if (r.offered === false) {
          rows.push({node: name, model: r.id, level: r.level || '',
                     since: r.since, by: r.by, note: r.note || ''});
        }
      }
    }
    const failed = views.filter(v => v.view === null).map(v => v.name);
    policyError = failed.length
      ? t('policy.read_failed', {nodes: failed.join(', ')})
      : '';
    policyOrphans = rows;
  } finally {
    policyBusy = false;
  }
  paintPolicy();
  return policyOrphans;
}

/* Öffnen von `Betrieb` ist der Moment, in dem die Liste sichtbar wird — dann
   wird sie gelesen, vorher nicht. */
(function watchOps() {
  const ops = $('ops');
  if (!ops || typeof ops.addEventListener !== 'function') return;
  ops.addEventListener('toggle', () => {
    if (!ops.open || !adminKey()) return;
    loadPolicyOrphans();
  });
})();

/* ── 2 Verteilung ─────────────────────────────────────────────────────────── */
/* Ein Block **je Modell** (issue #73): der Modellname ist die Überschrift, die
   Blöcke stehen nebeneinander (`#dist` ist das Raster, siehe dashboard.css) und
   unter 900 px untereinander. Vorher standen alle Modelle untereinander in
   einem Block — der Vergleich, um den es hier geht, lag damit zwei
   Bildschirmhälften auseinander, und die Thread-Registry daneben machte die
   Zeile so hoch, dass die Auslastung darunter eine Lücke ließ. */
/* Zwei Währungen, zwei Spalten (issue #91). Der Slot-Anteil ist die
   Routing-Größe und bleibt — aber er zählt **Threads**, nicht Arbeit: ein Modell
   ohne warmen Thread hatte hier gar keine Zeile, obwohl es auf beiden Knoten
   geladen war und Anfragen bediente. Deshalb steht die Überschrift jetzt
   `thread share` (nicht `share`), daneben die Requests je (Knoten, Modell) im
   selben Fenster wie der Verlauf, und eine Zeile ohne Slot sagt mit
   `no warm thread`, warum ihr Balken fehlt. */
function renderDist(h, s) {
  const th = h.threads || {};
  const total = th.active || 0;
  const slotsTotal = (h.hosts || []).reduce((a, x) => a + (x.max_threads || 0), 0);
  const order = new Map((h.hosts || []).map((x, i) => [x.name, i]));
  /* Der Streifen einer Zeile ist der ihres Knotens (#89) — dieselbe Zuordnung
     wie auf der Karte und in der Thread-Zeile, aus derselben `node_id`. */
  const byName = hostsByName(h);
  const win = h.requests_window_seconds;
  /* Requests dieses Modells auf diesem Knoten im Fenster (#91). Der Zähler
     steht am **Knoten** (`hosts[].requests_by_model`, ein Modell → Zahl), nicht
     an der Modellzeile — die kennt nur Threads. Der Zähler läuft ab dem Start;
     das Fenster schneidet ihn auf denselben Zeitraum zu, den der Verlauf zeigt. */
  const nodeReq = new Map((h.hosts || []).map(x => [x.name, x.requests_by_model || {}]));
  const reqOf = (m, hostName) => Number((nodeReq.get(hostName) || {})[m.id] || 0);
  const reqTip = t('dist.req_window_tip') + '  ' + t('common.source') +
    ': /healthz → hosts[].requests_by_model';
  /* Listed is who holds a slot, **or** served traffic in the window, **or** is
     loaded and routable on at least one node (#107). The third case is the
     operator's: `Qwen3.5-9B-oQ4e-mtp` sits resident on both nodes (16.8 GB),
     holds no thread and is called about every 50 minutes — so it was visible
     for 15 minutes and gone for 35, and the operator never saw it in a day.
     The row is not wrong, it was intermittent; the weight is the fact that
     does not blink.

     `loaded` and `dispatch` are per (model, node), and both are required: a
     model that is resident but not routed to is not part of the distribution.
     `MarkItDown` is an oMLX-internal tool and arrives as `dispatch: false`,
     `allowed: false`, `actual_size: 0`, `tg_tps: null` on every node — it gets
     no row until a policy says otherwise. Models that are not loaded (20 of 23
     today) stay out as before. */
  const residentRoutable = m => (m.hosts || []).some(
    x => x.loaded === true && x.dispatch === true);
  const withWork = (h.models || []).filter(m =>
    residentRoutable(m) || (m.hosts || []).some(
      x => (x.threads || 0) > 0 || reqOf(m, x.host) > 0));

  if (!withWork.length) {
    /* Ein Block ohne Nachbarn nimmt die ganze Zeile: eine halbe Spalte mit
       einem Satz darin sähe aus wie ein fehlender Block. */
    return '<div class="dist-block dist-note dist-solo">' + esc(t('dist.empty')) + '</div>';
  }

  return withWork.map(m => {
    const threadsHere = m.hosts.reduce((a, x) => a + (x.threads || 0), 0);
    /* Ohne belegten Slot gibt es keinen Ist-Anteil: 0 / 0 ist keine Messung,
       sondern eine Division durch null. Dann steht hier der Satz, der das sagt,
       statt „Ist 100,0 %“ und „Ist 0,0 %“. Die Zeilen bleiben trotzdem stehen —
       eine Zahl Requests ohne Slot ist der Grund, warum es den Block gibt. */
    /* One row per (model, node) — but a node that would neither dispatch this
       model nor served it has nothing to distribute there, and a row of zeros
       under "target share" would claim it does. So the row set is the nodes
       that can serve it (`dispatch`) or did (a thread, a request in the
       window); the second kind is why Qwen3.5-9B keeps its M3 row in #91 even
       with `dispatch: false`. A model included by `residentRoutable` always
       has at least one such node, so `rows` is never empty. */
    const rows = m.hosts.filter(x => x.dispatch !== false ||
        (x.threads || 0) > 0 || reqOf(m, x.host) > 0)
      .sort((a, b) => (order.get(a.host) ?? 9) - (order.get(b.host) ?? 9)).map(x => {
      const tShare = (th.target_share || {})[x.host] ?? 0;
      const aShare = (th.actual_share || {})[x.host] ?? 0;
      const d = aShare - tShare;
      const reqs = reqOf(m, x.host);
      /* Der Balken ist der Anteil an den *belegten* Slots; ohne einen einzigen
         Slot gibt es nichts zu füllen. Dann sagt die Zelle selbst, warum. */
      const track = threadsHere === 0
        ? '<span class="dist-why">' + esc(t('dist.no_warm_thread')) + '</span>'
        : `<span class="dtrack">${hostStripe(byName.get(x.host) || {name: x.host})}<i class="a" style="width:${(aShare * 100).toFixed(1)}%"></i><i class="tk" style="left:${(tShare * 100).toFixed(1)}%"></i></span>`;
      const actual = threadsHere === 0 ? '—' : T('share_a', pct(aShare), true);
      const delta = threadsHere === 0
        ? '—'
        : `<span class="delta ${Math.abs(d) > 0.1 ? 'off' : 'on'}"
            data-tip="${esc(tipText('delta'))}">${signed(d)}</span>`;
      return `
        <div class="dist-row">
          <span class="dist-host" title="${esc(x.host)}">${esc(x.host)}</span>
          ${track}
          <span class="dval num tip" data-tip="${esc(reqTip)}">${nf(reqs)}</span>
          <span class="dval plain">${T('share_t', pct(tShare), true)}</span>
          <span class="dval">${actual}</span>
          <span class="dval">${delta}</span>
        </div>`;
    }).join('');

    const reqLine = m.hosts.map(x => {
      const n = ((s.by_host || {})[x.host] || {}).requests;
      const all = s.requests_total;
      return esc(x.host) + ' ' + nf(n) + ' (' + nf(all ? n / all * 100 : 0, 1) + ' %)';
    }).join(' · ');

    return `
      <div class="dist-block" data-key="${esc(m.id)}">
        <div class="dist-head">
          <span class="dist-name" title="${esc(m.id)}">${esc(m.id)}</span>
          <span class="dist-count">${T('slots_total', esc(t('dist.slots', {a: nf(threadsHere), b: nf(slotsTotal)})))}</span>
        </div>
        ${threadsHere === 0 ? '<div class="dist-empty">' + t('dist.no_slots') + '</div>' : ''}
        <div class="dist-cols" aria-hidden="true">
          <span class="dc"><b>${esc(t('dist.th_node'))}</b><span class="key">${esc(t('dist.th_node_sub'))}</span></span>
          <span class="dc"><b>${esc(t('dist.th_share'))}</b><span class="key">${esc(t('dist.th_share_sub'))}</span></span>
          <span class="dc num tip" data-tip="${esc(reqTip)}"><b>${esc(t('dist.th_req'))}</b><span class="key">${esc(t('dist.th_req_sub', {n: nf(win)}))}</span></span>
          <span class="dc num"><b>${esc(t('dist.th_target'))}</b><span class="key">%</span></span>
          <span class="dc num"><b>${esc(t('dist.th_actual'))}</b><span class="key">%</span></span>
          <span class="dc num"><b>${esc(t('dist.th_delta'))}</b><span class="key">${esc(t('dist.th_delta_sub'))}</span></span>
        </div>
        ${rows}
        <div class="dist-note tip" data-tip="${esc(t('dist.requests_tip') + '  ' + t('common.source') + ': /stats → by_host[].requests')}">
          ${esc(t('dist.requests_since', {list: reqLine}))}<br>
          ${threadsHere === 0
            ? t('dist.note_noslot')
            : total <= 1
              ? t('dist.note_one')
              : esc(t('dist.note_many'))}
        </div>
      </div>`;
  }).join('');
}

/* ── 4 Thread-Registry (issue #73: ein eigener Tab) ───────────────────────── */
/* Die Restzeit eines Threads (#34): `idle_seconds` wurde zum Zeitpunkt des
   Abrufs gemessen, also endet die Bindung bei
   `fetch_time + ttl_seconds − idle_seconds`. `fetch_time` ist die `server_time`
   dieses Payloads — dieselbe Uhr, gegen die die Seite zählt. */
/* Der letzte Griff auf einen Thread, der nicht durchging (#35): der Grund steht
   als Zeile im Block selbst. Der Block hat kein eigenes Panel wie Betrieb oder
   Richtlinien, und eine stille Absage wäre von einem Klick ohne Wirkung nicht zu
   unterscheiden. */
let threadError = '';

/* Der Quellen-Chip nennt die Herkunft, nicht das Alter (#103). `source` wird
   einmal bei der Geburt des Threads geschrieben und nie wieder: ein Thread, der
   ohne Affinity-Header ankommt, steht für immer als `new` im Register — auch
   nach 62 Turns, wenn an ihm nichts mehr neu ist. Das Log liest dieselbe
   Tatsache aus dem Schlüssel (`_thread_source()` in proxy.py: kein `sid:`-
   Präfix = `fp`) und ist damit die ehrlichere der beiden Sprachen. Deshalb
   benennt `new` hier nur die Geburt, und nur solange sie eine ist: ab dem
   zweiten Turn zeigt der Chip `fp`. Die übrigen Werte (`header`,
   `fingerprint`, `prefix_match`) sind keine Altersangabe und bleiben, wie sie
   sind — die volle Angleichung an das Log ist nicht Teil von #103. */
function threadSource(source, turns) {
  return source === 'new' && Number(turns) > 1 ? 'fp' : source;
}

/* A thread is **silent** when it has not been asked anything for this long
   (#93). 900 s is the placement thread decay window
   (`placement.thread_decay_seconds`) — the point at which placement itself
   stops counting the thread as warm — and half of the default
   `affinity.ttl_seconds` (1800 s). Measured on the live instance on 19.11.:
   a thread sat at `turns=23, active=0, idle_seconds=1394` and never received
   another request, because the client's session id had moved and the old
   cache scope can never be resumed. It still held a slot and a row until the
   TTL ran out, and the operator reads the thread count as "what is running".
   A silent thread is not running — the row says so. */
const THREAD_SILENT_SECONDS = 900;
const threadSilent = th => !(Number(th.active || 0) > 0) &&
  isNum(th.idle_seconds) && Number(th.idle_seconds) >= THREAD_SILENT_SECONDS;

/* Per node: how many of its threads carry a request right now, how many are
   idle, and how many of those have gone silent. The host card shows one figure
   in the cell and this split in the tooltip (#93). */
function threadSplit(h, host) {
  const reg = ((h.threads || {}).registry || []).filter(th => th.host === host);
  const active = reg.filter(th => Number(th.active || 0) > 0).length;
  return {active: active, idle: reg.length - active,
          silent: reg.filter(threadSilent).length};
}

function renderThreads(h) {
  const reg = (h.threads || {}).registry || [];
  const fetched = typeof h.server_time === 'number' ? h.server_time : pageNow();
  const keyed = !!adminKey();
  /* Der Streifen einer Thread-Zeile ist der des Knotens aus `threads[].host`
     (#89): die Zeile nennt den Namen weiter im Text, der Streifen macht nur das
     Scannen schneller. Ein Knoten, der nicht mehr in `hosts[]` steht (gerade
     entfernt), behält seinen Streifen über den Namen. */
  const byName = hostsByName(h);
  const notice = threadError ? '<div class="maint-why">' + esc(threadError) + '</div>' : '';
  const rows = reg.length
    ? reg.map(th => {
      const at = fetched + Number(th.ttl_seconds) - Number(th.idle_seconds);
      const text = deadlineText('thread', at - pageNow());
      /* Die TTL selbst steht im Tooltip: die Zeile zählt herunter, sie
         wiederholt die feste Zahl nicht. */
      const tip = tipBody(['th_remaining', 'th_ttl']) + '\n' +
        t('threads.ttl', {n: nf(th.ttl_seconds)});
      /* Der Löschknopf (#35): ohne Schlüssel aus und mit dem Grund im `title`,
         mit einem laufenden Request aus und mit `threads.delete_active` — ein
         Griff, der den Slot unter einer laufenden Anfrage wegzieht, ist genau
         das, was die Reservierung verhindert. Der Knopf steht *neben* der
         Frist, nicht darin: der 1-s-Takt schreibt nur den Text des
         `data-deadline`-Elements und muss dessen Knoten behalten (#34). */
      /* Der Zustand steht im Markup, nicht in einem Handler (#69): der 2-s-Patch
         ersetzt den ganzen Knoten, ein nachträglich gesetztes Attribut wäre beim
         nächsten Tick wieder weg. `data-busy` fehlt bei einem ruhenden Thread —
         ein Attribut mit `false` wäre eine zweite Wahrheit. */
      const busy = Number(th.active || 0) > 0;
      /* The silence (#93): a thread past the threshold gets the word, not just
         the absence of `data-busy` — `active: 0` also describes a thread that
         answered a second ago. The chip carries the threshold in its tooltip;
         the row keeps its slot until the TTL, so it cannot simply disappear. */
      const silent = threadSilent(th);
      const why = !keyed ? keyHelp() : busy ? t('threads.delete_active') : '';
      const del = '<button class="btn" style="margin-left:auto"' +
        ' data-key="' + esc(th.key) + '" onclick="threadDelete(this)"' +
        (keyed && !busy ? '' : ' disabled') +
        (why ? ' title="' + esc(why) + '"' : '') + '>' +
        T('th_delete', esc(t('threads.delete'))) + '</button>';
      return `
      <div class="thread-item" data-key="${esc(th.key)}"${busy ? ' data-busy="true"' : ''}${silent ? ' data-silent="true"' : ''}>
        ${hostStripe(byName.get(th.host) || {name: th.host})}
        <div class="th-top">
          <span class="th-key">${esc(th.key)}</span>
          <span class="chip">${esc(th.kind)} · ${esc(threadSource(th.source, th.turns))}</span>
          <span class="chip">${T('th_key', esc(th.key_name ? t('threads.key', {name: th.key_name})
                                                        : t('threads.key_anon')))}</span>
        </div>
        <div class="th-model" title="${esc(th.model)}">${esc(th.model)}</div>
        <div class="th-meta">
          <span>${esc(th.host)}</span>
          <span>${T('th_turns', esc(t('threads.turns', {n: nf(th.turns)})))}</span>
          <span>${busy ? T('th_idle', esc(t('threads.busy')))
                       : T('th_idle', esc(t('threads.idle', {t: ageText(Number(th.idle_seconds))})))}</span>
          ${silent ? '<span class="chip th-silent tip" data-tip="' +
            esc(t('threads.silent_tip', {min: nf(THREAD_SILENT_SECONDS / 60)})) + '">' +
            esc(t('threads.silent')) + '</span>' : ''}
          <span><span data-deadline="${esc(at)}" data-deadline-kind="thread" class="tip" data-tip="${esc(tip)}">${esc(text)}</span></span>
          ${del}
        </div>
      </div>`;}).join('')
    : '<div class="thread-item th-meta">' + esc(t('threads.none')) + '</div>';
  return notice + rows;
}

/* Ein Thread hält den Slot seines Knotens bis zum TTL (1800 s) — der Knopf
   beendet das vorher (#35). Eine Rückfrage, ein Aufruf über `adminApi()`, danach
   derselbe Tick wie nach jeder anderen Admin-Aktion; ein Abbruch der Rückfrage
   schickt nichts. Der Schlüssel im Pfad ist das `label`, das die Seite zeigt. */
async function threadDelete(btn) {
  const key = btn && btn.getAttribute ? btn.getAttribute('data-key') : '';
  if (!key) return;
  if (!adminKey()) { threadError = keyHelp(); repaintThreads(); return; }
  if (!window.confirm(t('threads.delete_confirm'))) return;
  threadError = '';
  try {
    await adminApi('DELETE', '/admin/threads/' + encodeURIComponent(key));
    await tick();
  } catch (e) {
    threadError = e.status === 401 ? keyHelp() : e.message;
    repaintThreads();
  }
}

const repaintThreads = () => { if (last) apply('threads', renderThreads(last.h)); };

/* ── 2.1 Auslastung je Host (issue #33) ───────────────────────────────────── */
/* Was die Knoten getan haben, seit RIPPLE läuft — nicht was RIPPLE ihnen
   geschickt hat. Zwei kompakte Tabellen übereinander:

   (a) Zeit: fünf *exklusive* Zustände je Knoten. Jeder Probe-Takt (10 s) wird
       genau einem zugeordnet, also addieren sie sich auf das abgetastete
       Fenster — die zweite Zeile jeder Zelle ist der Anteil daran. `loading`
       ist ein eigener Zustand: ein 453-GB-Kaltstart dauert zwei Minuten und ist
       weder Leerlauf noch Decode.
   (b) Arbeit: Anfragen, Prefill- und Decode-Sekunden aus dem `usage`, das oMLX
       selbst misst, daneben `client` — RIPPLEs eigene Uhr von der Auslieferung
       bis zum letzten Byte. Die beiden stehen nebeneinander und nicht
       untereinander, weil sie verschiedene Dinge messen: was der Client gewartet
       hat, ist nicht die Zeit, die der Knoten gearbeitet hat.

   Darunter die eigene Zählung der Knoten (SQLite, `range=today`) als
   Gegenprobe: die Lücke zur RIPPLE-Zählung ist der Verkehr, der an RIPPLE
   vorbei ging. Und die eine Aktion des Blocks — zurücksetzen — hinter dem
   Admin-Schlüssel, wie jeder andere Schreibgriff der Seite. */
/* Der eine Schreibgriff des Blocks: `loadBusy` hält den zweiten Klick ab,
   `loadError` ist die letzte Absage (Schlüssel fehlt oder der Endpunkt sagt nein). */
let loadBusy = false;
let loadError = '';
/* Der eine Lesegriff des Blocks (#39): mit ausgeblendetem Leerlauf rechnen die
   Anteile nur noch über die Zeit unter Last — Fenster minus Leerlauf. Reine
   Ansichtssache, kein Serverzustand: der Umschalter zeichnet den Block neu. */
let loadHideIdle = false;

/* Anzeigereihenfolge (#44): `mixed` steht zwischen den beiden Zustaenden, die es
   verbindet. Am Ende gelesen war es ein Rest am rechten Rand; zwischen Prefill
   und Decode liest der Balken als eine Bewegung — nichts, laden, prefill,
   prefill+decode, decode. */
const LOAD_STATES = [
  {id: 'load.idle', field: 'idle', tip: 'load_idle'},
  {id: 'load.loading', field: 'loading', tip: 'load_loading'},
  {id: 'load.prefill', field: 'prefill', tip: 'load_prefill'},
  {id: 'load.mixed', field: 'mixed', tip: 'load_mixed'},
  {id: 'load.decode', field: 'decode', tip: 'load_decode'},
];
/* Die Arbeitsspalten: Katalog-id der Überschrift, Feld im Payload, Tooltip-id
   und ob die Zahl eine Sekunden- oder eine Stückzahl ist. */
/* Die Arbeitstabelle trägt nur, was der Balken nicht sagt: wie viele Anfragen,
   und wie viele Tokens sie bewegt haben — in Millionen, weil sechsstellige
   Zahlen in einer Spalte niemand liest. Die Sekunden von Prefill und Decode
   stehen im Balken als Anteil und im Tooltip als Dauer; als eigene Spalten
   wären sie eine zweite Skala neben der ersten (#38). */
const LOAD_WORK = [
  {id: 'load.requests', field: 'requests', tip: 'load_requests', unit: 'n'},
  {id: 'load.prompt_tokens', field: 'prompt_tokens', tip: 'load_prompt_tokens', unit: 'mtok'},
  {id: 'load.completion_tokens', field: 'completion_tokens',
   tip: 'load_completion_tokens', unit: 'mtok'},
];
/* Die Einheit unter dem Kürzel (#37): Symbole, keine Wörter — dieselbe Regel wie
   bei `h`/`min`/`s` in `loadDur`. Eine Stückzahl trägt keine. */
const LOAD_UNIT = {n: '', s: 's', mtok: 'Mtok'};

/* Ein Wert der Arbeitstabelle in seiner Einheit. `Mtok` teilt durch 1e6 und
   schreibt zwei Stellen — 522 105 Tokens sind 0,52 Mtok, nicht 522.105. */
const loadValue = (value, unit) => {
  const n = Number(value || 0);
  if (unit === 'mtok') {
    if (n === 0) return nf(0, 0);
    /* Eine frische Zählung liegt unter 0,005 Mtok; „0,00" wäre dann nicht falsch,
       aber unlesbar — also die Schranke nennen, statt eine Null zu behaupten. */
    return n / 1e6 < 0.005 ? '<' + nf(0.01, 2) : nf(n / 1e6, 2);
  }
  return nf(n, 0);
};

/* Dauer in der Einheit, die die Zahl lesbar hält. Die Einheiten sind Symbole
   (`h`, `min`, `s`) wie `ms` im Entscheidungsstrom — sie werden nicht übersetzt. */
const loadDur = v => {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  const s = Number(v);
  if (s >= 3600) return nf(s / 3600, 1) + ' h';
  if (s >= 60) return nf(s / 60, 0) + ' min';
  return nf(s, 0) + ' s';
};

/* Wann das Fenster begann: Uhrzeit, und der Tag nur wenn er nicht heute ist —
   ein Reset von gestern soll nicht wie einer von vor zehn Minuten aussehen. */
const loadWhen = ts => {
  const d = new Date(Number(ts) * 1000);
  const hhmm = String(d.getHours()).padStart(2, '0') + ':' +
               String(d.getMinutes()).padStart(2, '0');
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear() &&
                  d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? hhmm
    : String(d.getDate()).padStart(2, '0') + '.' +
      String(d.getMonth() + 1).padStart(2, '0') + '. ' + hhmm;
};

const loadShare = (part, whole) =>
  (whole > 0 ? nf(Number(part || 0) / whole * 100, 1) + ' %' : '—');

/* Der Balken ist die Anzeige, nicht die Zahlenreihe (#38): fünf Zustände als ein
   waagerechter Balken je Knoten, Breite = Anteil am abgetasteten Fenster. Ein
   Zustand mit 0 s bekommt kein Segment — ein 0-%-Strich sähe nach Messung aus.
   Jedes Segment trägt Dauer und Prozentzahl im Tooltip; beides ist aus derselben
   Zahl gerechnet wie der Balken selbst. */
/* Das Rechenfenster einer Zeile: das abgetastete Fenster, oder — wenn der
   Leerlauf ausgeblendet ist — nur die Zeit unter Last (#39). */
const loadWindow = row => {
  const sampled = Number(row.sampled_seconds || 0);
  if (!loadHideIdle) return sampled;
  return Math.max(0, sampled - Number(row.idle || 0));
};

/* Die Zustände, die der Balken zeigt: alle, oder ohne Leerlauf. */
const loadStates = () => (loadHideIdle ? LOAD_STATES.filter(c => c.field !== 'idle')
                                       : LOAD_STATES);

const loadBar = row => {
  const window = loadWindow(row);
  if (!(window > 0)) return '<span class="loadbar empty"></span>';
  const segs = loadStates().map(col => {
    const value = Number(row[col.field] || 0);
    if (!(value > 0)) return '';
    const pct = value / window * 100;
    /* `toFixed` für die Breite: `nf()` schreibt im Deutschen ein Komma, und ein
       Komma in `style="width:…%"` ist keine Länge. */
    /* Die Werte dieser Zeile über der Quelle: `tipText` erklärt das Segment und
       nennt die Quelle, die zweite Zeile nennt Dauer und Anteil. */
    return '<span class="seg seg-' + col.field + '" style="width:' + pct.toFixed(3) + '%"' +
      ' data-tip="' + esc(tipText('load_seg') + '\n' +
        t('load.seg_tip', {state: t(col.id), t: loadDur(value), pct: nf(pct, 1)})) +
      '"></span>';
  }).join('');
  return '<span class="loadbar">' + segs + '</span>';
};

/* Die führende Zahl neben dem Balken: welcher Zustand die Zeile beherrscht. Ohne
   sie müsste man für jede Zeile über den Balken fahren, um zu wissen, was er
   sagt — und bei einem Balken, der zu 100 % aus einem Zustand besteht, ist das
   die einzige Information, die es gibt. */
const loadLead = row => {
  const window = loadWindow(row);
  /* Unter Last und trotzdem nichts zu verteilen: der Knoten war im ganzen
     Fenster leer. Ein leerer Balken ohne Wort wäre von „keine Daten" nicht zu
     unterscheiden, also sagt die Zeile es. */
  if (!(window > 0)) return loadHideIdle ? esc(t('load.noload')) : '';
  let best = null;
  for (const col of loadStates()) {
    const value = Number(row[col.field] || 0);
    if (!(value > 0)) continue;
    if (best === null || value > best.value) best = {col, value};
  }
  if (best === null) return '';
  const pct = best.value / window * 100;
  /* Auch diese Zahl braucht eine Quelle — sie ist aus demselben Fenster gerechnet
     wie der Balken und würde sonst als nackte Zahl dastehen. */
  return '<span class="tip" data-tip="' + esc(tipText('load_lead')) + '">' +
    '<b>' + nf(pct, 1) + ' %</b> ' + esc(t(best.col.id)) + '</span>';
};

/* Eine Zeile: Name, Balken, führende Zahl. Die Summenzeile bekommt dieselbe
   Form, damit sie nicht als Fußnote unter der Tabelle hängt. */
/* Die Summenzeile trägt eine Achse über die volle Zeilenbreite (#106). Sie ist
   der erste Sohn der Zeile, steht aber absolut (`left:0;right:0` im
   `position:relative` der Zeile) und damit außerhalb des Grids: als
   `grid-column:1/-1`-Kind würde sie eine zweite Rasterreihe samt `gap`
   aufmachen und die Zeile um 15 px wachsen lassen. Absolut sitzt sie im
   8-px-Abstand, den `margin-top` bezahlt. `aria-hidden`, weil sie nichts sagt,
   was der Text nicht schon sagt. */
const loadRow = (row, kind) =>
  '<div class="loadrow' + (row.name ? '' : ' total') + '" data-key="' + kind + '-' +
  esc(row.name || 'total') + '">' +
  (row.name ? '' : '<span class="load-sum-rule" aria-hidden="true"></span>') +
  '<span class="lr-name">' + esc(row.name || t('load.total')) + '</span>' +
  loadBar(row) +
  '<span class="lr-lead">' + loadLead(row) + '</span>' +
  '</div>';

/* Eine Legende für den ganzen Block statt fünf Spaltenköpfen: die Farbe allein
   trägt nie die Bedeutung (Design §3) — jeder Eintrag nennt den Zustand und
   erklärt ihn im Tooltip. */
const loadLegend = () =>
  '<div class="loadlegend">' + LOAD_STATES.map(col =>
    '<span class="lg tip" data-tip="' + esc(tipText(col.tip)) + '">' +
    '<i class="seg-' + col.field + '"></i>' + esc(t(col.id)) + '</span>').join('') +
  '</div>';

function renderLoad(h) {
  const ld = (h && h.load) || {};
  const rows = ld.hosts || [];
  if (!rows.length) return '';
  const total = ld.total || {};
  const keyed = !!adminKey();
  /* Ohne Schlüssel ist der Knopf aus und sagt warum — dieselbe Regel wie bei
     Calibration und Maintenance: der Schlüssel liegt in
     sessionStorage['ripple_admin'], die Beschriftung ist ein Tooltip mit Quelle,
     die Erklärung des Griffs steht im `title`. */
  const resetBtn = '<button class="btn load-reset" onclick="loadReset()"' +
    (keyed && !loadBusy ? '' : ' disabled') +
    ' title="' + esc(keyed ? t('load.reset_tip') : keyHelp()) + '">' +
    T('load_reset', esc(t('load.reset'))) + '</button>';
  const why = keyed ? '' : '<div class="maint-why">' + esc(keyHelp()) + '</div>';
  const trouble = loadError ? '<div class="maint-why">' + esc(loadError) + '</div>' : '';

  /* Die Knotenspalte hat eine feste Breite, weil die Tabelle `table-layout:fixed`
     erbt und die Spalte sonst den Rest gleichmaessig teilt: Ungarisch
     "Csomópont" braucht 90 px, bekam in der schmalen Spalte von 2.1 aber 69 und
     lief in die RQ-Spalte (#53). 96 px deckt alle sechzehn Sprachen. */
  const head = cells => '<thead><tr><th style="width:96px"><span class="th-main">' +
    esc(t('dist.th_node')) + '</span></th>' + cells + '</tr></thead>';
  /* Kopfzelle: Kürzel oben, Einheit darunter (#37). Die Einheiten sind Symbole
     (`s`, `Tok`) wie `h`/`min` in `loadDur` — sie werden nicht übersetzt.
     Seit 21.11.2026 trägt auch der Kopf den Tooltip: `PTK`, `CTK` und `RQ`
     erschließen sich nur dem, der die Abkürzung schon kennt, und die Erklärung
     stand bisher allein an der Zahl darunter (Betreiber-Fund). Es ist derselbe
     Text wie an der Zelle — keine zweite Quelle, kein zweiter Satz, der altern
     kann. */
  const th = (id, unit, tipKey) => '<th class="num' + (tipKey ? ' tip' : '') + '"' +
    (tipKey ? ' data-tip="' + esc(tipText(tipKey)) + '"' : '') +
    '><span class="th-main" style="white-space:normal">' + esc(t(id)) + '</span>' +
    (unit ? '<span class="th-sub">' + esc(unit) + '</span>' : '') + '</th>';

  /* `group-start` zieht die Trennlinie über die Summenzeile — die eine
     Auszeichnung, die die Tabelle braucht, und sie steht schon im Style Sheet. */
  const rowOpen = (row, kind) =>
    '<tr' + (row.name ? '' : ' class="group-start"') + ' data-key="' + kind + '-' +
    esc(row.name || 'total') + '"><td>' + esc(row.name || t('load.total')) + '</td>';
  const barRows = rows.concat([{...total, name: null}])
    .map(row => loadRow(row, 'time')).join('');

  /* Der Umschalter über den Balken (#39): eine Ansicht, kein Schreibgriff —
     deshalb ein Knopf mit `aria-pressed` und ohne Rückfrage. Die Beschriftung
     wechselt mit dem Zustand, damit man nicht am Balken ablesen muss, worauf
     die Prozente sich gerade beziehen. */
  const idleToggle = '<span class="load-tools">' +
    '<button class="btn load-toggle" onclick="loadToggleIdle()" aria-pressed="' +
    (loadHideIdle ? 'true' : 'false') + '" title="' + esc(tipText('load_hide_idle')) +
    '">' + esc(t(loadHideIdle ? 'load.show_idle' : 'load.hide_idle')) + '</button></span>';
  const workRows = rows.concat([{...total, name: null}]).map(row =>
    rowOpen(row, 'work') + LOAD_WORK.map(col => '<td class="num">' +
      T(col.tip, loadValue(row[col.field], col.unit)) + '</td>').join('') +
    '</tr>').join('');

  /* `since` trägt beides: den Anfang des Fensters und seine Länge. Zwei Zahlen
     in einem Satz statt einer zweiten Beschriftung, die es in acht Sprachen
     nicht gibt. */
  const since = loadWhen(ld.since || ld.started_at || 0) + ' · ' +
                loadDur(ld.window_seconds);
  const noUsage = Number(total.requests_without_usage || 0);

  /* Die Gegenprobe: die Beschriftung der drei Zahlen einmal oben, darunter je
     Knoten ein Tripel und die Summe. Dieselben Wörter wie die Spaltenköpfe
     darüber — es sind dieselben drei Größen. */
  const cross = ld.cross || {};
  const crossRows = cross.hosts || [];
  const triple = row => nf(row.requests) + ' · ' + loadDur(row.prefill_seconds) +
    ' · ' + loadDur(row.generation_seconds);
  const crossLine = crossRows.length
    ? '<div class="dist-note tip" data-tip="' + esc(tipText('load_cross')) + '"' +
      (cross.error ? ' title="' + esc(cross.error) + '"' : '') + '>' +
      esc(t('load.cross', {range: cross.range || 'today'})) + ' · ' +
      [t('load.requests'), t('load.prefill_s'), t('load.decode_s')]
        .map(word => esc(word)).join(' · ') + '<br>' +
      crossRows.map(r => esc(r.name) + ' ' + triple(r)).join('   ·   ') +
      '   ·   ' + esc(t('load.total')) + ' ' + triple(cross.total || {}) + '</div>'
    : '';

  /* Zwei Spalten (#40): der Balken braucht die Breite, die Arbeitstabelle nicht.
     Untereinander war der Block 1400 px breit für drei Zahlen je Knoten; auf
     schmalen Fenstern (≤ 900 px) steht beides wieder untereinander. */
  return `
    <div class="load-split">
      <div class="load-main">
        <div class="loadbars">${barRows}</div>
        <div class="load-legendrow">${loadLegend()}${idleToggle}</div>
      </div>
      <div class="load-side">
        <table class="load-work">
          ${head(LOAD_WORK.map(col => th(col.id, LOAD_UNIT[col.unit], col.tip)).join(''))}
          <tbody>${workRows}</tbody>
        </table>
      </div>
    </div>
    <div class="log-foot">
      <span>${T('load_since', esc(t('load.since', {t: since})))}</span>
      ${noUsage > 0 ? '<span>' + T('load_no_usage',
        esc(t('load.no_usage', {n: nf(noUsage)}))) + '</span>' : ''}
      <span style="margin-left:auto">${resetBtn}</span>
    </div>
    ${why}${trouble}
    ${crossLine}`;
}

/* Ohne Schlüssel sagt der Knopf warum; mit Schlüssel setzt er die Zählung auf
   null und liest sie gleich wieder — sonst stünden für bis zu zwei Sekunden die
   alten Zahlen unter dem neuen `since`. */
async function loadReset() {
  if (loadBusy) return;
  if (!adminKey()) { loadError = keyHelp(); repaintLoad(); return; }
  loadBusy = true;
  loadError = '';
  repaintLoad();
  try {
    await adminApi('POST', '/admin/stats/reset');
    await tick();
  } catch (e) {
    loadError = e.status === 401 ? keyHelp() : e.message;
  } finally {
    loadBusy = false;
    repaintLoad();
  }
}

const repaintLoad = () => { if (last) apply('load', renderLoad(last.h)); };

/* Der Leerlauf-Umschalter: kein Serveraufruf, nur ein neues Zeichnen mit anderem
   Rechenfenster. */
function loadToggleIdle() { loadHideIdle = !loadHideIdle; repaintLoad(); }

/* Derselbe Zustand ohne Zeichnen — für Messungen, die den Block als Zeichenkette
   prüfen wollen (dieselbe Rolle wie `setDim`). */
const loadSetHideIdle = value => { loadHideIdle = !!value; };

/* ── 4 Entscheidungen ─────────────────────────────────────────────────────── */
/* The filter state (issue #101) is module scope, not DOM: the bar itself lives
   in the shell above `#log`, so the 2 s patch of the log cannot reach the field
   the operator is typing in. `logSetFilter` is the only writer; `renderLog`
   reads it. */
const logFilter = {sid: '', reason: '', host: ''};

/* The short form of a thread key for the narrow column. `sid:` comes off, the
   family marker (`gwk_`, `local-`) stays whole, the id is cut to eight
   characters — and a `#<fp8>` suffix survives untouched, because it is what
   tells two work units of one conversation apart (#92). A key without a marker
   is cut to twelve. The full key stays in `title` and in the tooltip. */
function logShortSid(value) {
  if (value === null || value === undefined || value === '') return '—';
  let key = String(value);
  if (key.slice(0, 4).toLowerCase() === 'sid:') key = key.slice(4);
  const unit = key.indexOf('#');
  const family = unit >= 0 ? key.slice(0, unit) : key;
  const suffix = unit >= 0 ? key.slice(unit) : '';
  /* A marker longer than six characters is not a marker: a Hermes sid is
     `20261117_150500_…`, and keeping that whole would make the cell wider than
     the column it exists for. */
  const found = /^([A-Za-z0-9]+[_-])/.exec(family);
  const head = found && found[1].length <= 6 ? found[1] : '';
  return head + family.slice(head.length, head.length + (head ? 8 : 12)) + suffix;
}

/* One row against the three filters: a substring on the raw `thread` value
   (typing `sid:` or a fragment of the id both work, case-insensitively), an
   exact match on the reason token and on the host name. */
function logMatch(x) {
  const sid = logFilter.sid.trim().toLowerCase();
  if (sid) {
    const raw = x.thread === null || x.thread === undefined ? '' : String(x.thread);
    if (raw.toLowerCase().indexOf(sid) === -1) return false;
  }
  if (logFilter.reason && x.reason !== logFilter.reason) return false;
  if (logFilter.host && (x.host || '') !== logFilter.host) return false;
  return true;
}

/* The single writer of `logFilter`: set, then repaint the one container below
   the bar. `patch` is partial, so a keystroke in the sid field leaves the two
   selects alone. */
function logSetFilter(patch) {
  if (patch && typeof patch === 'object') {
    if ('sid' in patch) logFilter.sid = patch.sid === null || patch.sid === undefined ? '' : String(patch.sid);
    if ('reason' in patch) logFilter.reason = patch.reason ? String(patch.reason) : '';
    if ('host' in patch) logFilter.host = patch.host ? String(patch.host) : '';
  }
  if (last) apply('log', renderLog(last.s));
  return logFilter;
}

/* The two selects offer what the ring buffer actually holds — the reason tokens
   and the host names of the payload, plus the "all" option the shell carries.
   The list is rewritten only when it changes: a rebuild while the dropdown is
   open would close it, and the 2 s tick must not do that. A chosen value that
   left the buffer stays as its own option, so the select keeps saying which
   filter is in force. */
function fillLogSelect(id, field, values) {
  const el = $(id);
  if (!el) return;
  const seen = {};
  const list = [];
  for (const v of values) {
    if (v === null || v === undefined || v === '' || seen[v]) continue;
    seen[v] = 1;
    list.push(String(v));
  }
  list.sort();
  const chosen = logFilter[field];
  if (chosen && list.indexOf(chosen) === -1) list.push(chosen);
  const shape = lang + '\u0000' + list.join('\u0000');
  if (el.__shape === shape) { el.value = chosen; return; }
  el.__shape = shape;
  el.innerHTML = '<option value="">' + esc(t('log.filter_all')) + '</option>' +
    list.map(v => '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
  el.value = chosen;
}

function fillLogFilters(s) {
  const rows = (s && s.decisions) || [];
  fillLogSelect('logFilterReason', 'reason', rows.map(x => x.reason));
  fillLogSelect('logFilterHost', 'host', rows.map(x => x.host));
}

function renderLog(s) {
  const all = s.decisions || [];
  /* Newest first (issue #101). `decisions` is the payload's array, oldest
     first, and it is the same object the page keeps for the next re-render —
     so the reversal happens on a copy. Everything below describes the rows the
     filter left visible; `all.length` is the ring buffer they come from. */
  const d = all.slice().reverse().filter(logMatch);
  const max = Math.max(...d.map(x => x.duration_ms || 0), 1);
  const durs = d.map(x => x.duration_ms).filter(v => typeof v === 'number').sort((a, b) => a - b);
  const med = durs.length ? durs[Math.floor(durs.length / 2)] : null;
  const p90 = durs.length ? durs[Math.min(durs.length - 1, Math.floor(durs.length * 0.9))] : null;
  const bad = d.filter(x => x.status >= 400).length;

  const rows = d.map(x => {
    const cls = x.status >= 500 ? 'bad' : (x.status >= 400 ? 'warn' : '');
    /* Der Grund ist ein Token (`thread`, `rebind`, …) und bleibt als Token
       stehen; `reason.<token>` erklärt ihn. Ein unbekanntes Token fällt auf
       sich selbst zurück. */
    const whyId = 'reason.' + x.reason;
    const why = t(whyId) === whyId ? x.reason : t(whyId);
    /* `data-label` an jeder Zelle (#134): unter 700 px steht die Zeile als
       Block, die Kopfzeile ist dann fort — jeder Wert trägt seinen Bezeichner
       selbst, aus demselben Katalog wie `thead` (`log.th_*`), keine neuen Ids.
       Die Zeit ist `log.th_time`, nicht die Unterzeile `th_time_sub`
       („Ortszeit"): im Block ist „Zeit" die Beschriftung, die trägt. */
    return `<tr data-key="${esc([x.time, x.model, x.host, x.reason, x.duration_ms, x.status, x.thread].join('|'))}">
      <td class="t num" data-label="${esc(t('log.th_time'))}">${T('d_time', esc(logTime(x)))}</td>
      <td class="m" data-label="${esc(t('log.th_model'))}" title="${esc(x.model)}">${T('d_model', esc(x.model))}</td>
      <td class="sid" data-label="${esc(t('log.th_sid'))}" title="${esc(x.thread || '—')}" data-tip="${esc((x.thread || '—') + '  ' + t('common.source') + ': /stats → decisions[].thread')}">${esc(logShortSid(x.thread))}</td>
      <td data-label="${esc(t('log.th_node'))}">${T('d_host', esc(x.host || '—'))}</td>
      <td data-label="${esc(t('log.th_reason'))}"><span class="chip" data-tip="${esc(t('log.reason_tip', {why: why}) + '  ' + t('common.source') + ': /stats → decisions[].reason')}">${esc(x.reason)}</span></td>
      <td class="num" data-label="${esc(t('log.th_prompt'))}">${T('d_prompt', nf(x.prompt_chars))}</td>
      <td class="num" data-label="${esc(t('log.th_dur'))}"><div class="dur">${T('d_dur', nf(x.duration_ms, 1))}
        ${T('d_bar', '<span class="dur-track"><i style="width:' + ((x.duration_ms || 0) / max * 100).toFixed(0) + '%"></i></span>')}
      </div></td>
      <td class="num" data-label="${esc(t('log.th_status'))}"><span class="st ${cls}">${T('d_status', nf(x.status))}</span></td>
    </tr>`;
  }).join('');

  /* Measured maxima across the sixteen catalogs: Greek needs 92 px for
     "Κατάσταση" — the column was 74 px and the panel answered with a horizontal
     scrollbar (#53) — 81 for the prompt length, Russian 126 for the duration.
     The widths live in `dashboard.css` as `col.c-*` since #101: the eighth
     column (sid) needs a narrower raster at 741 px, and an inline width cannot
     be overridden by the media query that does it. */
  const body = d.length ? rows : `<tr class="log-none"><td colspan="8">${esc(t('log.filter_none'))}</td></tr>`;
  return `
    <table>
      <colgroup>
        <col class="c-time"><col class="c-model"><col class="c-sid"><col class="c-node">
        <col class="c-reason"><col class="c-prompt"><col class="c-dur"><col class="c-status">
      </colgroup>
      <thead>
        <tr>
          <th><span class="th-main">${esc(t('log.th_time'))}</span><span class="th-sub">${esc(t('log.th_time_sub'))}</span></th>
          <th><span class="th-main">${esc(t('log.th_model'))}</span></th>
          <th><span class="th-main">${esc(t('log.th_sid'))}</span></th>
          <th><span class="th-main">${esc(t('log.th_node'))}</span></th>
          <th><span class="th-main">${esc(t('log.th_reason'))}</span><span class="th-sub">${esc(t('log.th_reason_sub'))}</span></th>
          <th class="num"><span class="th-main">${esc(t('log.th_prompt'))}</span><span class="th-sub">${esc(t('log.th_prompt_sub'))}</span></th>
          <th class="num"><span class="th-main">${esc(t('log.th_dur'))}</span><span class="th-sub">${esc(t('log.th_dur_sub'))}</span></th>
          <th class="num"><span class="th-main">${esc(t('log.th_status'))}</span></th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>
    <div class="log-foot">
      <span>${T('d_count', '<b>' + nf(d.length) + '</b>')} ${esc(t('log.count_suffix'))} · ${esc(t('fleet.of'))} ${T('d_count', nf(all.length))}</span>
      <span>${esc(t('log.median'))} ${T('d_med', '<b>' + nf(med, 1) + ' ms</b>')}</span>
      <span>${esc(t('log.p90'))} ${T('d_med', '<b>' + nf(p90, 1) + ' ms</b>')}</span>
      <span>${T('d_status', '<b>' + nf(bad) + '</b>')} ${esc(t('log.error_suffix'))}</span>
      <span>${esc(t('log.oldest'))} ${T('d_time', esc(d.length ? logTime(d[d.length - 1]) : '—'))} ${esc(t('log.newest'))} ${T('d_time', esc(d.length ? logTime(d[0]) : '—'))}</span>
    </div>`;
}

/* ── Parameter: value in effect, where it comes from, reset per row (#78) ─── */
/* The block used to be a list of what is in force and said so in its own words:
   "changing them means: edit the configuration file and restart". That is right
   for identity and transport and wrong for routing policy. Since #78 the table
   carries three columns behind its two labels — the value in effect, where it
   comes from (`config.yaml` or `set at the page`) and a reset control per row —
   and the policy paths the server lists in `editable` are fields.

   The origin is derived, never hardcoded: a row whose dotted path is in
   `overrides` was set at the page, everything else comes from the file. A
   backend that sends neither key (an older one) gets the table of before: no
   third column, no field, no reset, no error. */

/* `hosts.*.weight` covers every host row, `affinity.ttl_seconds` is one path. */
function paramsPattern(pattern, path) {
  const a = String(pattern).split('.');
  const b = String(path).split('.');
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== '*' && a[i] !== b[i]) return false;
  return true;
}

const paramsEditable = (path, editable) =>
  !!path && (editable || []).some(p => paramsPattern(p, path));

const paramsOverridden = (overrides, path) =>
  !!path && !!overrides && Object.prototype.hasOwnProperty.call(overrides, path);

/* The number the operator typed, read the way the page writes it: `0,9` on the
   German page and `0.9` on the English one are the same value, `1.800` and
   `1,800` the same thousand. The decimal mark comes from the formatter the
   cells use, not from a second list of locales. */
const paramsDecimal = () => (nf(1.5, 1).indexOf(',') === -1 ? '.' : ',');

function paramsRead(text, kind) {
  const raw = String(text === null || text === undefined ? '' : text).trim();
  if (kind === 'bool') return raw === 'true' || raw === 'on';
  let s = raw.replace(/[\s\u00a0\u202f\u2009']/g, '');
  if (paramsDecimal() === ',') s = s.replace(/\./g, '').replace(',', '.');
  else s = s.replace(/,/g, '');
  return s === '' ? NaN : Number(s);
}

/* The loader's own message, verbatim: a 400 answers `{"error": "invalid_value",
   "path": …, "message": …}` — the sentence is the field's, not ours. */
function paramsMessage(e) {
  const d = (e && e.data) || {};
  if (typeof d.message === 'string' && d.message) return d.message;
  if (d.error && typeof d.error.message === 'string' && d.error.message) return d.error.message;
  return (e && e.message) || '';
}

/* The last refusal, per path: it is drawn next to the field it belongs to and
   survives the 2 s patch until the next write. */
let paramsErr = null;

/* Die Scope-Zelle trägt links den Knoten-Streifen (#131): in der Flottenkarte
   steht er am Kartenrand, hier im Textfluss vor dem Namen. Beide Zeilenarten
   bekommen denselben 6-px-Platz — eine Knotenzeile den Streifen, `global`,
   `placement`, `affinity` & Co. einen leeren Slot gleicher Breite. Ohne den
   leeren Slot würde der Text der Nicht-Knoten-Zeilen um genau diese Breite
   springen, und die Spalte wäre keine Spalte mehr. Der Streifen ist
   `aria-hidden` und trägt keinen Text: er ist eine Marke, keine Auskunft — den
   Namen liest man daneben. */
function paramsSlot(host) {
  return host ? hostStripe(host) : '<i class="pslot" aria-hidden="true"></i>';
}

/* One row: scope · parameter · value in effect · origin · reset. The `data-key`
   carries the value and the origin, so the reconciler replaces a row whose
   number changed on the server and leaves a row alone while the operator is
   typing in it. */
function paramsRow(scope, label, value, o) {
  const path = o.path || '';
  const tip = esc(o.keyed ? t('params.edit_tip') : keyHelp());
  const lock = o.keyed ? '' : ' disabled';
  let cell = esc(value);
  if (o.edit) {
    const words = o.words || ['common.yes', 'common.no'];
    const field = o.kind === 'bool'
      ? '<select class="pedit" data-path="' + esc(path) + '" data-kind="bool"' +
        ' aria-label="' + esc(label) + '" data-tip="' + tip + '"' + lock + '>' +
        '<option value="true"' + (o.raw ? ' selected' : '') + '>' + esc(t(words[0])) + '</option>' +
        '<option value="false"' + (o.raw ? '' : ' selected') + '>' + esc(t(words[1])) + '</option>' +
        '</select>'
      : '<input class="pedit" type="text" inputmode="decimal" autocomplete="off"' +
        ' spellcheck="false" data-path="' + esc(path) + '" data-kind="num"' +
        ' aria-label="' + esc(label) + '" data-tip="' + tip + '"' +
        ' value="' + esc(fmtNum(o.raw)) + '"' + lock + '>';
    cell = '<span class="pval">' + cell + '</span> ' + field;
  }
  if (o.why) cell += '<span class="pwhy">' + esc(o.why) + '</span>';
  if (paramsErr && paramsErr.path === path) {
    cell += '<span class="perr">' + esc(paramsErr.message) + '</span>';
  }

  const src = o.over
    ? '<span class="psrc page" data-tip="' + esc(t('params.src_page_tip')) + '">' +
      esc(t('params.src_page')) + '</span>'
    : '<span class="psrc">' + esc(t('params.src_file')) + '</span>';

  const reset = o.over
    ? '<button class="btn pbtn" type="button" data-path="' + esc(path) + '"' +
      ' data-tip="' + esc(t('params.reset_tip', {path: path})) + '"' + lock + '>' +
      esc(t('params.reset')) + '</button>'
    : '';

  const key = (path || String(label)) + '|' + String(o.raw) + '|' + (o.over ? 'page' : 'file');
  /* `data-label` an jeder Zelle (#134): unter 700 px ist die Zeile ein Block,
     und die Spaltenüberschrift, die den Wert erklärt hätte, ist dann weg. Jede
     Zelle trägt ihren Bezeichner selbst — dieselben Wörter wie `thead`, aus
     demselben Katalog, keine neuen Ids. Die Klassen (`pname`, `pv`, `ps`, `pr`)
     sind der Griff, an dem das Block-Styling die Zeilen formt. */
  return '<tr data-key="' + esc(key) + '">' +
         '<td class="pscope" data-label="' + esc(t('params.th_scope')) + '">' +
         paramsSlot(o.host) + esc(scope) + '</td>' +
         '<td class="pname" data-label="' + esc(t('params.th_param')) + '">' +
           (o.help ? '<span class="tip" data-tip="' + esc(o.help) + '">' + esc(label) + '</span>'
                   : esc(label)) +
         '</td>' +
         '<td class="pv" data-label="' + esc(t('params.th_value')) + '">' + cell + '</td>' +
         '<td class="ps" data-label="' + esc(t('params.th_source')) + '">' + src + '</td>' +
         '<td class="pr" data-label="' + esc(t('params.th_reset')) + '">' + reset + '</td></tr>';
}

function paramsTableLive(c, overrides, editable) {
  const rows = [];
  const keyed = !!adminKey();
  const onoff = b => b ? t('common.on') : t('common.off');
  const yesno = b => b ? t('common.yes') : t('common.no');
  /* Der erklärende Satz kommt aus dem Config-Schema (`GET /config → help`,
     #139): dort steht er neben dem Feld, das er beschreibt, und kann beim
     Umbenennen eines Feldes nicht still falsch werden. Host-Pfade tragen den
     Knotennamen, die Tabelle führt sie mit `*` — deshalb die zweite Suche. */
  const helpMap = c.help || {};
  const helpOf = path => {
    if (!path) return '';
    return helpMap[path] || helpMap[String(path).replace(/^hosts\.[^.]+\./, 'hosts.*.')] || '';
  };
  const add = (scope, label, value, opts) => {
    const o = Object.assign({keyed: keyed}, opts || {});
    o.help = helpOf(o.path);
    /* Both derived from the two keys the server sends, never hardcoded: a row
       is a field when its path matches an `editable` pattern, and it was set at
       the page when its path is in `overrides`. */
    o.edit = paramsEditable(o.path, editable);
    o.over = paramsOverridden(overrides, o.path);
    rows.push(paramsRow(scope, label, value, o));
  };

  /* Restart-bound: identity and transport. Shown with the reason instead of an
     input — the page cannot change them without dropping every in-flight
     request, so it says so rather than pretending. */
  const restart = t('params.restart_bound');

  add('global', t('params.file'), c.source || '—');
  add('global', t('params.listen'), c.listen, {path: 'listen', why: restart});
  add('global', t('params.log_level'), (c.log_level || '—') + ' · ' + (c.log_file || '—'),
      {path: 'log_level', why: restart});

  const p = c.placement || {};
  add('placement', 'binding strategy', p.binding_strategy, {path: 'placement.binding_strategy'});
  add('placement', 'balance_abs_threshold', fmtNum(p.balance_abs_threshold),
      {path: 'placement.balance_abs_threshold', kind: 'num', raw: p.balance_abs_threshold});
  add('placement', 'balance_rel_threshold', fmtNum(p.balance_rel_threshold),
      {path: 'placement.balance_rel_threshold', kind: 'num', raw: p.balance_rel_threshold});
  const rb = p.rebind || {};
  add('placement', t('params.rebind'), onoff(rb.enabled),
      {path: 'placement.rebind.enabled', kind: 'bool', raw: !!rb.enabled,
       words: ['common.on', 'common.off']});
  add('placement', 'rebind.max_per_hour', fmtNum(rb.max_per_hour) + '/h',
      {path: 'placement.rebind.max_per_hour', kind: 'num', raw: rb.max_per_hour});
  add('placement', 'rebind.safety_factor', '≥ ' + fmtNum(rb.safety_factor) + '×',
      {path: 'placement.rebind.safety_factor', kind: 'num', raw: rb.safety_factor});
  add('placement', 'rebind.per_thread_cooldown_seconds',
      fmtNum(rb.per_thread_cooldown_seconds) + ' s',
      {path: 'placement.rebind.per_thread_cooldown_seconds', kind: 'num',
       raw: rb.per_thread_cooldown_seconds});
  add('placement', t('params.idle_decay'),
      (p.thread_decay_seconds === undefined ? '—' : t('params.quadratic', {v: fmtNum(p.thread_decay_seconds)})),
      {path: 'placement.thread_decay_seconds'});

  const L = p.loading || {};
  add('placement', 'loading',
      onoff(L.enabled !== false) +
      ' · safety ×' + fmtNum(L.safety_factor) +
      ' · max ' + fmtNum(L.max_seconds) + ' s' +
      ' · ' + t('params.samples_per_node', {n: fmtNum(L.samples)}) +
      ' · calibration_file ' + (L.calibration_file || '—'));
  add('placement', 'flap_guard_failures',
      t('params.flap', {n: p.flap_guard_failures === null || p.flap_guard_failures === undefined
                             ? '—' : fmtNum(p.flap_guard_failures)}),
      {path: 'placement.flap_guard_failures'});

  const a = c.affinity || {};
  add('affinity', t('params.session_header'), a.header, {path: 'affinity.header', why: restart});
  add('affinity', t('params.ttl'),
      t('params.ttl_minutes', {v: fmtNum(a.ttl_seconds), m: fmtNum(Math.round(a.ttl_seconds / 60))}),
      {path: 'affinity.ttl_seconds', kind: 'num', raw: a.ttl_seconds});
  add('affinity', t('params.batch_ttl'),
      (a.batch_ttl_seconds === undefined ? '—' : fmtNum(a.batch_ttl_seconds) + ' s'),
      {path: 'affinity.batch_ttl_seconds'});
  add('affinity', t('params.prompt_tokens'), fmtNum(a.min_prompt_tokens),
      {path: 'affinity.min_prompt_tokens'});
  add('affinity', t('params.prefix_match_label'),
      t('params.prefix_match', {ratio: fmtNum(a.match_threshold), chars: fmtNum(a.prefix_chars)}),
      {path: 'affinity.match_threshold', kind: 'num', raw: a.match_threshold});
  add('affinity', t('params.ephemeral'), fmtNum(a.ephemeral_window_seconds) + ' s',
      {path: 'affinity.ephemeral_window_seconds', kind: 'num', raw: a.ephemeral_window_seconds});

  const d = c.discovery || {};
  add('discovery', t('params.interval'),
      fmtNum(d.interval_seconds) + ' s / ' + fmtNum(d.timeout_seconds) + ' s',
      {path: 'discovery.interval_seconds'});
  add('discovery', 'unhealthy after (legacy)',
      t('params.unhealthy_after', {n: fmtNum(d.unhealthy_after)}),
      {path: 'discovery.unhealthy_after'});

  const x = c.proxy || {};
  add('proxy', t('params.retry'), yesno(x.retry_other_node), {path: 'proxy.retry_other_node'});
  add('proxy', 'request log', onoff(x.request_log), {path: 'proxy.request_log'});
  add('proxy', 'connect timeout', fmtNum(x.connect_timeout_seconds) + ' s',
      {path: 'proxy.connect_timeout_seconds'});

  for (const host of (c.hosts || [])) {
    const hp = 'hosts.' + host.name + '.';
    const models = host.models || [];
    /* Jede Zeile dieses Knotens trägt seinen Streifen (#131) — der Datensatz
       kommt aus der Antwort, nicht aus `{name}`: `/config` führt dieselbe
       `node_id` wie `/healthz`, und genau daraus fällt der Platz im
       Flottenblock. Ein selbst gebauter Knoten hätte den Namen als Schlüssel
       und bekäme einen anderen Ton als seine Karte. */
    const row = (label, value, opts) =>
      add(host.name, label, value, Object.assign({host: host}, opts || {}));
    row(t('params.weight'), fmtNum(host.weight) + ' → ' + pctP(host.share),
        {path: hp + 'weight', kind: 'num', raw: host.weight});
    row(t('params.max'), fmtNum(host.max_inflight) + ' / ' + fmtNum(host.max_threads),
        {path: hp + 'max_inflight', why: restart});
    row(t('params.spill'), fmtNum(host.spill_threshold),
        {path: hp + 'spill_threshold', kind: 'num', raw: host.spill_threshold});
    row(t('params.hard'), fmtNum(host.hard_threshold),
        {path: hp + 'hard_threshold', why: restart});
    row(t('params.prefer_resident'), yesno(host.prefer_resident),
        {path: hp + 'prefer_resident', kind: 'bool', raw: !!host.prefer_resident});
    row(t('params.pp_rate'),
        host.pp_tps === null || host.pp_tps === undefined
          ? t('params.pp_unmeasured') : t('params.pp_cold', {v: fmtNum(host.pp_tps)}));
    row('allow list', fmtNum(models.length) + ' — ' + models.join(', '),
        {path: hp + 'models', why: restart});
    row(t('params.api_key'), host.api_key, {path: hp + 'api_key', why: restart});
  }

  const head = '<thead><tr><th>' + esc(t('params.th_scope')) + '</th><th>' + esc(t('params.th_param')) +
    '</th><th>' + esc(t('params.th_value')) + '</th><th>' + esc(t('params.th_source')) +
    '</th><th>' + esc(t('params.th_reset')) + '</th></tr></thead>';
  /* Without a key the fields are off and the block says why, in the same
     sentence the other operator blocks use (`key.help`). */
  const why = keyed ? '' : '<div class="params-why">' + esc(keyHelp()) + '</div>';
  return why + '<table class="ptable"><colgroup>' +
    '<col class="c-scope"><col class="c-param"><col class="c-value">' +
    '<col class="c-source"><col class="c-reset"></colgroup>' + head +
    '<tbody>' + rows.join('') + '</tbody></table>';
}

/* An older backend: `editable` and `overrides` are absent, so the block is the
   read-only list it was before this issue — same columns, same formatting, no
   input and no empty column. */
function paramsTableLegacy(c) {
  const rows = [];
  /* Dieselbe Scope-Zelle wie in der Tabelle mit Feldern (#131): der leere Slot
     steht auch hier, sonst wäre die Spalte in der einen Tabelle eine andere als
     in der anderen. `data-label` an beiden Bezeichnerzellen wie dort (#134):
     unter 700 px wird auch diese Liste zu Blöcken. Die Wertzelle bleibt
     absichtlich ein nacktes `<td>`: `tests/dashboard_dom_stub.js` misst die
     Zahlen dieser Tabelle über `<td>…</td>` (deutsch/englisch dieselben Werte),
     und das Stub gehört nicht zu diesem Issue. Im Block erkennt das Styling sie
     als `td:last-child:not(.pr)` — sie ist die einzige Zelle hinter den zwei
     Bezeichnern. */
  const add = (scope, k, v, host) =>
    rows.push(`<tr><td class="pscope" data-label="${esc(t('params.th_scope'))}">` +
              `${paramsSlot(host)}${esc(scope)}</td>` +
              `<td class="pname" data-label="${esc(t('params.th_param'))}">${esc(k)}</td>` +
              `<td>${esc(v)}</td></tr>`);
  const onoff = b => b ? t('common.on') : t('common.off');
  const yesno = b => b ? t('common.yes') : t('common.no');

  add('global', esc(t('params.file')), c.source || '—');
  add('global', esc(t('params.listen')), c.listen);
  add('global', esc(t('params.log_level')), c.log_level + ' · ' + (c.log_file || '—'));

  const p = c.placement || {};
  add('placement', 'binding strategy', p.binding_strategy);
  add('placement', 'hysteresis abs / rel', fmtNum(p.balance_abs_threshold) + ' / ' + fmtNum(p.balance_rel_threshold));
  add('placement', esc(t('params.rebind')),
      !p.rebind || !p.rebind.enabled
        ? esc(t('common.off'))
        : esc(t('params.rebind_on', {n: p.rebind.max_per_hour, gain: fmtNum(p.rebind.safety_factor),
                                     cooldown: fmtNum(p.rebind.per_thread_cooldown_seconds)})));
  add('placement', esc(t('params.idle_decay')),
      (p.thread_decay_seconds === undefined ? '—' : esc(t('params.quadratic', {v: fmtNum(p.thread_decay_seconds)}))));

  const L = p.loading || {};
  add('placement', 'loading',
      esc(onoff(L.enabled !== false)) +
      ' · safety ×' + fmtNum(L.safety_factor) +
      ' · max ' + fmtNum(L.max_seconds) + ' s' +
      ' · ' + esc(t('params.samples_per_node', {n: fmtNum(L.samples)})) +
      ' · calibration_file ' + (L.calibration_file || '—'));
  add('placement', 'flap_guard_failures',
      esc(t('params.flap', {n: p.flap_guard_failures === null || p.flap_guard_failures === undefined
                               ? '—' : fmtNum(p.flap_guard_failures)})));

  const a = c.affinity || {};
  add('affinity', esc(t('params.session_header')), a.header);
  add('affinity', esc(t('params.ttl')), esc(t('params.ttl_minutes', {v: fmtNum(a.ttl_seconds), m: fmtNum(Math.round(a.ttl_seconds / 60))})));
  add('affinity', esc(t('params.batch_ttl')),
      (a.batch_ttl_seconds === undefined ? '—' : fmtNum(a.batch_ttl_seconds) + ' s'));
  add('affinity', esc(t('params.prompt_tokens')), fmtNum(a.min_prompt_tokens));
  add('affinity', esc(t('params.prefix_match_label')), esc(t('params.prefix_match', {ratio: fmtNum(a.match_threshold), chars: fmtNum(a.prefix_chars)})));
  add('affinity', esc(t('params.ephemeral')), fmtNum(a.ephemeral_window_seconds) + ' s');

  const d = c.discovery || {};
  add('discovery', esc(t('params.interval')), fmtNum(d.interval_seconds) + ' s / ' + fmtNum(d.timeout_seconds) + ' s');
  add('discovery', 'unhealthy after (legacy)', esc(t('params.unhealthy_after', {n: fmtNum(d.unhealthy_after)})));

  const x = c.proxy || {};
  add('proxy', esc(t('params.retry')), esc(yesno(x.retry_other_node)));
  add('proxy', 'request log', esc(onoff(x.request_log)));
  add('proxy', 'connect timeout', fmtNum(x.connect_timeout_seconds) + ' s');

  for (const host of (c.hosts || [])) {
    add(host.name, esc(t('params.weight')), fmtNum(host.weight) + ' → ' + pctP(host.share), host);
    add(host.name, esc(t('params.max')), fmtNum(host.max_inflight) + ' / ' + fmtNum(host.max_threads), host);
    add(host.name, esc(t('params.spill')), fmtNum(host.spill_threshold), host);
    add(host.name, esc(t('params.hard')), fmtNum(host.hard_threshold), host);
    add(host.name, esc(t('params.prefer_resident')), esc(yesno(host.prefer_resident)), host);
    add(host.name, esc(t('params.pp_rate')),
        host.pp_tps === null || host.pp_tps === undefined
          ? esc(t('params.pp_unmeasured')) : esc(t('params.pp_cold', {v: fmtNum(host.pp_tps)})), host);
    add(host.name, esc(t('params.allow_list')), fmtNum(host.models.length) + ' — ' + host.models.join(', '), host);
    add(host.name, esc(t('params.api_key')), host.api_key, host);
  }

  return '<table><thead><tr><th>' + esc(t('params.th_scope')) + '</th><th>' + esc(t('params.th_param')) +
         '</th><th>' + esc(t('params.th_value')) + '</th></tr></thead>' +
         '<tbody>' + rows.join('') + '</tbody></table>';
}

function renderParams(c) {
  const cfg = c || {};
  const overrides = cfg.overrides && typeof cfg.overrides === 'object' ? cfg.overrides : null;
  const editable = Array.isArray(cfg.editable) ? cfg.editable : null;
  if (!overrides || !editable) return paramsTableLegacy(cfg);
  return paramsTableLive(cfg, overrides, editable);
}

/* ── 6.5 schreiben: PATCH je Pfad, DELETE für die Zeile ───────────────────── */
/* The page builds the request from the row it drew: `PATCH
   /admin/config/<path>` with `{"value": …, "confirm": false}`. A 409 means the
   value moves running threads — the question names the count before anything is
   applied, and only the second call carries `confirm: true` (#78). */

/* The question a weight change asks, in the page's language: the count comes
   from the 409, the sentence from the catalog. */
const paramsWeightQuestion = (n, value) =>
  t('params.weight_confirm', {n: nf(n, 0), w: fmtNum(value)});

async function paramsSubmit(path, value) {
  if (!adminKey()) return {ok: false, status: 0, message: keyHelp()};
  paramsErr = null;
  try {
    const data = await adminApi('PATCH', '/admin/config/' + path,
                                {value: value, confirm: false});
    await tick();
    return {ok: true, status: 200, applied: true, data: data};
  } catch (e) {
    const d = (e && e.data) || {};
    /* 409: the value is legal and would move threads that are running now. Ask
       with the number, then repeat the same PATCH with `confirm: true`. */
    if (e && e.status === 409 && typeof d.threads_that_would_move === 'number') {
      const n = d.threads_that_would_move;
      const ask = paramsWeightQuestion(n, value);
      /* Without a way to ask, nothing is applied: a silent redistribution is
         exactly what this confirmation exists to prevent. */
      const yes = typeof window !== 'undefined' && typeof window.confirm === 'function' &&
                  window.confirm(ask);
      if (!yes) return {ok: false, status: 409, threads: n, applied: false, question: ask};
      try {
        const done = await adminApi('PATCH', '/admin/config/' + path,
                                    {value: value, confirm: true});
        await tick();
        return {ok: true, status: 200, threads: n, applied: true, question: ask, data: done};
      } catch (e2) {
        paramsErr = {path: path, message: paramsMessage(e2)};
        await tick();
        return {ok: false, status: (e2 && e2.status) || 0, message: paramsErr.message,
                threads: n, applied: false, question: ask};
      }
    }
    paramsErr = {path: path, message: paramsMessage(e)};
    await tick();
    return {ok: false, status: (e && e.status) || 0, message: paramsErr.message};
  }
}

async function paramsReset(path) {
  if (!adminKey()) return {ok: false, status: 0, message: keyHelp()};
  paramsErr = null;
  try {
    const data = await adminApi('DELETE', '/admin/config/' + path);
    await tick();
    return {ok: true, status: 200, data: data};
  } catch (e) {
    paramsErr = {path: path, message: paramsMessage(e)};
    await tick();
    return {ok: false, status: (e && e.status) || 0, message: paramsErr.message};
  }
}

/* Enter saves, Escape puts the field back to the value in effect, and the same
   save runs when the field loses focus with a changed value. */
function paramsKey(ev) {
  const el = ev && ev.target && ev.target.closest ? ev.target.closest('.pedit') : null;
  if (!el) return false;
  if (ev.key === 'Escape') {
    const v = el.getAttribute('value');
    if (v !== null) el.value = v;
    if (typeof el.blur === 'function') el.blur();
    return true;
  }
  if (ev.key !== 'Enter') return false;
  if (ev.preventDefault) ev.preventDefault();
  paramsCommit(el);
  return true;
}

async function paramsCommit(el) {
  const path = el.getAttribute('data-path');
  const kind = el.getAttribute('data-kind') || 'num';
  const value = paramsRead(el.value, kind);
  if (kind === 'num' && !isFinite(value)) {
    /* Not a number in this language: do not send a null, say it at the field. */
    paramsErr = {path: path, message: t('params.not_a_number', {v: el.value})};
    if (last) apply('params', renderParams(last.c));
    return {ok: false, status: 0, message: paramsErr.message};
  }
  const res = await paramsSubmit(path, value);
  /* Declined: nothing was written, so the field goes back to the value in
     effect instead of keeping a number the fleet never took. */
  if (res && res.status === 409 && res.applied === false) {
    const back = el.getAttribute('value');
    if (back !== null) el.value = back;
  }
  return res;
}

document.addEventListener('keydown', paramsKey);
document.addEventListener('change', ev => {
  const el = ev && ev.target && ev.target.closest ? ev.target.closest('.pedit') : null;
  if (el) paramsCommit(el);
});
document.addEventListener('click', ev => {
  const btn = ev && ev.target && ev.target.closest ? ev.target.closest('.pbtn') : null;
  if (btn && !btn.disabled) paramsReset(btn.getAttribute('data-path'));
});

/* ── Kalibrierung (Betreiberaktion, admin-Schlüssel) ──────────────────────── */
const calibBusy = {};   // node name → job id while a calibration runs
const calibPost = {};   // node name → the POST answer (models / evicts)

function calibNotice(text, cls) {
  apply('calibPanel', '<div class="' + (cls || 'warn') + '">' + esc(text) + '</div>');
}

function calButtonState(name, disabled) {
  const b = document.querySelector('button[data-node="' + name + '"]');
  if (b) b.disabled = !!disabled;
}

function renderCalibration(name, d, post) {
  const job = (d && d.job) || null;
  const parts = [];
  parts.push(`<div class="calib-head"><strong>${esc(name)}</strong>` +
    `<span class="chip">${esc(t('calib.source', {v: (d && d.source) || '—'}))}</span>` +
    (d && d.calibrated_at ? `<span class="chip">${esc(d.calibrated_at)}</span>` : '') + '</div>');
  if (post) {
    const models = (post.models || []);
    const evicts = (post.evicts || []);
    parts.push(`<div class="calib-line num">${esc(t('calib.job_line', {job: post.job, node: post.node_id || '—'}))}</div>`);
    parts.push(`<div class="calib-line num">${esc(t('calib.loads', {list: models.length ? models.join(', ') : '—'}))}</div>`);
    parts.push(`<div class="calib-line num">${esc(t('calib.may_evict', {list: evicts.length ? evicts.join(', ') : t('calib.nothing')}))}</div>`);
  }
  if (job) {
    const cls = job.state === 'running' ? 'info' : job.state === 'failed' ? 'bad' : 'ok';
    parts.push(`<div class="calib-line num ${cls}">${esc(t('calib.job_state', {state: job.state, step: job.step || '—'}))}</div>`);
  }
  const samples = (d && d.samples) || [];
  if (samples.length) {
    parts.push('<table><thead><tr><th>' + esc(t('calib.th_model')) + '</th><th class="num">GB</th>' +
      '<th class="num">' + esc(t('calib.th_seconds')) + '</th></tr></thead><tbody>' +
      samples.map(s => `<tr><td>${esc(s.model)}</td>` +
        `<td class="num">${nf(s.size_gb, 1)}</td>` +
        `<td class="num">${nf(s.seconds, 1)}</td></tr>`).join('') + '</tbody></table>');
  }
  const f = (d && d.fit) || null;
  if (f) {
    // The intercept stays as measured — a negative value is the interesting
    // number (the fit subtracts a fixed cost), so it is not rounded away.
    parts.push(`<div class="calib-line num">${esc(t('calib.fit', {slope: num(f.slope), intercept: num(f.intercept),
      worst: num(f.worst_slope), linearity: num(f.linearity)}))}</div>`);
    if (f.intercept !== null && f.intercept < 0) {
      parts.push('<div class="note">' + esc(t('calib.negative_intercept')) + '</div>');
    }
  }
  apply('calibPanel', parts.join(''));
}

async function calibrate(name) {
  if (calibBusy[name]) return;                    // one job per node, ever
  const ops = $('ops');
  if (ops) ops.open = true;                       // the job's output lives in there
  const base = '/admin/nodes/' + encodeURIComponent(name);
  if (!adminKey()) { calibNotice(keyHelp(), 'warn'); return; }

  // 1. current state first, so the dialog says what it replaces
  let cur = null;
  try {
    cur = await adminApi('GET', base + '/calibration');
  } catch (e) {
    calibNotice(e.status === 401 ? keyHelp()
      : t('calib.unreadable', {node: name, msg: e.message}), 'bad');
    return;
  }
  const now = t('calib.now_source', {src: (cur && cur.source) || '—'}) +
    (cur && cur.calibrated_at ? t('calib.now_at', {at: cur.calibrated_at}) : t('calib.now_never')) +
    (cur && (cur.samples || []).length ? t('calib.now_samples', {n: cur.samples.length}) : '');

  // 2. the confirmation names the node, the models that will be loaded and what
  //    may be evicted. `plan` is the dry run the GET computes with the same
  //    function as the POST, so the dialog cannot promise something else.
  const plan = (cur && cur.plan) || null;
  if (plan && plan.error) {
    calibNotice(t('calib.not_calibratable', {node: name, err: plan.error}), 'bad');
    return;
  }
  const models = (plan && plan.models) || [];
  const evicts = (plan && plan.evicts) || [];
  const what = models.length ? t('calib.confirm_loads', {list: models.join(', ')}) : '';
  const risk = evicts.length ? t('calib.confirm_evicts', {list: evicts.join(', ')})
                             : t('calib.confirm_nothing');
  const msg = t('calib.confirm', {node: name, what: what, risk: risk, now: now});
  if (!window.confirm(msg)) return;

  // 3. start the job
  calibBusy[name] = 'starting';
  calButtonState(name, true);
  let res = null;
  try {
    res = await adminApi('POST', base + '/calibrate');
  } catch (e) {
    delete calibBusy[name];
    calButtonState(name, false);
    const text = e.status === 409 ? t('calib.busy_409', {msg: e.message})
               : e.status === 401 ? keyHelp()
               : t('calib.start_failed', {msg: e.message});
    calibNotice(text, 'bad');
    return;
  }
  calibBusy[name] = res.job;
  calibPost[name] = res;
  renderCalibration(name, cur, res);

  // 4. poll the state while the job runs
  let d = cur;
  let state = 'running';
  let seen = false;
  let ticks = 0;
  while (state === 'running' && ticks < 900) {
    await sleep(2000);
    ticks++;
    try {
      d = await adminApi('GET', base + '/calibration');
    } catch (e) {
      calibNotice(t('calib.lost', {node: name, msg: e.message}), 'bad');
      break;
    }
    if (d.job) { seen = true; state = d.job.state || 'done'; }
    else if (seen) { state = 'done'; }
    renderCalibration(name, d, res);
  }
  delete calibBusy[name];
  calButtonState(name, false);
}

/* ── Replikation: ein Modell von A nach B (issue #13) ─────────────────────── */
/* Der dritte und letzte Schreibweg der Seite. Derselbe Ablauf wie bei Calibrate:
   Schlüssel prüfen, Plan holen, rückfragen, starten, den Job pollen. Der Plan
   kommt vom Server (`GET /admin/replication/plan`) und wird von derselben
   Funktion berechnet wie das POST — die Rückfrage kann also nichts anderes
   versprechen, als der Job tut. Quelle jedes Feldes steht im `data-tip`. */
const replState = {plan: null, job: null, target: '', source: '', model: '', busy: false};

function replNotice(text, cls) {
  const ops = $('ops');
  if (ops) ops.open = true;
  apply('replPanel', '<div class="' + (cls || 'warn') + '">' + esc(text) + '</div>');
}

function replValue(id) {
  const el = $(id);
  return el && el.value ? el.value : '';
}

function replModels(h, source) {
  return ((h && h.models) || [])
    .filter(m => (m.hosts || []).some(x => x.host === source))
    .map(m => m.id).sort();
}

function replSelect(id, label, values, current, tip) {
  const opts = values.map(v => '<option value="' + esc(v) + '"' +
    (v === current ? ' selected' : '') + '>' + esc(v) + '</option>').join('');
  return '<label class="repl-field tip" data-tip="' + esc(tip) + '">' +
    '<span>' + esc(label) + '</span>' +
    '<select id="' + id + '">' + (opts || '<option value="">—</option>') + '</select></label>';
}

function renderReplication(h) {
  const hosts = ((h && h.hosts) || []).map(x => x.name);
  const source = replState.source || replValue('replSource') || hosts[0] || '';
  const target = replState.target || replValue('replTarget') ||
    hosts.find(n => n !== source) || '';
  const models = replModels(h, source);
  const model = replState.model || replValue('replModel') || models[0] || '';
  const keyed = !!adminKey();
  const parts = [];

  parts.push('<div class="repl-form">' +
    replSelect('replSource', t('repl.source'), hosts, source,
      t('repl.source_tip') + '  ' + t('common.source') + ': /healthz → hosts[].name') +
    replSelect('replModel', t('repl.model'), models, model,
      t('repl.model_tip') + '  ' + t('common.source') + ': /healthz → models[].hosts[].host') +
    replSelect('replTarget', t('repl.target'), hosts.filter(n => n !== source), target,
      t('repl.target_tip') + '  ' + t('common.source') + ': /healthz → hosts[].name') +
    '<label class="repl-field tip" data-tip="' + esc(t('repl.overwrite_tip') + '  ' + t('common.source') + ': /admin/replication/plan → already_there') +
      '"><span>' + esc(t('repl.overwrite')) + '</span>' +
      '<span class="repl-check"><input type="checkbox" id="replOverwrite"' +
      (replState.plan && replState.plan.overwrite ? ' checked' : '') +
      '></span></label>' +
    '<button class="btn" onclick="replicationCheck()"' + (keyed ? '' : ' disabled') +
      '>' + esc(t('repl.check')) + '</button>' +
    '</div>');

  const plan = replState.plan;
  if (plan) {
    const cls = plan.ok ? 'ok' : 'bad';
    parts.push('<div class="calib-head"><strong>' + esc(plan.source) + ' → ' +
      esc(plan.target) + '</strong>' +
      '<span class="chip">' + esc(plan.model) + '</span>' +
      '<span class="chip ' + cls + '">' + esc(plan.ok ? t('repl.copyable') : t('repl.refused')) + '</span></div>');
    parts.push('<div class="calib-line num">' +
      '<span class="tip" data-tip="' + esc(t('repl.size_tip') + '  ' + t('common.source') + ': /admin/replication/plan → size_gb') + '">' +
      gb(plan.size_bytes) + ' GB</span>' +
      ' ' + esc(t('repl.needs', {v: gb(plan.required_bytes)})) +
      ' ' + esc(t('repl.free', {v: gb(plan.free_bytes)})) +
      ' <span class="tip" data-tip="' + esc(t('repl.free_tip') + '  ' + t('common.source') + ': /admin/replication/plan → free_bytes') + '">?</span>' +
      '</div>');
    if (plan.target_path) {
      parts.push('<div class="calib-line num">' + t('repl.to', {path: esc(plan.target_path)}) +
        '<span class="tip" data-tip="' + esc(t('repl.path_tip') + '  ' + t('common.source') + ': /admin/replication/plan → target_path') + '">?</span></div>');
    }
    (plan.errors || []).forEach(e => parts.push('<div class="calib-line bad">' + esc(e) + '</div>'));
    (plan.warnings || []).forEach(w => parts.push('<div class="calib-line warn">' + esc(w) + '</div>'));
    if (plan.ok) {
      parts.push('<div class="repl-actions"><button class="btn" onclick="replicationStart()"' +
        (replState.busy ? ' disabled' : '') + '>' + esc(t('repl.copy')) + '</button></div>');
    }
  }

  const job = replState.job;
  if (job) {
    const cls = job.state === 'running' ? 'info' : job.state === 'failed' ? 'bad'
      : job.state === 'cancelled' ? 'warn' : 'ok';
    parts.push('<div class="calib-line num ' + cls + '">' + esc(t('calib.job_state', {state: job.state, step: job.step || '—'})) + '</div>');
    parts.push('<div class="calib-line num">' +
      '<span class="tip" data-tip="' + esc(t('repl.progress_tip') + '  ' + t('common.source') + ': /admin/nodes/<target>/replication → job.percent') + '">' +
      esc(t('repl.progress', {p: nf(job.percent || 0, 0)})) + '</span>' +
      ' · ' + gb(job.bytes_done) + ' GB' +
      (job.rate ? ' · ' + esc(job.rate) : '') +
      (job.eta ? ' ' + esc(t('repl.eta', {v: job.eta})) : '') + '</div>');
    if (job.reload) {
      parts.push('<div class="calib-line num">' + esc(job.reload.ok ? t('repl.reload_ok') : t('repl.reload_failed')) +
        ': ' + esc(job.reload.message || '—') + '</div>');
    }
    if (job.settings && job.settings.sent) {
      parts.push('<div class="calib-line num">' + esc(t('repl.settings', {sent: nf(job.settings.sent),
        compared: nf(job.settings.compared), diff: nf((job.settings.mismatches || []).length)})) +
        '<span class="tip" data-tip="' + esc(t('repl.settings_tip') + '  ' + t('common.source') + ': /admin/nodes/<target>/replication → job.settings') + '">?</span></div>');
      (job.settings.mismatches || []).slice(0, 8).forEach(m =>
        parts.push('<div class="calib-line bad">' + esc(t('repl.mismatch', {key: m.key,
          src: JSON.stringify(m.source), dst: JSON.stringify(m.target)})) + '</div>'));
    }
    const rep = job.report || {};
    if (job.state !== 'running') {
      parts.push('<div class="calib-line num">' +
        esc(t('repl.report', {bytes: gb(rep.bytes), s: nf(rep.seconds || 0, 1), rate: nf(rep.mb_per_s || 0, 1)})) +
        '<span class="tip" data-tip="' + esc(t('repl.report_tip') + '  ' + t('common.source') + ': /admin/nodes/<target>/replication → job.report') + '">?</span></div>');
      (rep.not_replicated || []).forEach(x =>
        parts.push('<div class="calib-line bad">' + esc(t('repl.not_replicated', {x: x})) + '</div>'));
    }
    if (job.error) parts.push('<div class="calib-line bad">' + esc(job.error) + '</div>');
    if (job.state === 'running') {
      parts.push('<div class="repl-actions"><button class="btn" onclick="replicationCancel()">' + esc(t('repl.cancel')) + '</button>' +
        '<span class="hint"> ' + esc(t('repl.cancel_note')) + '</span></div>');
    }
  }

  if (!keyed) parts.push('<div class="pol-why">' + esc(keyHelp()) + '</div>');
  return parts.join('');
}

async function replicationCheck() {
  if (!adminKey()) { replNotice(keyHelp(), 'warn'); return; }
  const source = replValue('replSource');
  const target = replValue('replTarget');
  const model = replValue('replModel');
  const overwrite = !!($('replOverwrite') && $('replOverwrite').checked);
  if (!source || !target || !model) { replNotice(t('repl.choose'), 'warn'); return; }
  replState.source = source; replState.target = target; replState.model = model;
  replState.job = null;
  const q = '?source=' + encodeURIComponent(source) + '&target=' + encodeURIComponent(target) +
    '&model=' + encodeURIComponent(model) + (overwrite ? '&overwrite=1' : '');
  try {
    replState.plan = await adminApi('GET', '/admin/replication/plan' + q);
  } catch (e) {
    replNotice(e.status === 401 ? keyHelp() : t('repl.check_failed', {msg: e.message}), 'bad');
    return;
  }
  if (last) apply('replPanel', renderReplication(last.h));
}

async function replicationStart() {
  const plan = replState.plan;
  if (!plan || !plan.ok || replState.busy) return;
  if (!adminKey()) { replNotice(keyHelp(), 'warn'); return; }
  // Die Rückfrage nennt Quelle, Ziel, Größe und freien Platz — die vier Dinge,
  // die der Betreiber kennen muss, bevor ein 100-GB-Copy anläuft.
  const free = (plan.free_gb === null || plan.free_gb === undefined) ? '?' : gb(plan.free_bytes);
  const msg = t('repl.confirm', {model: plan.model, src: plan.source, dst: plan.target,
    size: gb(plan.size_bytes), free: free});
  if (!window.confirm(msg)) return;

  replState.busy = true;
  let res = null;
  try {
    res = await adminApi('POST', '/admin/nodes/' + encodeURIComponent(plan.target) + '/replicate',
      {source: plan.source, model: plan.model, overwrite: !!plan.overwrite});
  } catch (e) {
    replState.busy = false;
    replNotice(e.status === 409 ? ('409 — ' + e.message)
      : e.status === 401 ? keyHelp()
      : t('repl.start_failed', {msg: e.message}), 'bad');
    if (last) apply('replPanel', renderReplication(last.h));
    return;
  }
  replState.job = res.job_state || null;
  replState.target = plan.target;
  if (last) apply('replPanel', renderReplication(last.h));

  // Ab hier pollen, bis der Job steht — der Zustand kommt aus dem GET, nicht aus
  // dem POST: ein 202 sagt nur, dass er läuft.
  const base = '/admin/nodes/' + encodeURIComponent(plan.target) + '/replication';
  let ticks = 0;
  while (ticks < 3600) {
    await sleep(2000);
    ticks++;
    try {
      const d = await adminApi('GET', base);
      replState.job = d.job;
    } catch (e) {
      replState.busy = false;
      replNotice(t('repl.lost', {dst: plan.target, msg: e.message}), 'bad');
      return;
    }
    if (last) apply('replPanel', renderReplication(last.h));
    if (!replState.job || replState.job.state !== 'running') break;
  }
  replState.busy = false;
  if (last) apply('replPanel', renderReplication(last.h));
}

async function replicationCancel() {
  if (!replState.target) return;
  if (!window.confirm(t('repl.cancel_confirm', {dst: replState.target}))) return;
  try {
    await adminApi('DELETE', '/admin/nodes/' + encodeURIComponent(replState.target) + '/replication');
    replNotice(t('repl.cancelled'), 'warn');
  } catch (e) {
    replNotice(t('repl.cancel_failed', {msg: e.message}), 'bad');
  }
}

/* ── Wartung: setzen und beenden (issue #10) ──────────────────────────────── */
/* Der Weg ist derselbe wie bei Calibrate: Schlüssel prüfen, Rückfrage, dann
   ein Aufruf über `adminApi()`. Die Rückmeldung landet im Betriebsblock, und
   die Karte ist die eigentliche Antwort — deshalb danach ein `tick()`, statt
   bis zu zwei Sekunden auf den nächsten zu warten. */
function maintNotice(text, cls) {
  const ops = $('ops');
  if (ops) ops.open = true;
  apply('maintPanel', '<div class="' + (cls || 'warn') + '">' + esc(text) + '</div>');
}

async function maintenance(name, minutes) {
  if (!adminKey()) { maintNotice(keyHelp(), 'warn'); return; }
  const span = (minutes === null || minutes === undefined)
    ? t('maint.confirm_endless') : t('maint.confirm_minutes', {n: minutes});
  const msg = t('maint.confirm', {node: name, span: span});
  if (!window.confirm(msg)) return;
  const q = (minutes === null || minutes === undefined)
    ? '' : '?minutes=' + encodeURIComponent(minutes);
  try {
    const res = await adminApi('POST', '/admin/nodes/' + encodeURIComponent(name) + '/maintenance' + q);
    maintNotice(t('maint.set_ok', {node: name, line: maintLine(res)}), 'ok');
  } catch (e) {
    maintNotice(e.status === 401 ? keyHelp()
      : t('maint.set_failed', {node: name, msg: e.message}), 'bad');
  }
  tick();
}

async function maintenanceClear(name) {
  if (!adminKey()) { maintNotice(keyHelp(), 'warn'); return; }
  if (!window.confirm(t('maint.clear_confirm', {node: name}))) return;
  try {
    const res = await adminApi('DELETE', '/admin/nodes/' + encodeURIComponent(name) + '/maintenance');
    maintNotice(res.maintenance
      ? t('maint.still_running', {node: name, line: maintLine(res)})
      : t('maint.cleared', {node: name}), 'ok');
  } catch (e) {
    maintNotice(e.status === 401 ? keyHelp()
      : t('maint.clear_failed', {node: name, msg: e.message}), 'bad');
  }
  tick();
}

/* ── Modell-Policy: setzen und zurücknehmen (issue #11) ───────────────────── */
/* Derselbe Weg wie bei Wartung und Kalibrierung: Schlüssel prüfen, bei den
   einschneidenden Schritten zurückfragen, ein Aufruf über `adminApi()`,
   Rückmeldung in den Betriebsblock, danach `tick()`. Die Tabelle selbst liest
   /healthz — was hier passiert, steht nach dem Tick in models[].hosts[].policy. */
function policyNotice(text, cls) {
  const ops = $('ops');
  if (ops) ops.open = true;
  policyMsg = {text: text, cls: cls || 'warn'};
  paintPolicy();
}

/* Ein Modellname darf einen Schrägstrich enthalten (`org/model`) und das
   Backend nimmt `{model:path}`: je Segment kodieren, den Schrägstrich stehen
   lassen — sonst wäre genau der Fall nicht adressierbar, für den es den
   Pfadparameter gibt. */
const policyPath = (node, model) => '/admin/nodes/' + encodeURIComponent(node) +
  '/models/' + String(model).split('/').map(encodeURIComponent).join('/');

/* Ein Satz aus der Antwort von GET/POST/DELETE — für den Betriebsblock. Die
   Felder sind die der Admin-Sicht und nicht erfunden: `level` (der Zustand),
   `listed` (wäre das Modell in /v1/models?) und `dispatch` (würde RIPPLE
   hierhin routen?). */
const policyLine = (res, model) => {
  const row = ((res && res.models) || []).find(r => r.id === model);
  if (!row) return t('policy.saved');
  return (row.level || t('models.policy_active')) +
    (row.listed ? ' · ' + t('policy.in_models') : ' · ' + t('policy.not_in_models')) +
    (row.dispatch ? ' · ' + t('policy.routes_here') : ' · ' + t('policy.no_route'));
};

async function modelPolicy(sel) {
  const node = sel.getAttribute('data-node') || '';
  const model = sel.getAttribute('data-model') || '';
  const was = sel.getAttribute('data-level') || '';
  const level = sel.value || '';
  if (level === was) return;
  if (!adminKey()) { sel.value = was; policyNotice(keyHelp(), 'warn'); tick(); return; }

  /* `hidden` ist die eine Stufe, die der Client sieht: Hermes und opencode
     verlieren das Modell aus ihrer Liste. Deshalb vorher fragen — und beim
     Zurücknehmen ebenso, weil auch das die Liste verändert. `disabled` ist die
     stille Stufe und kommt ohne Rückfrage aus. */
  const ask = level === 'hidden'
    ? t('policy.confirm_hide', {model: model, node: node})
    : level === ''
      ? t('policy.confirm_restore', {model: model, node: node})
      : '';
  if (ask && !window.confirm(ask)) { sel.value = was; return; }

  try {
    const res = level
      ? await adminApi('POST', policyPath(node, model) + '?level=' + encodeURIComponent(level))
      : await adminApi('DELETE', policyPath(node, model));
    policyNotice(t('policy.notice', {node: node, model: model, line: policyLine(res, model)}), 'ok');
    /* Der Zustand hat sich geändert: die Admin-Sicht, aus der die Liste der
       Regeln ohne Zeile kommt, ist damit veraltet. Der nächste Tick liest sie
       einmal nach — nicht jeder Tick, und nicht sofort ein zweiter Request. */
    policyStale = true;
  } catch (e) {
    sel.value = was;
    policyNotice(e.status === 401 ? keyHelp()
      : t('policy.change_failed', {model: model, node: node, msg: e.message}), 'bad');
  }
  await tick();
}

/* ── Der Lock: das Schloss über dem Modellnamen (issue #23) ───────────────── */
/* #21 hält den Zustand (die Datei, die Verweigerung, die Admin-Routen) und
   meldet ihn in /healthz; hier steht nur die Bedienung. Das Schloss sitzt in
   der Modellzelle und nicht in der Knotenspalte, weil der Lock je **Modell**
   gilt: `hosts[].lock` sagt nur, wo er gerade etwas schützt (gesperrt *und*
   dort geladen), `models[].lock` ist der Zustand, den das Icon zeigt.

   FA-22: kein Icon-Set, kein Font-Glyph, keine externe Datei — ein Inline-SVG
   mit zwei Pfaden. Die Farbe kommt aus `currentColor`, also aus dem
   Stylesheet, und der Zustand hängt an `data-locked`: der 2-s-Patch schreibt
   Attribut und Pfad um, ohne den Knoten zu ersetzen. Offen und geschlossen
   unterscheiden sich in der Form des Bügels, nicht nur im Farbton. */
const LOCK_SHACKLE = {
  open: 'M8 11V7a4 4 0 0 1 8 0',        // Bügel endet oben: offen
  closed: 'M8 11V7a4 4 0 0 1 8 0v4',    // Bügel kommt am Körper an: zu
};

const lockSvg = locked =>
  '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true" focusable="false">' +
  '<rect x="5" y="11" width="14" height="9" rx="2" fill="currentColor"/>' +
  '<path d="' + (locked ? LOCK_SHACKLE.closed : LOCK_SHACKLE.open) + '" fill="none"' +
  ' stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

/* Wo der Lock gerade etwas schützt: `hosts[].lock` ist „gesperrt **und** hier
   geladen“ — die Kopie, die eine Verweigerung auslösen kann. */
const lockHosts = m => (m.hosts || []).filter(x => x.lock).map(x => x.host).sort();

/* Der dritte Zustand (issue #126): gesperrt, aber auf keinem Host resident
   machbar. Die Aussage kommt aus der Admin-Sicht — `feasible === false` heißt
   „mindestens ein Host bietet das Modell an und **keiner** kann es aufnehmen“.
   `feasible === null` ist etwas anderes: kein Host bietet es an, der Lock ist
   dann nicht falsch, nur gegenstandslos — und das Schloss bleibt still, wie
   bisher. Ein oMLX-Builtin hat keinen Betreiber-Zustand und warnt nie. */
const lockWarn = m => !m.builtin && !!m.lock && m.feasible === false;

/* Sperrbar ist **jedes** Modell (issue #118): Sperrbarkeit ist eine Eigenschaft
   des Modells, nicht seiner Routbarkeit. Der Lock sagt, was passiert, *wenn* ein
   Modell resident ist — also muss er auf einem kalten Modell setzbar sein, auf
   einem, das die Allow-List nicht durchlässt, und auf einem, das noch kein
   Knoten listet (frischer oMLX-Start). #28 verweigerte solche Schreibvorgänge,
   weil eine Sperre dort nichts verweigern könne; das war die falsche Frage.
   Die eine Ausnahme ist kein Modell, sondern ein oMLX-Builtin (MarkItDown,
   `builtin: true` aus `/healthz`): immer gesperrt, nicht aufhebbar. Sein Schloss
   ist abgeschaltet und sagt im Tooltip warum, statt in eine 409 zu führen. */

/* Zustand, Wirkung und Quelle in einem Satz — dieselbe Regel wie bei jeder
   Kennzahl: die Farbe verstärkt die Aussage, sie trägt sie nicht allein. */
const lockTip = (m, keyed) => {
  /* Ein Builtin hat keinen Betreiber-Zustand: „offen" wäre dort eine Aussage,
     die es nicht gibt. Der Tooltip nennt deshalb nur, was es ist — mit
     derselben Quellenzeile wie jeder andere Tip der Seite, hier `builtin`
     statt `lock`: der Zustand ist abgeleitet, nicht gespeichert (#118). */
  if (m.builtin) return tipBody(['lock']) + '\n' +
    t('lock.builtin_tip', {model: m.id}) + '  ' +
    t('common.source') + ': /healthz → models[].builtin';
  const locked = !!m.lock;
  const here = lockHosts(m);
  const warn = lockWarn(m);
  /* Der Warnsatz nennt Host und Grund aus der Admin-Sicht (#126) — und nur,
     was die Antwort wirklich trägt: fehlt einer der beiden, gibt es keinen
     halben Satz. Der kurze Zustand `lock.warn` steht in der Zustandszeile. */
  const at = warn && m.feasible_host ? String(m.feasible_host) : '';
  const why = warn && m.feasible_reason ? String(m.feasible_reason) : '';
  return tipBody(['lock']) + '\n' +
    t('lock.state', {state: locked ? (warn ? t('lock.warn') : t('lock.closed')) : t('lock.open')}) +
    (locked
      ? (m.lock_reason
          ? t('lock.in_effect', {hosts: here.join(', ') || t('lock.nohost')})
          : t('lock.nowhere'))
      : t('lock.may_evict')) +
    (at && why ? '\n' + t('lock.warn_tip', {host: at, reason: why}) + '  ' +
      t('common.source') + ': /admin/models → models[].feasible' : '') +
    (keyed ? '' : t('lock.no_key', {help: keyHelp()}));
};

/* Der Knopf ist ohne Schlüssel sichtbar, aber wirkungslos. `aria-disabled`
   statt `disabled`: ein abgeschaltetes Formularelement bekommt keine
   Zeigerereignisse, dann käme der Tooltip nicht — und der nennt genau den
   Grund. Der Klick prüft den Schlüssel selbst und tut sonst nichts; beim
   Builtin prüft er `data-builtin` und schreibt gar nicht erst. */
function lockButton(m, keyed) {
  const builtin = !!m.builtin;
  const locked = builtin || !!m.lock;
  const warn = lockWarn(m);
  const inert = builtin || !keyed;
  return '<button type="button" class="model-lock tip" data-model="' + esc(m.id) + '"' +
    ' data-locked="' + (locked ? '1' : '0') + '"' +
    (builtin ? ' data-builtin="1"' : '') +
    /* Der dritte Zustand ist ein Attribut, keine Klasse (#126): `data-warn`
       steht nur, wenn er gilt, und das Stylesheet hängt Kontur und
       Ausrufezeichen daran — der Zustand überlebt also auch den Graustufen-
       Blick. Die Farbe verstärkt ihn, sie trägt ihn nicht. */
    (warn ? ' data-warn="1"' : '') +
    ' data-tip="' + esc(lockTip(m, keyed)) + '"' +
    ' aria-disabled="' + (inert ? 'true' : 'false') + '"' +
    ' aria-label="' + esc(t('lock.aria', {model: m.id,
      state: warn ? t('lock.warn') : (locked ? t('lock.closed') : t('lock.open'))})) + '"' +
    /* Der `title` der Zelle gilt sonst auch für das Schloss — zwei Tooltips
       übereinander. Ein leerer `title` schaltet den geerbten ab. */
    ' title="" onclick="modelLock(this)">' + lockSvg(locked) + '</button>';
}

/* `{model:path}` im Backend: je Segment kodieren, den Schrägstrich stehen
   lassen — dieselbe Regel wie bei der Policy (#11), sonst wäre `org/model`
   genau in dem Fall nicht adressierbar, für den es den Pfadparameter gibt. */
const lockPath = model => '/admin/models/' +
  String(model).split('/').map(encodeURIComponent).join('/') + '/lock';

/* Ein Satz aus der Antwort des Schreibens — die Admin-Sicht, nicht erfunden. */
const lockLine = (res, model) => {
  const row = ((res && res.models) || []).find(r => r.id === model);
  if (!row) return t('lock.saved');
  const here = (row.hosts || []).filter(x => x.lock).map(x => x.host);
  /* Der Lock ist gespeichert, wirkt aber nicht (#126): `feasible === false`
     heißt, kein Host kann das Modell aufnehmen. Die Notiz sagt das mit dem
     Grund aus derselben Antwort, statt den Betreiber bis zum nächsten Preload
     warten zu lassen. Fehlt der Grund, steht der Strich, den die Seite für
     einen fehlenden Wert setzt — kein erfundener Satz. */
  const warn = row.lock && row.feasible === false;
  return (row.lock ? t('lock.locked') : t('lock.free')) +
    (row.lock
      ? (here.length ? t('lock.resident', {hosts: here.join(', ')}) : t('lock.not_loaded'))
      : t('lock.evict_again')) +
    (row.listed ? ' · ' + t('policy.in_models') : '') +
    (warn ? ' · ' + t('lock.warn_saved', {model: model,
      reason: row.feasible_reason || '—'}) : '');
};

/* Der Zustand aus der Antwort lokal nachziehen, damit das Schloss sofort kippt
   — ohne eine zweite Anfrage. Der nächste 2-s-Tick bestätigt ihn aus /healthz,
   der einen Quelle; die Admin-Sicht ist nur die Antwort auf den Klick. */
function applyLockView(view, model) {
  if (!last || !view) return;
  const row = ((view.models) || []).find(r => r.id === model);
  const m = ((last.h && last.h.models) || []).find(x => x.id === model);
  if (!row || !m) return;
  m.lock = !!row.lock;
  m.lock_reason = row.lock_reason === undefined ? null : row.lock_reason;
  if (row.builtin !== undefined) m.builtin = !!row.builtin;
  /* Die Machbarkeit kommt aus derselben Antwort (#126): beide Schreib-Endpunkte
     liefern `_models_lock_view()`, also dieselben drei Felder wie das Lesen. */
  if (row.feasible !== undefined) m.feasible = row.feasible;
  if (row.feasible_reason !== undefined) m.feasible_reason = row.feasible_reason;
  if (row.feasible_host !== undefined) m.feasible_host = row.feasible_host;
  /* Die Antwort ist die frischeste Sicht, die die Seite hat: sie wandert in den
     Zwischenspeicher, damit der nächste /healthz-Tick den Warnzustand nicht
     wieder ausradiert (#126). */
  rememberFeasibility(view.models);
  const byHost = new Map((row.hosts || []).map(x => [x.host, !!x.lock]));
  (m.hosts || []).forEach(x => { if (byHost.has(x.host)) x.lock = byHost.get(x.host); });
  apply('models', renderModels(last.h));
}

/* Die Machbarkeit ist keine /healthz-Angabe: sie steht nur in der Admin-Sicht
   (`GET /admin/models`). Der 2-s-Takt ersetzt `last.h` durch einen frischen
   /healthz-Abzug — ohne diesen Zwischenspeicher wäre der Warnzustand zwei
   Sekunden nach dem Start wieder weg. Gelesen wird beim Start und mit der
   Antwort jedes Klicks; ändert sich der Speicher dazwischen, zeigt sich das mit
   dem nächsten Klick oder einem Reload, nicht mit einer Anfrage alle zwei
   Sekunden (der Takt bleibt bei seinen drei Endpunkten). */
const feasibleById = new Map();

function rememberFeasibility(rows) {
  (rows || []).forEach(r => {
    if (r.feasible === undefined) return;      // eine ältere Antwort sagt nichts
    feasibleById.set(r.id, {
      feasible: r.feasible,
      feasible_reason: r.feasible_reason === undefined ? null : r.feasible_reason,
      feasible_host: r.feasible_host === undefined ? null : r.feasible_host,
    });
  });
}

function mergeFeasibility(h) {
  ((h && h.models) || []).forEach(m => {
    const r = feasibleById.get(m.id);
    if (!r) return;
    m.feasible = r.feasible;
    m.feasible_reason = r.feasible_reason;
    m.feasible_host = r.feasible_host;
  });
  return h;
}

/* Die Machbarkeit **vor** dem Klick (#126): ein Lock auf ein Modell, das
   nirgends mehr Platz hat, ist gespeichert und trotzdem wirkungslos — ohne
   diese Zeile merkt der Betreiber das erst beim nächsten Preload. `GET
   /admin/models` trägt je Modell `feasible`, `feasible_reason` und
   `feasible_host`; die Seite legt genau diese drei an die Zeilen, die sie
   schon zeigt, und zeichnet die Tabelle neu. Alles andere bleibt /healthz. */
function applyFeasibility(view) {
  if (!last || !view) return;
  rememberFeasibility(view.models);
  mergeFeasibility(last.h);
  apply('models', renderModels(last.h));
}

/* Einmal beim Start, und nur mit Schlüssel: ohne Schlüssel antwortet die Route
   403, und eine Anfrage, deren Antwort feststeht, ist keine Messung. Der
   2-s-Takt bleibt bei seinen drei Endpunkten — die Machbarkeit ändert sich mit
   dem Speicher, nicht mit der Sekunde. Nach einem Klick zieht `applyLockView`
   sie aus der Antwort des Schreibens nach. */
async function loadFeasibility() {
  if (!adminKey() || !last) return;
  try {
    applyFeasibility(await adminApi('GET', '/admin/models'));
    /* Dieselbe Antwort beantwortet die Frage des Chips (#130): der Schlüssel
       im Tab ist gültig, der Tab ist Admin. */
    adminBooted(true);
  } catch (e) {
    /* Nicht gelesen heißt unbekannt, nicht „machbar": die Zeilen behalten
       `feasible: null`, und das Schloss bleibt still, statt eine Warnung zu
       behaupten, die niemand gesehen hat. Und der Chip behauptet keinen Admin:
       ein Schlüssel, der hier scheitert, wird gelöscht. */
    adminBooted(false);
  }
}

/* Derselbe Weg wie bei Policy, Wartung und Kalibrierung: Schlüssel prüfen,
   rückfragen, ein Aufruf über `adminApi()`, Rückmeldung in den Betriebsblock.
   Beide Richtungen fragen — beide ändern, was die Flotte annimmt. Ein Klick
   kostet genau einen Request: POST zum Sperren, DELETE zum Aufheben. */
async function modelLock(btn) {
  const model = btn.getAttribute('data-model') || '';
  /* Ein oMLX-Builtin wird gar nicht erst geschrieben (issue #118): das Backend
     verweigert beide Richtungen mit 409 `lock_builtin`, und ein Klick, der nur
     eine Absage erzeugen kann, ist eine Sackgasse. Der Tooltip sagt warum. */
  if (btn.getAttribute('data-builtin') === '1') return;
  const locked = btn.getAttribute('data-locked') === '1';
  if (!adminKey()) return;                      // sichtbar, aber wirkungslos
  const ask = locked
    ? t('lock.confirm_unlock', {model: model})
    : t('lock.confirm_lock', {model: model});
  if (!window.confirm(ask)) return;
  let res = null;
  try {
    res = locked ? await adminApi('DELETE', lockPath(model))
                 : await adminApi('POST', lockPath(model));
  } catch (e) {
    policyNotice(t('lock.not_changed', {model: model,
      msg: e.status === 403 ? keyHelp() : e.message}), 'bad');
    return;
  }
  applyLockView(res, model);
  policyNotice(t('lock.notice', {model: model, line: lockLine(res, model)}), 'ok');
}

/* ── Restart: end the process, launchd starts it again (issue #25) ───────── */
/* The one operator action that ends the process it runs in. #22 built the
   endpoint and its guards; this is its surface. Three things make it different
   from the four controls above:

   - Its state costs no request: `/healthz` carries `restart` (`supervised`,
     `supervisor`, `reason`, `inflight`, `hosts`, `pending`, `delay_seconds`),
     and `/healthz` is already in the tick — without a key, because the reason a
     control is off has to be readable *before* an unlock. The plain 2 s tick
     stays at exactly `/healthz`, `/stats`, `/config`.
   - The reason is the service's own sentence, shown as it is: it is written for
     the operator, and a paraphrase would be a second version of a fact.
   - After the 202 the process ends about a second later. The page therefore
     stops polling into the void: it waits for the service to answer again as a
     *new* process (smaller `uptime_seconds`) and then lets the normal tick show
     the new `build.commit` in the footer.

   `force` is sent only after the second question, and only because the endpoint
   answered 409: the page never acknowledges a drain it has not been told about. */
const RESTART_PATH = '/admin/restart';
/* Half a second between probes, sixty of them: half a minute is longer than any
   launchd relaunch measured here, short enough that an open page recovers. */
const RESTART_PROBE_MS = 500;
const RESTART_PROBES = 60;

/* The refusal cause is a token, not a sentence (#29). The service sends
   `restart.reason_code` beside its English prose; the page renders
   `restart.reason.<code>` from the catalog and keeps the prose as the tooltip,
   so the German page reads one German sentence and the operator can still check
   the service's own wording. A code the page does not know falls back to the
   prose verbatim — the page never invents a friendlier cause than the service
   has. */
const RESTART_REASONS = {
  no_label: 'restart.reason.no_label',
  inherited_label: 'restart.reason.inherited_label',
  label_not_owner: 'restart.reason.label_not_owner',
  query_failed: 'restart.reason.query_failed',
  inflight: 'restart.reason.inflight',
};
const RESTART_WHY_SRC = '/healthz → restart.reason, restart.reason_code';
const RESTART_REFUSAL_SRC = 'POST /admin/restart → error.message, reason_code';

/* The sentence and the prose behind it, for a `restart` block from `/healthz`
   or an error body from the endpoint. `params` come from `reason_params`. */
function restartReason(r) {
  if (!r || !r.reason) return null;
  const id = RESTART_REASONS[r.reason_code];
  if (!id) return {text: r.reason, tip: null};
  return {text: t(id, r.reason_params || {}), tip: r.reason};
}

/* The cause as the page shows it: one sentence from the catalog, the service's
   prose in the tooltip (with its source, like every other tooltip). */
function restartWhy(r, src) {
  const why = restartReason(r);
  if (!why) return '';
  const tip = why.tip
    ? ' data-tip="' + esc(why.tip + '\n' + t('common.source') + ': ' + (src || RESTART_WHY_SRC)) + '"'
    : '';
  return '<div class="restart-why bad"' + tip + '>' + esc(why.text) + '</div>';
}

const restartState = {busy: false, pending: false, msg: null};
let tickTimer = null;

const restartOf = h => (h && h.restart) || null;

function paintRestart() {
  apply('restartPanel', renderRestart(last ? last.h : null));
}

function restartNotice(text, cls, tip) {
  restartState.msg = {text: text, cls: cls || 'warn', tip: tip || null};
  paintRestart();
}

/* The block names what the service says about itself, in its own words: whether
   launchd started it, how much is running, how long the delay is — and why the
   button is off. Every number carries its tooltip with the source. */
function renderRestart(h) {
  const r = restartOf(h);
  const keyed = !!adminKey();
  const pending = restartState.pending || !!(r && r.pending);
  const supervised = r ? !!r.supervised : null;
  const inflight = r && typeof r.inflight === 'number' ? r.inflight : null;
  const hosts = (r && r.hosts) || [];
  const delay = r && typeof r.delay_seconds === 'number' ? r.delay_seconds : null;
  const off = pending || !keyed || supervised === false || restartState.busy;
  const parts = [];

  const state = [];
  if (r) {
    if (supervised) {
      state.push(T('restart_supervised',
        esc(t('restart.supervised', {label: r.supervisor || '—'}))));
    }
    if (inflight !== null) {
      state.push(T('restart_inflight', esc(inflight > 0
        ? t('restart.inflight', {n: nf(inflight), hosts: hosts.join(', ') || '—'})
        : t('restart.inflight_none'))));
    }
    if (delay !== null) {
      state.push(T('restart_delay', esc(t('restart.delay', {v: nf(delay, 1)}))));
    }
    if (pending) state.push('<span class="restart-pending">' + esc(t('restart.pending')) + '</span>');
  }

  parts.push('<div class="restart-row">' +
    '<button type="button" class="btn restart-btn" data-state="' +
      (pending ? 'pending' : supervised === false ? 'unsupervised' : 'ready') + '"' +
      ' onclick="restartNow()"' + (off ? ' disabled' : '') + '>' +
      esc(t(pending ? 'restart.pending_btn' : 'restart.button')) + '</button>' +
    (state.length ? '<div class="restart-state">' + state.join(' · ') + '</div>' : '') +
    '</div>');

  /* Why the control is off, in words and in front of it — a disabled button
     without a reason is a dead end. Both reasons can hold at once. */
  if (!keyed) {
    parts.push('<div class="restart-why">' + esc(keyHelp()) + '</div>');
  }
  if (supervised === false) {
    parts.push(restartWhy(r));
  }
  if (restartState.msg) {
    parts.push('<div class="' + restartState.msg.cls + '"' +
      (restartState.msg.tip ? ' data-tip="' + esc(restartState.msg.tip) + '"' : '') +
      '>' + esc(restartState.msg.text) + '</div>');
  }
  if (!r && !restartState.msg) {
    parts.push('<div class="calib-line">' + esc(t('panel.restart.idle')) + '</div>');
  }
  return parts.join('');
}

/* One tick into a process that is gone would only paint a connection error every
   two seconds. The wait below owns the requests until the service is back. */
function stopTick() {
  if (tickTimer !== null && typeof clearInterval === 'function') clearInterval(tickTimer);
  tickTimer = null;
}

function startTick() {
  if (tickTimer !== null) return;
  tickTimer = setInterval(tick, 2000);
}

async function probeHealth() {
  try {
    return (await getHealth()) || null;
  } catch (e) {
    return null;
  }
}

/* The answer after the gap is the *new* process: its uptime is smaller than the
   one the page held before the 202 (the old process keeps answering for up to a
   second after it accepted). A changed `build.commit` counts too. */
function isNewProcess(h, before) {
  if (!before || typeof before.uptime !== 'number') return true;
  const now = typeof h.uptime_seconds === 'number' ? h.uptime_seconds : null;
  if (now === null) return false;
  if (now < before.uptime) return true;
  const commit = (h.build || {}).commit || '';
  return !!before.commit && !!commit && commit !== before.commit;
}

async function waitForRestart(before) {
  stopTick();
  let seen = null;
  for (let i = 0; i < RESTART_PROBES && !seen; i++) {
    await sleep(RESTART_PROBE_MS);
    const h = await probeHealth();
    if (h && isNewProcess(h, before)) seen = h;
  }
  restartState.pending = false;
  restartState.msg = seen
    ? {text: t('restart.back', {commit: ((seen.build || {}).commit) || '—',
                               n: nf(seen.uptime_seconds || 0, 0)}), cls: 'ok'}
    : {text: t('restart.gone', {n: nf(RESTART_PROBES * RESTART_PROBE_MS / 1000, 0)}), cls: 'bad'};
  startTick();
  await tick();
}

/* The accepted restart: 202 says the exit is on its way. The page shows that,
   then waits — and the footer, not this panel, carries the new commit. */
async function restartAccepted(res) {
  restartState.busy = false;
  restartState.pending = true;
  const before = (last && last.h)
    ? {commit: ((last.h.build || {}).commit) || '', uptime: last.h.uptime_seconds}
    : null;
  const delay = res && typeof res.delay_seconds === 'number' ? res.delay_seconds : 1;
  restartNotice(t('restart.accepted', {v: nf(delay, 1)}), 'ok');
  await waitForRestart(before);
}

/* The second question. The 409 carries the drain sentence and the numbers; the
   confirmation repeats them and names the one thing the operator cannot read out
   of a counter: the streaming request is their own session. */
async function restartForce(refusal) {
  const data = (refusal && refusal.data) || {};
  const hosts = data.hosts || [];
  const ok = window.confirm(t('restart.force_confirm', {
    n: nf(typeof data.inflight === 'number' ? data.inflight : 0),
    hosts: hosts.join(', ') || '—',
    msg: refusal.message || '',
  }));
  if (!ok) {
    /* The panel says why in the page's language (#29); the service's own
       sentence — the one that names the streaming session — stays in the
       question above, where the operator just read it. */
    restartNotice(t('restart.inflight_kept', {
      msg: t('restart.reason.inflight', {
        n: nf(typeof data.inflight === 'number' ? data.inflight : 0),
        hosts: hosts.join(', ') || '—',
      }),
    }), 'warn', (refusal.message || '') + '\n' + t('common.source') + ': ' +
       RESTART_REFUSAL_SRC);
    return;
  }
  restartState.busy = true;
  paintRestart();
  try {
    const res = await adminApi('POST', RESTART_PATH + '?force=1');
    await restartAccepted(res);
  } catch (e) {
    restartState.busy = false;
    restartNotice(e.status === 401 ? keyHelp() : t('restart.failed', {msg: e.message}), 'bad');
  }
}

async function restartNow() {
  if (restartState.busy) return;
  if (!adminKey()) { restartNotice(keyHelp(), 'warn'); return; }
  const r = restartOf(last ? last.h : null) || {};
  /* A click while the exit is pending is not an error at the endpoint (it
     answers 202 again) — the page shows `pending` and keeps the button off. */
  if (r.pending || restartState.pending) { restartNotice(t('restart.pending'), 'info'); return; }
  if (r.supervised === false) {
    const why = restartReason(r);
    restartNotice(why ? why.text : t('restart.not_supervised'),
                  'bad', why && why.tip ? why.tip + '\n' + t('common.source') + ': ' +
                                         RESTART_WHY_SRC : null);
    return;
  }
  const delay = typeof r.delay_seconds === 'number' ? r.delay_seconds : 1;
  /* The first question already says how much is running — `/healthz` carries
     the number, so this costs no request. "Cancel" is the offer to wait. */
  const busy = (typeof r.inflight === 'number' && r.inflight > 0)
    ? ' ' + t('restart.confirm_busy', {n: nf(r.inflight),
                                       hosts: (r.hosts || []).join(', ') || '—'})
    : '';
  if (!window.confirm(t('restart.confirm', {v: nf(delay, 1), busy: busy}))) return;

  restartState.busy = true;
  paintRestart();
  let res = null;
  try {
    res = await adminApi('POST', RESTART_PATH);
  } catch (e) {
    restartState.busy = false;
    if (e.status === 409) { await restartForce(e); return; }
    /* 503 is the race the page cannot see: /healthz said supervised and the
       endpoint disagreed. Its body carries the code too (#29), so the notice is
       a sentence in the page's language with the service's wording as tooltip. */
    if (e.status === 503) {
      const data = e.data || {};
      const why = restartReason({reason: e.message, reason_code: data.reason_code,
                                 reason_params: data.reason_params});
      restartNotice(why ? why.text : t('restart.not_supervised'),
                    'bad', why && why.tip ? why.tip + '\n' + t('common.source') +
                                           ': ' + RESTART_REFUSAL_SRC : null);
      return;
    }
    restartNotice(e.status === 401 ? keyHelp()
      : e.status === 429 ? t('restart.rate_limited', {msg: e.message})
      : t('restart.failed', {msg: e.message}), 'bad');
    return;
  }
  await restartAccepted(res);
}

/* ── 7 Knoten: aufnehmen und entfernen (issue #71) ─────────────────────────── */
/* Zwei Wege hinein, einer hinaus — und keiner davon ohne Bestätigung:

   - per Adresse: `POST /admin/nodes?dry_run=true` liest den Knoten (node_id,
     Modellliste, Speicher, gemessene Umlaufzeit, abgeleiteter Standort) und
     schreibt nichts. Erst der zweite Aufruf, nach der Rückfrage, ändert die
     Flotte.
   - per Suche: `POST /admin/discover` läuft über ein CIDR und liefert je
     Antwort denselben Steckbrief; ein Klick übernimmt die Adresse in den
     ersten Weg, damit die Vorschau für beide Wege derselbe Code ist.
   - hinaus: `DELETE /admin/nodes/{name}` ist der Drain aus #70. Der Dialog
     nennt, was in Flug ist und wie lange gewartet wird, bevor er fragt.

   Warum die Felder den 2-s-Takt überleben: die Adress- und CIDR-Felder stehen
   in der Shell (`dashboard.py`) und werden von keinem Patch berührt. Die vier
   Felder der Vorschau (Name, Gewicht, Standort, Rolle) leben in `nodeState`:
   was getippt wird, steht dort, und der nächste Tick baut dieselbe Zeichenkette
   und schreibt deshalb nichts. Zusätzlich hält `applyNode()` jeden Patch
   zurück, solange der Fokus in einem Eingabefeld dieses Kastens steht — ein
   ersetztes Feld verliert Text und Cursor, und beides ist teurer als ein Tick
   Verzögerung. */
const NODE_STEPS = {resolve: 'nodes.err.resolve', node_id: 'nodes.err.node_id',
                    models: 'nodes.err.models', cidr: 'nodes.err.cidr'};
const NODE_ROLES = [['worker', 'nodes.role_worker'], ['entry', 'nodes.role_entry']];

const nodeState = {
  preview: null,   // the dry-run answer, as the server sent it
  found: null,     // the discovery answer
  drain: null,     // the node whose removal is being confirmed
  msg: null,       // {text, cls, tip}
  busy: false,
  draft: {name: '', weight: '1', site: '', role: 'worker'},
};

function nodeValue(id) {
  const el = $(id);
  return el && typeof el.value === 'string' ? el.value.trim() : '';
}

const nodeNum = (v, fallback) => {
  const n = parseFloat(String(v === undefined || v === null ? '' : v).replace(',', '.'));
  return isFinite(n) ? n : fallback;
};

function nodeNotice(text, cls, tip) {
  nodeState.msg = {text: text, cls: cls || 'warn', tip: tip || null};
  paintNodes();
}

/* Der Server nennt den Schritt (`resolve`, `node_id`, `models`, `cidr`); die
   Seite macht daraus einen Satz in ihrer Sprache und behält seinen Wortlaut als
   Tooltip — dieselbe Regel wie bei den Abbruchgründen des Neustarts (#29). Ein
   Schritt ohne Katalogtext fällt auf `nodes.err.other` zurück, damit eine neue
   Fehlerart nicht als leerer Kasten erscheint. */
function nodeError(e) {
  const data = (e && e.data) || {};
  const err = data.error || data || {};
  const detail = err.last_error || err.message || (e && e.message) || '';
  const id = NODE_STEPS[err.step];
  return {text: id ? t(id, {detail: detail}) : t('nodes.err.other', {msg: detail}),
          tip: detail || null};
}

/* Was in den vier Feldern steht, ist die Wahrheit — nicht der Zustand von
   vorhin: der Klick liest sie, bevor er sendet (dieselbe Regel wie `replValue`
   im Replikationsformular). */
function nodeReadDraft() {
  const d = nodeState.draft;
  const take = (id, key) => {
    const el = $(id);
    if (el && typeof el.value === 'string') d[key] = el.value;
  };
  take('nodeName', 'name');
  take('nodeWeight', 'weight');
  take('nodeSite', 'site');
  take('nodeRole', 'role');
  return d;
}

/* Ein Feld, in dem gerade getippt wird, wird von keinem Tick ersetzt. Der
   Patch wird dann auf den nächsten Tick vertagt; der nächste Tick kommt in
   zwei Sekunden, und ein Formular, das den Text verliert, ist der teurere
   Fehler. */
function applyNode(id, html) {
  const host = $(id);
  if (!host) return;
  const active = document.activeElement;
  if (active && typeof active.tagName === 'string' &&
      /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName) &&
      typeof host.contains === 'function' && host.contains(active)) {
    return;
  }
  apply(id, html);
}

function paintNodes(h, c) {
  const health = h || (last && last.h) || null;
  const cfg = c || (last && last.c) || null;
  applyNode('nodePreview', renderNodePreview(cfg));
  applyNode('nodeFound', renderNodeFound());
  applyNode('nodeDrain', renderNodeDrain());
  applyNode('nodeMsg', renderNodeMsg());
  applyNode('nodeList', renderNodeList(health));
}

/* ── 7.1 Die Vorschau: was der Knoten ist, bevor er dazugehört ────────────── */
async function nodePreview(address) {
  if (!adminKey()) { nodeNotice(keyHelp(), 'warn'); return; }
  const addr = (typeof address === 'string' && address) || nodeValue('nodeAddress');
  if (!addr) { nodeNotice(t('nodes.need_address'), 'warn'); return; }
  nodeState.busy = true;
  nodeState.preview = null;
  nodeState.found = null;
  nodeState.msg = null;
  paintNodes();
  try {
    const p = await adminApi('POST', '/admin/nodes?dry_run=true', {address: addr});
    nodeState.preview = p;
    /* Die drei Entscheidungen mit ihren gemessenen Vorgaben: gleiches Gewicht,
       der aus der Adresse abgeleitete Standort, Rolle `worker`. */
    nodeState.draft = {
      name: p.name || '',
      weight: String(p.weight === null || p.weight === undefined ? 1 : p.weight),
      site: p.site || p.site_derived || '',
      role: p.role || 'worker',
    };
  } catch (e) {
    const why = nodeError(e);
    nodeNotice(why.text, 'bad', why.tip);
  }
  nodeState.busy = false;
  paintNodes();
}

function nodeCancel() {
  nodeState.preview = null;
  nodeState.msg = null;
  paintNodes();
}

/* Der eine Klick, der schreibt: was in der Vorschau steht, geht so hinaus. */
async function nodeAdd() {
  const p = nodeState.preview;
  if (!p || nodeState.busy) return;
  if (!adminKey()) { nodeNotice(keyHelp(), 'warn'); return; }
  const d = nodeReadDraft();
  const body = {
    address: p.address,
    name: (d.name || p.name || '').trim(),
    weight: nodeNum(d.weight, 1),
    role: d.role || 'worker',
  };
  if (d.site) body.site = d.site;
  const ok = window.confirm(t('nodes.confirm', {
    name: body.name, address: body.address, weight: nf(body.weight, 1),
    site: body.site || '—', role: body.role,
  }));
  if (!ok) return;
  nodeState.busy = true;
  paintNodes();
  try {
    const res = await adminApi('POST', '/admin/nodes', body);
    nodeState.preview = null;
    nodeNotice(t('nodes.added', {name: body.name, models: nf(res.models || 0),
                                 rtt: nf(p.rtt_ms, 1)}), 'ok');
    await tick();
  } catch (e) {
    const why = nodeError(e);
    nodeNotice(why.text, 'bad', why.tip);
  }
  nodeState.busy = false;
  paintNodes();
}

/* ── 7.2 Die Suche: derselbe Steckbrief je Antwort ────────────────────────── */
async function nodeScan() {
  if (!adminKey()) { nodeNotice(keyHelp(), 'warn'); return; }
  const cidr = nodeValue('nodeCidr');
  if (!cidr) { nodeNotice(t('nodes.need_cidr'), 'warn'); return; }
  const port = nodeNum(nodeValue('nodePort'), 8000);
  nodeState.busy = true;
  nodeState.found = null;
  nodeState.preview = null;
  nodeState.msg = null;
  paintNodes();
  try {
    nodeState.found = await adminApi('POST', '/admin/discover', {cidr: cidr, port: port});
  } catch (e) {
    const why = nodeError(e);
    nodeNotice(why.text, 'bad', why.tip);
  }
  nodeState.busy = false;
  paintNodes();
}

/* Ein Klick auf einen Fund ist kein zweiter Weg zum Hinzufügen: er füllt das
   Adressfeld und läuft durch dieselbe Vorschau wie 7.1. */
function nodePick(address) {
  const el = $('nodeAddress');
  if (el) el.value = address;
  return nodePreview(address);
}

/* ── 7.3 Der Drain-Dialog (issue #70) ─────────────────────────────────────── */
function nodeAskRemove(name) {
  if (!adminKey()) { nodeNotice(keyHelp(), 'warn'); return; }
  const h = last && last.h;
  const x = ((h && h.hosts) || []).filter(v => v.name === name)[0] || null;
  const fleet = (h && h.fleet) || {};
  nodeState.drain = {
    name: name,
    inflight: x ? (x.inflight || 0) : 0,
    models: nodeRunning(h, name),
    deadline: typeof fleet.drain_seconds === 'number' ? fleet.drain_seconds : null,
    source: x ? x.source : '',
    busy: false,
  };
  nodeState.msg = null;
  paintNodes();
}

/* Was auf dem Knoten läuft, aus dem Buch, das RIPPLE ohnehin führt:
   `/healthz → models[].hosts[]` trägt `inflight` je (Modell, Knoten). */
function nodeRunning(h, name) {
  const out = [];
  for (const m of ((h && h.models) || [])) {
    for (const x of (m.hosts || [])) {
      if (x.host === name && (x.inflight || 0) > 0) {
        out.push({model: m.id, inflight: x.inflight});
      }
    }
  }
  return out;
}

function nodeDrainCancel() {
  nodeState.drain = null;
  paintNodes();
}

async function nodeDrainConfirm() {
  const d = nodeState.drain;
  if (!d || d.busy) return;
  if (!adminKey()) { nodeNotice(keyHelp(), 'warn'); return; }
  d.busy = true;
  paintNodes();
  try {
    const res = await adminApi('DELETE', '/admin/nodes/' + encodeURIComponent(d.name));
    nodeState.drain = null;
    nodeNotice(t('nodes.removed', {name: d.name, v: nf(res.waited_seconds || 0, 2),
                                   n: nf(res.threads_released || 0)}), 'ok');
    await tick();
  } catch (e) {
    d.busy = false;
    const data = (e && e.data) || {};
    const err = data.error || data || {};
    if (err.code === 'drain_timeout') {
      /* Nichts wurde entfernt: der Knoten ist ausgesetzt, nicht halb weg. Die
         Zahlen der Antwort ersetzen die der Seite, damit der Dialog den
         aktuellen Stand zeigt und nicht den von vor dem Klick. */
      d.inflight = typeof err.inflight === 'number' ? err.inflight : d.inflight;
      d.models = err.models || d.models;
      d.deadline = typeof err.deadline_seconds === 'number' ? err.deadline_seconds : d.deadline;
      nodeNotice(t('nodes.drain_timeout', {msg: err.message || ''}), 'bad',
                 err.message || null);
    } else if (err.code === 'config_host') {
      nodeState.drain = null;
      nodeNotice(t('nodes.remove_config'), 'warn', err.message || null);
    } else {
      nodeNotice(e.status === 401 ? keyHelp()
        : t('nodes.err.other', {msg: (e && e.message) || ''}), 'bad');
    }
  }
  paintNodes();
}

/* Der Ausweg aus einem Drain, der nicht zu Ende kommt: aussetzen statt
   entfernen. Derselbe Endpunkt, den #70 dafür anbietet. */
async function nodeDrainDisable() {
  const d = nodeState.drain;
  if (!d || d.busy) return;
  if (!adminKey()) { nodeNotice(keyHelp(), 'warn'); return; }
  d.busy = true;
  paintNodes();
  try {
    await adminApi('POST', '/admin/nodes/' + encodeURIComponent(d.name) + '/disable');
    nodeState.drain = null;
    nodeNotice(t('nodes.disabled', {name: d.name}), 'warn');
    await tick();
  } catch (e) {
    d.busy = false;
    const why = nodeError(e);
    nodeNotice(why.text, 'bad', why.tip);
  }
  paintNodes();
}

/* ── 7 Die drei Kästen ────────────────────────────────────────────────────── */
const nodeField = (id, label, value, type) =>
  '<label class="node-field"><span>' + esc(label) + '</span>' +
  '<input id="' + id + '" type="' + type + '" value="' + esc(value) + '"' +
  ' autocomplete="off" spellcheck="false"' +
  (type === 'number' ? ' min="0" step="0.1"' : '') + '></label>';

const nodeSelect = (id, label, options) =>
  '<label class="node-field"><span>' + esc(label) + '</span>' +
  '<select id="' + id + '">' + options + '</select></label>';

/* Der Standort, den die Adresse ergibt — und der Widerspruch, wenn der
   Betreiber einen anderen wählt (#67). Beides steht als Satz da, nicht als
   Farbe: der Standort ist eine Lage, keine Routing-Regel, und ein Knoten
   außerhalb jedes Standorts lässt sich trotzdem aufnehmen. */
function nodeSiteLine(p) {
  if (p.site_mismatch) {
    return '<div class="calib-line warn">⚠ ' + esc(t('nodes.site_mismatch', {
      declared: p.site_name || p.site || '—',
      derived: p.site_derived_name || p.site_derived || '—'})) + '</div>';
  }
  if (!p.site_derived) {
    return '<div class="calib-line warn">⚠ ' +
      esc(t('nodes.site_none', {address: p.address || '—'})) + '</div>';
  }
  return '';
}

function renderNodePreview(c) {
  const p = nodeState.preview;
  if (!p) {
    return '<div class="calib-line">' +
      esc(t(nodeState.busy ? 'nodes.previewing' : 'panel.nodes.idle')) + '</div>';
  }
  const keyed = !!adminKey();
  const models = p.models || [];
  const shown = models.slice(0, 12).map(m => '<span class="chip">' + esc(m) + '</span>');
  if (models.length > 12) shown.push('<span class="chip">+' + nf(models.length - 12) + '</span>');
  const modelList = models.length ? shown.join(' ')
    : '<span class="warn">' + esc(t(p.models_error ? 'nodes.models_unreadable'
                                                  : 'nodes.models_none')) + '</span>';
  const parts = [];

  parts.push('<div class="calib-head"><strong>' + esc(p.name || p.address) + '</strong>' +
    '<span class="chip">' + esc(p.address || '—') + '</span>' +
    '<span class="chip ' + (p.step ? 'warn' : 'ok') + '">' +
      esc(t(p.step ? 'nodes.preview_partial' : 'nodes.preview_ok')) + '</span>' +
    '<span class="chip">' + esc(t('nodes.dry_run')) + '</span></div>');

  /* Vier Zahlen, jede mit ihrer Quelle (Tooltip-Konvention der Seite). */
  parts.push('<div class="node-facts">' +
    '<div class="calib-line num">' + T('node_id',
      '<b>' + esc(t('nodes.node_id')) + '</b> ' + esc(p.node_id || '—')) + '</div>' +
    '<div class="calib-line num">' + T('node_models',
      '<b>' + esc(t('nodes.models')) + '</b> ' + nf(models.length)) + ' ' + modelList + '</div>' +
    '<div class="calib-line num">' + T('node_memory',
      '<b>' + esc(t('nodes.memory')) + '</b> ' + gb(p.used_bytes) + ' / ' +
      gb(p.ceiling_bytes) + ' GB') + '</div>' +
    '<div class="calib-line num">' + T('node_rtt',
      '<b>' + esc(t('nodes.rtt')) + '</b> ' + nf(p.rtt_ms, 1) + ' ms') + '</div>' +
    '<div class="calib-line num">' + T('node_site',
      '<b>' + esc(t('nodes.site')) + '</b> ' +
      esc(p.site_derived_name || p.site_derived || '—')) + '</div>' +
    '</div>');

  parts.push(nodeSiteLine(p));
  if (p.models_error) {
    parts.push('<div class="calib-line warn">' + esc(t('nodes.models_unreadable')) +
      ': ' + esc(p.models_error) + '</div>');
  }

  /* Die drei Entscheidungen plus der Name — mit den gemessenen Vorgaben. */
  const sites = ((c && c.sites) || []).map(s => s.id);
  if (p.site_derived && sites.indexOf(p.site_derived) === -1) sites.push(p.site_derived);
  const siteOpts = ['<option value="">—</option>'].concat(sites.map(id =>
    '<option value="' + esc(id) + '"' +
    (id === nodeState.draft.site ? ' selected' : '') + '>' + esc(id) + '</option>')).join('');
  const roleOpts = NODE_ROLES.map(([v, id]) =>
    '<option value="' + v + '"' + (v === nodeState.draft.role ? ' selected' : '') + '>' +
    esc(t(id)) + '</option>').join('');

  parts.push('<div class="node-form node-draft">' +
    nodeField('nodeName', t('nodes.name'), nodeState.draft.name, 'text') +
    nodeField('nodeWeight', t('nodes.weight'), nodeState.draft.weight, 'number') +
    nodeSelect('nodeSite', t('nodes.site'), siteOpts) +
    nodeSelect('nodeRole', t('nodes.role'), roleOpts) +
    '</div>');

  parts.push('<div class="repl-actions">' +
    '<button class="btn" onclick="nodeAdd()"' +
      (keyed && !nodeState.busy ? '' : ' disabled') + '>' +
      esc(t(nodeState.busy ? 'nodes.adding' : 'nodes.add')) + '</button>' +
    '<button class="btn" onclick="nodeCancel()">' + esc(t('nodes.cancel')) + '</button>' +
    (keyed ? '' : '<span class="hint">' + esc(keyHelp()) + '</span>') +
    '</div>');
  return parts.join('');
}

function renderNodeFound() {
  const r = nodeState.found;
  if (!r) {
    return nodeState.busy
      ? '<div class="calib-line">' + esc(t('nodes.scanning')) + '</div>' : '';
  }
  if (!r.count) {
    return '<div class="calib-line">' +
      esc(t('nodes.found_none', {cidr: r.cidr || '—'})) + '</div>';
  }
  const head = '<div class="calib-line num">' +
    esc(t('nodes.found_n', {n: nf(r.count), cidr: r.cidr || '—'})) + '</div>';
  return head + (r.found || []).map(f => {
    const known = f.known
      ? '<span class="warn">' + esc(t('nodes.known', {nodes: (f.known_nodes || []).join(', ')})) +
        '</span>' : '';
    return '<div class="node-row" data-key="found-' + esc(f.address) + '">' +
      '<span class="node-addr num">' + esc(f.address) + '</span>' +
      '<span class="node-name">' + esc(f.name || '—') + '</span>' +
      (f.node_id ? '<span class="chip">' + esc(f.node_id) + '</span>' : '') +
      '<span class="num">' + T('node_scan', esc(t('nodes.models')) + ' ' + nf(f.model_count)) +
        '</span>' +
      '<span class="num">' + T('node_scan', nf(f.latency_ms, 1) + ' ms') + '</span>' +
      (f.site_derived ? '<span class="chip">' + esc(f.site_derived) + '</span>'
                      : '<span class="warn">' + esc(t('nodes.site_unknown')) + '</span>') +
      known +
      '<button class="btn" onclick="nodePick(\'' + esc(f.address) + '\')"' +
        (f.known ? ' disabled' : '') + '>' + esc(t('nodes.pick')) + '</button>' +
      '</div>';
  }).join('');
}

function renderNodeDrain() {
  const d = nodeState.drain;
  if (!d) return '';
  const deadline = d.deadline;
  const models = (d.models || []).map(m =>
    '<span class="chip">' + esc(m.model || m) + ' ×' +
    esc(nf(m.inflight || 0)) + '</span>').join(' ');
  const parts = [];
  parts.push('<div class="node-dialog" data-key="drain-' + esc(d.name) + '">');
  parts.push('<div class="calib-head"><strong>' +
    esc(t('nodes.drain_title', {name: d.name})) + '</strong>' +
    '<span class="chip ' + (d.inflight > 0 ? 'warn' : 'ok') + '">' +
      esc(d.inflight > 0 ? t('nodes.drain_inflight', {n: nf(d.inflight)})
                         : t('nodes.drain_idle')) + '</span>' +
    (d.source ? '<span class="chip">' + T('node_source', esc(d.source)) + '</span>' : '') +
    '</div>');
  if (models) {
    parts.push('<div class="calib-line num">' + esc(t('nodes.drain_models')) + ' ' +
      models + '</div>');
  }
  if (deadline !== null && deadline !== undefined) {
    parts.push('<div class="calib-line num">' + T('node_drain',
      esc(t('nodes.drain_wait', {v: nf(deadline, 0)}))) + '</div>');
  }
  parts.push('<p class="hint">' + esc(t('nodes.drain_note')) + '</p>');
  parts.push('<div class="repl-actions">' +
    '<button class="btn" onclick="nodeDrainConfirm()"' +
      (d.busy ? ' disabled' : '') + '>' + esc(t('nodes.drain_confirm')) + '</button>' +
    '<button class="btn" onclick="nodeDrainCancel()">' + esc(t('nodes.drain_cancel')) +
      '</button>' +
    '<button class="btn" onclick="nodeDrainDisable()"' +
      (d.busy ? ' disabled' : '') + '>' + esc(t('nodes.drain_disable')) + '</button>' +
    '</div>');
  parts.push('</div>');
  return parts.join('');
}

function renderNodeMsg() {
  const m = nodeState.msg;
  if (!m) return '';
  return '<div class="' + esc(m.cls || 'warn') + '"' +
    (m.tip ? ' data-tip="' + esc(m.tip) + '"' : '') + '>' + esc(m.text) + '</div>';
}

function renderNodeList(h) {
  const keyed = !!adminKey();
  const hosts = (h && h.hosts) || [];
  if (!hosts.length) return '<div class="calib-line">' + esc(t('panel.nodes.empty')) + '</div>';
  return hosts.map(x => {
    /* Ein Knoten aus `config.yaml` ist die Basis und wird nie gelöscht (#70):
       der Knopf ist aus und sagt warum, statt den 409 abzuwarten. */
    const fixed = x.source === 'config';
    const why = !keyed ? keyHelp() : fixed ? t('nodes.remove_config') : '';
    return '<div class="node-row" data-key="node-' + esc(x.name) + '">' +
      '<span class="node-name">' + esc(x.name) + '</span>' +
      chip(x.state, x.phase) +
      '<span class="node-addr num">' + esc(x.address || '—') + '</span>' +
      '<span class="node-src">' + T('node_source', esc(x.source || '—')) + '</span>' +
      '<span class="node-inflight num">' + T('node_inflight', esc(nf(x.inflight || 0))) +
        '</span>' +
      '<button class="btn node-remove" onclick="nodeAskRemove(\'' + esc(x.name) + '\')"' +
        (keyed && !fixed && !x.draining ? '' : ' disabled') +
        (why ? ' title="' + esc(why) + '"' : '') + '>' +
        esc(t('nodes.remove_btn')) + '</button>' +
      '</div>';
  }).join('');
}

/* Was getippt wird, wandert in den Zustand — der nächste Tick baut daraus
   dieselbe Zeichenkette, und deshalb schreibt er nichts. Die Feld-Ids sind die
   einzige Zuordnung, die nötig ist. */
function nodeInputChanged(el) {
  if (!el || !el.id) return;
  const d = nodeState.draft;
  if (el.id === 'nodeName') d.name = el.value;
  else if (el.id === 'nodeWeight') d.weight = el.value;
  else if (el.id === 'nodeSite') d.site = el.value;
  else if (el.id === 'nodeRole') d.role = el.value;
}
document.addEventListener('input', ev => nodeInputChanged(ev.target));
document.addEventListener('change', ev => nodeInputChanged(ev.target));

/* The filter bar of the decision log (issue #101). It sits in the shell, so
   these two listeners are the whole wiring: `input` for the sid field (each
   keystroke narrows the table), `change` for the two selects. Both only
   re-render `#log`; the field keeps its text, its selection and its focus
   because no tick ever touches it. */
document.addEventListener('input', ev => {
  const el = ev.target;
  if (el && el.id === 'logFilterSid') logSetFilter({sid: el.value});
});
document.addEventListener('change', ev => {
  const el = ev.target;
  if (!el) return;
  if (el.id === 'logFilterReason') logSetFilter({reason: el.value});
  else if (el.id === 'logFilterHost') logSetFilter({host: el.value});
});

/* ── DOM-Patch statt Vollreload ───────────────────────────────────────────── */
/* Each tick builds HTML strings and pushes them through `apply()`. If the
   string is identical to the last one, nothing is touched at all — that alone
   is what keeps the 2 s tick from flickering. Otherwise the children are
   reconciled one by one: same tag + same `data-key` stays, only changed text
   and attributes are written. The scroll container of the decision log is
   never replaced, so its scroll position survives, and a hovered tooltip does
   not disappear under the pointer. */
function apply(id, html) {
  const host = $(id);
  if (!host || host.__html === html) return;
  host.__html = html;
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  sync(host, tpl.content);
}

function sameKind(x, y) {
  if (!x || !y || x.nodeType !== y.nodeType) return false;
  if (x.nodeType !== 1) return true;
  if (x.tagName !== y.tagName) return false;
  return (x.getAttribute('data-key') || '') === (y.getAttribute('data-key') || '');
}

function sync(host, src) {
  const a = Array.from(host.childNodes);
  const b = Array.from(src.childNodes);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i], y = b[i];
    if (!y) { host.removeChild(x); continue; }
    if (!x) { host.appendChild(document.importNode(y, true)); continue; }
    if (!sameKind(x, y)) { host.replaceChild(document.importNode(y, true), x); continue; }
    if (x.nodeType === 3) { if (x.nodeValue !== y.nodeValue) x.nodeValue = y.nodeValue; continue; }
    if (x.nodeType !== 1) continue;
    if (x.outerHTML === y.outerHTML) continue;
    for (const at of Array.from(y.attributes)) {
      if (x.getAttribute(at.name) !== at.value) x.setAttribute(at.name, at.value);
    }
    for (const at of Array.from(x.attributes)) {
      if (!y.hasAttribute(at.name)) x.removeAttribute(at.name);
    }
    sync(x, y);
  }
}

/* The collapse row is a plain click target: the table is patched anyway, so a
   toggle only has to flip the flag and re-render the one container. */
function setDim(on) {
  showDim = !!on;
  if (last) apply('models', renderModels(last.h));
  return showDim;
}
document.addEventListener('click', ev => {
  const row = ev.target && ev.target.closest ? ev.target.closest('[data-toggle="dim"]') : null;
  if (row) setDim(!showDim);
});

/* ── The update offer (issue #123) ────────────────────────────────────────── */
/* The proxy asks the public GitHub releases API once at start-up and reports
   the answer in `/healthz` → `update`. The page only draws it — and only for
   `newer`: `current` (including a local build *ahead* of the last release),
   `unknown` (no network, a timeout, a 404) and `disabled` (the config switch)
   return the empty string, so the header never grows a line because a hint
   failed. The command is a literal, not a catalog entry — a command is not
   translated — the sentence around it is. */
function renderUpdate(h) {
  const u = (h && h.update) || {};
  if (u.state !== 'newer' || !u.latest) return '';
  const href = u.url ? ' href="' + esc(u.url) + '" target="_blank" rel="noopener"' : '';
  return '<a class="update-link"' + href + ' data-tip="' + esc(t('update.tip')) + '">' +
    esc(t('update.available', {v: u.latest})) + ' <code>ripple update</code></a>';
}

/* ── Ein Tick ─────────────────────────────────────────────────────────────── */
function render(h, s, c) {
  rememberCapability(c);
  apply('legend', renderLegend());
  apply('banners', renderBanners(h));
  apply('fleet', renderFleet(h));
  apply('kpis', renderKpis(h, s));
  apply('models', renderModels(h));
  apply('dist', renderDist(h, s));
  apply('threads', renderThreads(h));
  apply('load', renderLoad(h));
  apply('log', renderLog(s));
  /* The bar above the log (issue #101): its selects offer what this payload
     holds. The fields themselves are in the shell, so the patch above cannot
     take their text or their focus. */
  fillLogFilters(s);
  /* `applyNode`, not `apply`: a field the operator is typing in must not be
     replaced by the 2 s tick (#78, same rule as the node tab). */
  applyNode('params', renderParams(c));
  apply('replPanel', renderReplication(h));
  apply('policyPanel', renderPolicyPanel());
  apply('restartPanel', renderRestart(h));
  /* Der Knoten-Tab (issue #71). Er hängt nicht an `apply()` direkt, sondern an
     `applyNode()`: ein Feld, in dem gerade getippt wird, wird nicht ersetzt. */
  paintNodes(h, c);
  /* Der Patch ersetzt Knoten; ein ersetzter Knoten feuert kein `pointerout`.
     Ohne diese Zeile bliebe der Kasten mit dem Text eines Elements stehen,
     das es nicht mehr gibt. */
  if (tipOwner && !tipOwner.isConnected) tipHide();
  apply('snapTime', T('taken', esc(clock(new Date()))));
  /* The update offer (issue #123): the header line, or nothing. It hangs on the
     same tick as everything else, so a language switch redraws it without a
     reload and an answer that arrives late appears within 2 s. */
  apply('updateHint', renderUpdate(h));
  /* Die Herkunft der Zahlen („Live-Abzug von /healthz und /stats") steht als
     Tooltip an der Uhr statt als eigene Zeile im Kopf (#57). Jeder Tick setzt
     sie neu, damit ein Sprachwechsel ohne Reload mitgeht. */
  const snapSrc = document.querySelector('.snap-src');
  const snapWhen = document.getElementById('snapWhen');
  if (snapSrc && snapWhen) snapWhen.setAttribute('data-tip', snapSrc.textContent.trim());
  const b = h.build || {};
  apply('conn', esc(t('conn.connected', {v: h.version || '—'})) +
    (b.commit ? ' · ' + esc(b.commit) + (b.dirty ? '-dirty' : '') : '') +
    (b.date ? ' (' + esc(b.date) + ')' : '') +
    ' ' + esc(t('conn.uptime', {n: nf((h.uptime_seconds || 0) / 60, 0)})));
}

let last = null;   // the payload the page currently shows, for a local re-render

async function tick() {
  try {
    const [h, s, c] = await Promise.all([
      getHealth(),
      fetch('/stats').then(r => r.json()),
      fetch('/config').then(r => r.json()),
    ]);
    last = {h, s, c};
    /* Der /healthz-Abzug trägt die Machbarkeit nicht (#126): sie kommt aus der
       zuletzt gelesenen Admin-Sicht und wird hier an die neuen Zeilen gelegt,
       damit der Warnzustand den 2-s-Takt überlebt. */
    mergeFeasibility(h);
    render(h, s, c);
    /* Eine Änderung an der Policy ist der eine Moment, in dem die Admin-Sicht
       der Regeln veraltet ist: einmal nachlesen, im ersten Tick danach. Ohne
       Schlüssel gibt es nichts zu lesen, und ein Tick ohne Änderung stellt keine
       Admin-Anfrage — der 2-s-Takt bleibt bei drei Requests. */
    if (policyStale) {
      policyStale = false;
      await loadPolicyOrphans();
    }
  } catch (e) {
    /* Keep what is on screen — a lost tick is not a reason to empty the page.
       The last successful numbers stay, the footer says they are old. */
    apply('conn', esc(t('conn.lost', {msg: e && e.message ? e.message : e})));
  }
}

/* Start: erst die Kataloge, dann die Sprache (GUI-Wahl, sonst Systemsprache,
   sonst Englisch) und der erste Tick. Ohne Katalog läuft die Seite mit ids
   weiter — sie bleibt lesbar, statt leer zu sein. `ready` ist dasselbe
   Versprechen für die Testharnesse: erst wenn es erfüllt ist, sind Kataloge
   und erster Tick da. */
const ready = (async () => {
  await loadCatalogs();
  applyLang(storedLang() || systemLang() || FALLBACK);
  const sel = $('langSel');
  if (sel && typeof sel.addEventListener === 'function') {
    sel.addEventListener('change', ev => chooseLang(ev.target.value));
  }
  /* Der Tab kommt aus dem Hash, nicht aus einem gespeicherten Zustand: ein
     Reload mit `#models` landet auf `Modelle`, ein Reload ohne Hash auf
     `Flotte`. */
  applyHash();
  await tick();
  /* Der Warnzustand am Schloss muss vor dem ersten Klick da sein (#126): die
     Machbarkeit steht nur in der Admin-Sicht, nicht in /healthz. Einmal, mit
     Schlüssel, nach dem ersten Tick — ohne Schlüssel kostet sie keinen Request. */
  await loadFeasibility();
  startTick();
  /* Der 1-s-Takt der Fristen lebt neben dem 2-s-Patch: er schreibt nur den Text
     der `data-deadline`-Elemente und rendert keinen Block neu (#34). */
  startDeadlines();
})();

/* Exported for the layout test (tests/test_dashboard.py): the render functions
   are pure string builders, so a DOM stub can measure what they produce. */
globalThis.RIPPLE = {
  renderFleet, renderKpis, renderModels, renderDist, renderThreads, renderLog,
  /* Der Verlauf (#101): die Kurzform eines Thread-Schlüssels, das Prädikat der
     drei Filter, der eine Schreiber des Filterzustands und die zwei Auswahlen,
     die der Tick aus dem Puffer füllt — gemessen statt am Bild beurteilt. */
  logShortSid, logMatch, logSetFilter, fillLogFilters,
  get logFilter() { return logFilter; },
  renderParams, renderLegend, renderBanners, render, tick, apply, setDim,
  /* The update offer (#123): the one string builder, measured against a payload
     in both languages instead of being eyeballed in a browser. */
  renderUpdate,
  /* Der Parameterblock (#78): die zwei Tabellen (mit und ohne `editable`), die
     Pfadprüfung, das Lesen eines getippten Werts und die drei Aufrufe, die eine
     Zeile auslöst — der Test misst Payload und URL, nicht die Absicht. */
  paramsTableLegacy, paramsTableLive, paramsPattern, paramsEditable,
  paramsOverridden, paramsRead, paramsSubmit, paramsReset, paramsCommit,
  paramsKey, paramsWeightQuestion,
  /* Der Fahrtenschreiber (#83): die zwei Reihen, ihre Geometrie und der Block,
     den eine Karte zeichnet — die Tests rechnen die gezeichneten Punkte gegen
     die Zahlen des Vertrags, statt ein Bild zu beurteilen. */
  sparkline, hostSeries, seriesRows, seriesPath, seriesDots, seriesGeom, windowLabel,
  seriesValueAt,
  /* Der Zeiger auf der Kurve (#102): der Index aus einer Zeigerposition, der
     Readout eines Buckets und der Zustand, den ein Patch mitnehmen muss. */
  sparkIndex, sparkReadout, sparkValue, sparkX, sparkHoverSet, sparkHoverClear, sparkMove,
  get sparkHover() { return sparkHover; },
  SPARK_W, SPARK_H, SPARK_PAD, SPARK_HOLD,
  /* Die relative Last (#80): die eine Zelle, die Gewicht und Last nebeneinander
     setzt — der Test rendert sie gegen einen Payload mit und ohne Messung. */
  relLoad,
  maintControl, maintenance, maintenanceClear, maintLine,
  policyControl, modelPolicy, policyLine, policyPath,
  lockButton, lockTip, lockPath, lockLine, modelLock, lockWarn,
  applyFeasibility, loadFeasibility,
  renderPolicyPanel, loadPolicyOrphans, policyOrphanRow,
  renderReplication, replicationCheck, replicationStart, replicationCancel,
  replState,
  /* The per-host ledger (#33): the block, the one click behind the admin key and
     the formatters its cells use. */
  renderLoad, loadReset, loadToggleIdle, loadSetHideIdle, loadDur, loadWhen,
  loadShare, LOAD_STATES, LOAD_WORK,
  /* The delete control of a thread (#35): the click behind the admin key. */
  threadDelete,
  /* Die Quelle eines Threads im Chip (#103): `new` ist die Geburt, nicht das
     Leben — der Test ruft die Regel direkt auf statt sie im Markup zu suchen. */
  threadSource,
  /* The restart control (#25): the panel, the click, the wait after the 202 and
     the state the page keeps — the request accounting is measured through them. */
  renderRestart, restartNow, restartForce, restartState, waitForRestart,
  isNewProcess, startTick, stopTick, RESTART_PATH,
  /* The refusal cause as a code (#29): the catalog lookup and the block that
     renders it, so the harness measures all five codes instead of one. */
  restartReason, restartWhy, RESTART_REASONS,
  /* i18n (#17): `t`, the language switch and the two formatters are the page's
     public surface — the tests drive them directly instead of guessing at the
     catalogs. */
  t, chooseLang, applyLang, fillShell, loadCatalogs, LANGS, nf, clock,
  get lang() { return lang; },
  get catalogs() { return catalogs; },
  /* Die Tabs (#56, fünf seit #73): der Katalog der fünf, der Weg vom Hash in
     die Seite und die reine Sicht, die Leiste und Panels beschreibt —
     gemessen, nicht am Bild beurteilt. */
  TABS, DEFAULT_TAB, hashTab, tabView, setTab, applyHash, paintTabs,
  get activeTab() { return activeTab; },
  /* Der Knoten-Tab (#71): die Vorschau, die Suche, der Drain-Dialog und der
     Zustand, den sie teilen — damit der Harness „was getippt wird, überlebt den
     Tick" misst statt es zu glauben. */
  renderNodePreview, renderNodeFound, renderNodeDrain, renderNodeList,
  renderNodeMsg, nodePreview, nodeScan, nodeAdd, nodeCancel, nodePick,
  nodeAskRemove, nodeDrainConfirm, nodeDrainDisable, nodeDrainCancel,
  nodeError, nodeRunning, nodeReadDraft, nodeInputChanged, paintNodes,
  applyNode, NODE_STEPS, NODE_ROLES, nodeState,
  /* The deadlines (#34): the clock the page counts against, the two texts and
     the 1 s ticker — measured directly instead of guessing at a timer. */
  anchorClock, pageNow, deadlineText, paintDeadlines, startDeadlines,
  countdown, countdownMin,
  get clockOffset() { return clockOffset; },
  /* Der Admin-Modus im Masthead (#130): der Chip, das eine Feld, die
     Verifikation vor der Anzeige und der Abmeldeweg — der Test fährt die
     Zustände durch die echten Funktionen und misst, was im Storage landet. */
  adminPaint, adminOpen, adminToggle, adminSubmit, adminSignOut, adminBooted,
  adminSay, adminIsOpen,
  get adminState() { return adminState; },
  ready,
};
