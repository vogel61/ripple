"""The page's languages (issue #17): catalog, lookup, fallback, selector, numbers.

The page is hand-written JavaScript with no build step, so the honest units are:

- the catalogs (`ripple/static/i18n/en.json`, `de.json`): flat `id → text`, the
  same key set in both, and every id the page asks for is in them;
- the shell (`ripple/dashboard.py`): it carries ids, not words — one HTML, no
  per-language copy;
- the behaviour, executed for real: `HARNESS` runs `dashboard.js` under a DOM
  stub with a given `navigator.languages` and a given `localStorage`, and hands
  back what the page rendered. That is how "first start in English", "the choice
  survives a reload" and "1.234,5 against 1,234.5" are measured instead of
  eyeballed.

The German words live in `de.json`; the code and the tests name ids. That is
why the last group checks the two source files against a list of German words:
a German label typed into `dashboard.js` would be invisible to a translator and
would win over the catalog.
"""

from __future__ import annotations

import html.parser
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from ripple.dashboard import render_dashboard

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "ripple" / "static"
I18N = STATIC / "i18n"
NODE = shutil.which("node")

LANGS = ("en", "de")

# The vocabulary the page reads from /healthz (`ripple/routing.py`) — a reason
# token is data, its sentence is a catalog entry. A new token needs a new id.
REASON_TOKENS = (
    "thread", "new_thread", "ephemeral", "one_shot", "rebind", "fallback",
    "slot_evict", "model_pinned", "degraded_host", "conflict", "no_healthy_host",
    "unknown_model", "model_list_unavailable", "no_candidate",
)

# German words that must not appear in the code. Not a heuristic over every
# word — a list of the labels the page actually had before this issue, so a
# forgotten string is caught where it would be typed.
GERMAN = (
    "Wartung", "Speicher", "geladen", "verfügbar", "Anfrage", "Knoten", "Modell",
    "belegt", "Zustand", "Schlüssel", "Fenster", "Regel", "Spalte", "Betrieb",
    "Kalibrierung", "Replikation", "Parameter", "Flotte", "Verteilung",
    "Momentaufnahme", "übrig", "geschätzt", "Grenze", "Reserve", "Treffer",
    "verdrängt", "Proben", "Messung", "Zeichen", "Dauer", "Grund", "Einträge",
    "Züge", "gesperrt", "verbergen", "aufheben", "beenden", "prüfen",
    "übernommen", "kopierbar", "verweigert", "Zeile", "Sperre", "verdrängen",
    "Sprache", "lädt", "nicht", "kein",
)


# The shape words of `legend.note`, one row per catalog: the droplet of
# `draining`, the hollow triangle of `disabled`, the word for the fill (the two
# triangles differ by it, not by the silhouette), and the five-shape list of #17
# that must be gone. `test_every_catalog_names_all_seven_shapes_in_the_legend_note`
# walks this table against the directory.
LEGEND_SHAPES = {
    "cs": ("kapka", "prázdný trojúhelník", "výplň", "kruh, půlkruh, trojúhelník, prázdný čtverec, kříž"),
    "de": ("Tropfen", "hohles Dreieck", "Füllung", "Kreis, halber Kreis, Dreieck, offenes Quadrat, Kreuz"),
    "el": ("σταγόνα", "κοίλο τρίγωνο", "γέμισμα", "κύκλος, μισός κύκλος, τρίγωνο, ανοιχτό τετράγωνο, σταυρός"),
    "en": ("droplet", "hollow triangle", "fill", "circle, half circle, triangle, open square, cross"),
    "es": ("gota", "triángulo hueco", "relleno", "círculo, semicírculo, triángulo, cuadrado abierto, cruz"),
    "fr": ("goutte", "triangle évidé", "remplissage", "cercle, demi-cercle, triangle, carré ouvert, croix"),
    "hr": ("kap", "šuplji trokut", "ispunu", "krug, polukrug, trokut, otvoreni kvadrat, križ"),
    "hu": ("csepp", "üreges háromszög", "kitöltés", "kör, félkör, háromszög, nyitott négyzet, kereszt"),
    "it": ("goccia", "triangolo vuoto", "riempimento", "cerchio, semicerchio, triangolo, quadrato aperto, croce"),
    "pl": ("kropla", "pusty trójkąt", "wypełnienie", "koło, półkole, trójkąt, otwarty kwadrat, krzyżyk"),
    "pt": ("gota", "triângulo vazado", "preenchimento", "círculo, semicírculo, triângulo, quadrado aberto, cruz"),
    "ru": ("капля", "полый треугольник", "заливка", "круг, полукруг, треугольник, открытый квадрат, крест"),
    "sk": ("kvapka", "dutý trojuholník", "výplň", "kruh, polkruh, trojuholník, otvorený štvorec, krížik"),
    "sl": ("kaplja", "votel trikotnik", "polnilo", "krog, polkrog, trikotnik, odprt kvadrat, križ"),
    "tr": ("damla", "içi boş üçgen", "dolgu", "daire, yarım daire, üçgen, açık kare, çarpı"),
    "zh": ("水滴", "空心三角", "填充", "圆、半圆、三角、空心方、叉"),
}


# -- the catalogs -----------------------------------------------------------
@pytest.fixture(scope="module")
def en() -> dict:
    return json.loads((I18N / "en.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def de() -> dict:
    return json.loads((I18N / "de.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "dashboard.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page() -> str:
    return render_dashboard()


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "dashboard.css").read_text(encoding="utf-8")


def test_the_catalogs_are_flat_id_to_text(en, de):
    """`id → text`, nothing nested: a flat file is what makes "same key set"
    and "no empty string" checkable in one line."""
    for name, cat in (("en", en), ("de", de)):
        assert isinstance(cat, dict) and cat, name
        for key, text in cat.items():
            assert isinstance(key, str) and key, name
            assert "." in key, (name, key)          # `area.name`, not a sentence
            assert isinstance(text, str) and text.strip(), (name, key)


def test_the_two_catalogs_have_the_same_keys(en, de):
    """Parity in both directions: no missing translation, no orphan key."""
    assert sorted(set(en) - set(de)) == []          # nothing untranslated
    assert sorted(set(de) - set(en)) == []          # nothing invented


def test_every_catalog_on_disk_is_wired_complete_and_not_a_copy(en, js, page):
    """A language is a file plus two lists, and the three must agree.

    Adding a catalog is three edits (`i18n/<code>.json`, `LANGS` in
    `dashboard.js`, an `<option>` in `dashboard.py`) and it is easy to forget
    one. A file nobody offers is dead weight; an option whose file is missing
    is a 404 in the operator's browser. This test walks the directory instead of
    a hardcoded list, so the next language is covered the moment it lands.
    """
    codes = sorted(p.stem for p in I18N.glob("*.json"))
    assert "en" in codes and "de" in codes, codes
    for code in codes:
        cat = json.loads((I18N / f"{code}.json").read_text(encoding="utf-8"))
        # 1. the same key set as the source, in both directions
        assert sorted(set(cat) - set(en)) == [], (code, sorted(set(cat) - set(en))[:5])
        assert sorted(set(en) - set(cat)) == [], (code, sorted(set(en) - set(cat))[:5])
        # 2. no empty string and no placeholder lost or renamed
        for key, text in cat.items():
            assert isinstance(text, str) and text.strip(), (code, key)
            want = sorted(re.findall(r"\{\w+\}", en[key]))
            got = sorted(re.findall(r"\{\w+\}", text))
            assert got == want, (code, key, want, got)
            # Was in Backticks steht, ist ein Name oder eine Formel und bleibt
            # stehen (`deficit`, `max_inflight`, `placement.imbalance()`,
            # `gain ≥ cost × factor`). #144 hat hier zum ersten Mal Prosa in
            # Backticks eingeführt; ohne diese Zeile übersetzt sie jedes Kind
            # anders und der Tooltip zeigt je nach Sprache etwas anderes.
            assert sorted(re.findall(r"`([^`]+)`", text)) == \
                sorted(re.findall(r"`([^`]+)`", en[key])), (code, key)
        # 3. wired in both lists
        assert re.search(r"code:\s*'" + code + r"'", js), f"{code} missing in LANGS"
        assert f'<option value="{code}"' in page, f"{code} missing in the selector"
        # 4. a translated catalog, not an English copy (German has its own test)
        if code != "en":
            differing = sum(1 for k, v in cat.items() if v != en[k])
            assert differing >= 0.6 * len(en), (code, differing, len(en))


def test_the_english_parameter_help_is_the_schema_text_word_for_word(en):
    """#144: `en.json` and `FIELD_HELP` are two copies of the same 29 sentences.

    The page asks the catalog first and the text from `/config` second, so the two
    copies must not drift: an English edit that lands in only one of them would
    make the tooltip say different things depending on whether the id is present.
    Every other catalog is free to translate; this test only guards the source.
    """
    from ripple.config import FIELD_HELP

    ids = {k[len("params.help."):]: v for k, v in en.items() if k.startswith("params.help.")}
    assert ids == FIELD_HELP
    assert len(FIELD_HELP) == 29


def test_every_catalog_names_all_seven_shapes_in_the_legend_note():
    """The note under the legend is a promise, and #70 broke it silently.

    It said "every state has its own shape" and then listed five, because there
    were five. #70 added `draining` and `disabled`, #79 gave them a droplet and a
    hollow triangle and corrected `docs/install*.md` and `docs/manual-en.md` —
    but not the catalogs, so the page kept telling the operator about five shapes
    for seven states. The list must name seven, in legend order, and it must
    speak about the *fill* as well: `degraded` and `disabled` share the triangle
    silhouette, so "its own shape" alone would be a contradiction.

    One row per language — a new catalog without its shapes fails here rather
    than shipping a sentence nobody can check. The last element is the old
    five-shape enumeration; it must be gone.
    """
    codes = sorted(p.stem for p in I18N.glob("*.json"))
    assert set(codes) == set(LEGEND_SHAPES), sorted(set(codes) ^ set(LEGEND_SHAPES))
    for code in codes:
        cat = json.loads((I18N / f"{code}.json").read_text(encoding="utf-8"))
        note = cat["legend.note"]
        droplet, hollow, fill, five = LEGEND_SHAPES[code]
        # the two shapes #79 added
        assert droplet in note, (code, "no droplet", note)
        assert hollow in note, (code, "no hollow triangle", note)
        assert note.index(droplet) < note.index(hollow), (code, "droplet after hollow triangle")
        # the promise covers the fill, not just the outline
        assert fill in note, (code, "no word for the fill", note)
        # seven items, not five
        body = note.split("—")[-1].split(".")[0].split("。")[0]
        items = [i.strip(" —") for i in re.split(r"[,、]", body) if i.strip(" —")]
        assert len(items) == 7, (code, items)
        # and the five-shape sentence of #17 is not lying there any more
        assert five not in note, (code, "old five-shape list still present", five)


def test_the_english_catalog_is_the_original(en):
    """`en.json` is the source of truth, so it is English: a German umlaut in it
    would mean the original was typed in German and never translated."""
    assert [k for k, v in en.items() if re.search(r"[äöüßÄÖÜ]", v)] == []


def test_the_german_catalog_is_not_a_copy_of_the_english_one(en, de):
    """A translation pass that copied the English would pass every parity test.

    Parameter names and state tokens stay identical on purpose (`weight → share`,
    `disabled`), so the assertion is a share of differing sentences plus three
    spot checks on prose that must be German.
    """
    differing = [k for k in en if en[k] != de[k]]
    assert len(differing) >= 0.6 * len(en), (len(differing), len(en))
    for key in ("sec.fleet", "key.help", "fleet.used_pct", "tip.used"):
        assert en[key] != de[key], key


def test_every_id_the_script_asks_for_is_in_the_catalog(js, en, de):
    """A missing id falls back to English — but a *used* id that is missing in
    English too would render as the id, so the literal ids are checked."""
    literals = {i for i in re.findall(r"\bt\('([a-z][a-z0-9_.]+)'", js)
                if not i.endswith(".")}          # `t('tip.' + key)` is a family
    literals |= {"tip." + k for k in re.findall(r"\bT\('([a-z][a-z0-9_]+)'", js)}
    literals |= set(re.findall(r"'((?:maint\.preset|models\.policy)_[a-z0-9_]+)'", js))
    assert literals, "the scan found no ids — the pattern is wrong"
    assert sorted(literals - set(en)) == []
    assert sorted(literals - set(de)) == []


def test_every_tip_key_has_exactly_one_catalog_entry(js, en):
    """`T(key, …)` builds `tip.<key>` from the source table; the two sides must
    cover each other, or a tooltip silently loses its label."""
    found = re.search(r"const SRC = \{(.*?)\n\};", js, re.S)
    assert found, "the source table is gone"
    src = found[1]
    keys = set(re.findall(r"^\s{2}([a-z_][a-z0-9_]*):", src, re.M))
    assert len(keys) > 50, "the source table did not parse"
    tips = {k for k in en if k.startswith("tip.")}
    assert sorted({"tip." + k for k in keys} ^ tips) == []


def test_the_policy_select_shows_a_word_and_sends_the_token(js, en, de):
    """`disabled` is what the endpoint stores; the label is a catalog entry. It
    used to be the token itself, which is how the German page ended up with an
    English word in it."""
    assert "['disabled', 'models.policy_disabled']" in js
    assert "['hidden', 'models.policy_hidden']" in js
    assert en["models.policy_disabled"] == "disabled"      # the same word …
    assert de["models.policy_disabled"] == "deaktiviert"   # … and not here


def test_every_reason_token_has_a_sentence(en, de):
    for token in REASON_TOKENS:
        assert "reason." + token in en, token
        assert "reason." + token in de, token


def test_a_measurement_age_has_one_id_per_unit():
    """Das Alter einer Messung wird in seiner Einheit gelesen (issue #80).

    Die Ids sind der Vertrag: `fleet.ago` (die Zahl in Sekunden) darf nicht
    zurückkommen — sie war in sieben der 16 Sprachen breiter als die Zelle und
    las sich als „86,400 s ago" statt „1 d ago". Vier Einheiten, ein Platzhalter,
    in jedem Katalog auf der Platte: der Test läuft über das Verzeichnis, nicht
    über eine Liste, die man beim nächsten Katalog vergisst.
    """
    ids = ("fleet.ago_s", "fleet.ago_min", "fleet.ago_h", "fleet.ago_d")
    paths = sorted(I18N.glob("*.json"))
    assert len(paths) == 16, [p.name for p in paths]
    for path in paths:
        cat = json.loads(path.read_text(encoding="utf-8"))
        assert "fleet.ago" not in cat, path.name
        for i in ids:
            assert i in cat, (path.name, i)
            assert "{v}" in cat[i], (path.name, i)


def test_no_catalog_entry_is_dead_weight(js, page, en):
    """Every id is reachable: a literal in the script, an attribute in the
    shell, or one of the four families the script builds at runtime."""
    quoted = set(re.findall(r"'([a-z][a-z0-9_.]+)'", js))
    shell = set(re.findall(r'data-i18n(?:-html|-aria)?="([^"]+)"', page))
    # `params.help.<pfad>` steht als Literal nirgends im Skript: die Seite baut die
    # id aus dem Pfad der Zeile (#144). Dass genau die 29 Pfade des Schemas drin
    # stehen und kein erfundener, prüft
    # `test_the_english_parameter_help_is_the_schema_text_word_for_word`.
    families = ("tip.", "state.", "reason.", "phase.", "params.help.")
    unused = sorted(k for k in en
                    if k not in quoted and k not in shell
                    and not k.startswith(families))
    assert unused == []


# -- the shell --------------------------------------------------------------
def test_the_shell_carries_ids_instead_of_words(page):
    """One HTML, no per-language copy: every visible label is an attribute."""
    ids = re.findall(r'data-i18n(?:-html|-aria)?="([^"]+)"', page)
    assert len(ids) >= 40, len(ids)
    for required in ("app.title", "app.snapshot", "sec.fleet", "sec.models",
                     "sec.dist", "sec.threads", "sec.log", "sec.ops", "sec.maint",
                     "sec.policy", "sec.calib", "sec.repl", "sec.params",
                     "foot.source", "foot.readonly", "conn.connecting",
                     "panel.maint.idle", "panel.policy.idle", "panel.calib.idle",
                     "panel.repl.idle", "lang.label"):
        assert required in ids, required


def test_every_shell_id_exists_in_both_catalogs(page, en, de):
    ids = set(re.findall(r'data-i18n(?:-html|-aria)?="([^"]+)"', page))
    assert sorted(ids - set(en)) == []
    assert sorted(ids - set(de)) == []


def test_the_shell_keeps_the_words_out_of_the_markup(page, de):
    """The German title of the page is in `de.json`, not in the HTML — that is
    the whole point of the attribute."""
    assert de["app.title"] not in page
    assert de["sec.fleet"] not in page


class _Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.found = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ("meta", "link", "br", "img", "input", "hr"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_data(self, data):
        if data.strip():
            self.found.append((self.stack[-1] if self.stack else "?", data.strip()))


# What may be text in the shell: the wordmark (a name), the two language names
# (the language's own name is never translated), the em dash of an empty value,
# the section numbers, the FA-22 badge, the three endpoint paths — and `All`,
# the boot label of the two "all" options in the log's filter bar (#101). Those
# two carry `data-i18n="log.filter_all"` and are replaced by the catalog at
# boot; the word is the fallback for the moment before the script runs.
STRUCTURAL = re.compile(
    r"^(RIPPLE|\U0001F1E9\U0001F1EA Deutsch|\U0001F1EC\U0001F1E7 English|\U0001F1F9\U0001F1F7 Türkçe|\U0001F1EB\U0001F1F7 Français|\U0001F1EE\U0001F1F9 Italiano|\U0001F1EA\U0001F1F8 Español|\U0001F1F5\U0001F1F9 Português|\U0001F1F7\U0001F1FA Русский|\U0001F1E8\U0001F1F3 中文|\U0001F1F5\U0001F1F1 Polski|\U0001F1ED\U0001F1FA Magyar|\U0001F1E8\U0001F1FF Čeština|\U0001F1F8\U0001F1F0 Slovenčina|\U0001F1F8\U0001F1EE Slovenščina|\U0001F1ED\U0001F1F7 Hrvatski|\U0001F1EC\U0001F1F7 Ελληνικά|All|—|\+|"
    r"/healthz|/stats|/config|FA-22 ·|\d+(\.\d+)?)$"
)


def test_the_shell_has_no_prose_of_its_own(page):
    """Every word in the shell is either a name, a number or an attribute. This
    is what makes one HTML serve both languages — and it is a stronger rule
    than "no German in the shell", which an English label would pass."""
    reader = _Text()
    reader.feed(page)
    stray = [(tag, text) for tag, text in reader.found
             if not STRUCTURAL.match(text)]
    assert stray == []


def test_the_selector_sits_in_the_masthead_with_flag_and_own_name(page, css):
    """Top right, flag + the language's own name; the flag is the emoji the
    system font provides, because FA-22 forbids an icon set."""
    head = page[:page.index("</header>")]
    assert 'id="langSel"' in head
    assert '<option value="de"' in head and '<option value="en"' in head
    # 🇩🇪 and 🇬🇧 — regional indicator pairs, no image, no icon font
    assert "\U0001F1E9\U0001F1EA Deutsch" in head
    assert "\U0001F1EC\U0001F1E7 English" in head
    assert ".lang" in css and ".lang select" in css


# -- the behaviour, executed ------------------------------------------------
HARNESS = r"""
/* Runs `dashboard.js` under a DOM stub with a given browser language and a given
 * `localStorage`, and prints what it rendered. Called by tests/test_i18n.py.
 *
 * Why a second harness next to `dashboard_dom_stub.js`: that one measures the
 * layout, this one measures the language — it has to be able to set
 * `navigator.languages` and `localStorage['ripple_lang']`, and to read back what
 * `fillShell()` wrote into the shell's `data-i18n` elements.
 *
 * Usage: node <this file> <dashboard.js> <i18n-dir> <json>
 *   json: {"languages": ["en-GB"], "stored": "de"|null, "shell": true}
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
  el.removeAttribute = k => { delete el.attributes[k]; };
  el.addEventListener = () => {};
  el.classList = {add() {}, remove() {}};
  return el;
}

/* The shell's nodes, so `fillShell()` has something to fill. */
const shell = {};
function shellNode(attr, id) {
  const el = makeEl('span');
  el.attributes[attr] = id;
  (shell[attr] = shell[attr] || []).push(el);
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
  querySelectorAll(sel) {
    const m = /^\[([a-z0-9-]+)\]$/.exec(sel);
    return m && shell[m[1]] ? shell[m[1]] : [];
  },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};

const store = new Map();
if (opts.stored) store.set('ripple_lang', opts.stored);
const localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};

const warnings = [];
const consoleStub = {
  warn: (...a) => warnings.push(a.join(' ')),
  log: () => {}, error: (...a) => warnings.push(a.join(' ')),
};

/* FA-22: the page may read `/static/` and nothing else. Anything else is an
 * error here, so an accidental CDN would fail the test, not the operator. */
function fetchStub(url) {
  if (!String(url).startsWith('/static/i18n/')) {
    return Promise.reject(new Error('unexpected fetch: ' + url));
  }
  const file = path.join(i18nDir, String(url).replace(/^\/static\/i18n\//, ''));
  if (!fs.existsSync(file)) return Promise.reject(new Error('404 ' + url));
  const body = JSON.parse(fs.readFileSync(file, 'utf8'));
  return Promise.resolve({ok: true, status: 200, json: async () => body,
                          text: async () => JSON.stringify(body)});
}

class FrozenDate extends Date {
  constructor(...args) { if (args.length) { super(...args); } else { super(1763000000000); } }
  static now() { return 1763000000000; }
}

const sandbox = {
  Date: FrozenDate,
  document,
  localStorage,
  /* `languages` is what a modern browser sends; `language` alone is the old
   * shape and the one the issue's acceptance line names. */
  navigator: {languages: opts.languages || [],
              language: opts.language || (opts.languages || [])[0]},
  window: {confirm: () => false, addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => {},
  console: consoleStub,
};
sandbox.globalThis = sandbox;

if (opts.shell) {
  shellNode('data-i18n', 'sec.fleet');
  shellNode('data-i18n', 'app.snapshot');
  shellNode('data-i18n-html', 'app.snap_src');
  shellNode('data-i18n-aria', 'lang.label');
  shellNode('data-i18n', 'state.loading');       /* a state sentence, not a shell id */
}

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;

  const out = {
    lang: api.lang,
    stored: localStorage.getItem('ripple_lang'),
    warnings: warnings.slice(),
    t_fleet: api.t('sec.fleet'),
    t_state: api.t('state.loading'),
    t_param: api.t('fleet.estimated', {v: '453,6'}),
    t_missing: api.t('no.such.message'),
    nf: [api.nf(1234.5, 1), api.nf(1234567.25, 2), api.nf(0.5, 1)],
    clock: api.clock(new Date(1763000000000)),
    html_lang: document.documentElement.getAttribute('lang'),
    shell: {
      sec_fleet: shell['data-i18n'][0].textContent,
      app_snapshot: shell['data-i18n'][1].textContent,
      app_snap_src: shell['data-i18n-html'][0].innerHTML,
      lang_aria: shell['data-i18n-aria'][0].getAttribute('aria-label'),
      state_sentence: shell['data-i18n'][2].textContent,
    },
    warnings_after_missing: warnings.length,
  };

  /* The same miss twice must not log twice. */
  api.t('no.such.message');
  out.warnings_after_second_miss = warnings.length;

  /* Switching in the GUI: the choice is stored and the whole page follows —
     including the shell's `data-i18n` nodes, which are not part of `render()`. */
  const other = api.lang === 'de' ? 'en' : 'de';
  api.chooseLang(other);
  out.after_switch = {lang: api.lang, stored: localStorage.getItem('ripple_lang'),
                      t_fleet: api.t('sec.fleet'), nf: api.nf(1234.5, 1),
                      clock: api.clock(new Date(1763000000000)),
                      html_lang: document.documentElement.getAttribute('lang'),
                      shell_sec_fleet: shell['data-i18n'][0].textContent};

  /* A key the German catalog lacks: English is the fallback, once. */
  api.chooseLang('de');
  delete api.catalogs.de['sec.fleet'];
  out.fallback = api.t('sec.fleet');
  out.warnings_after_fallback = warnings.length;

  /* An unsupported code is not a language, it is English. */
  /* `ja` steht hier, weil fr und it seit dem 16.11. echte Kataloge sind und
     als Gegenbeispiel nicht mehr taugen. */
  api.chooseLang('ja');
  out.after_unsupported = {lang: api.lang, stored: localStorage.getItem('ripple_lang')};
  out.warnings_all = warnings.slice();

  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


def _run(tmp_path, *, languages=(), stored=None, shell=True, language=None) -> dict:
    """The real `dashboard.js` in a stub browser with the given language."""
    if NODE is None:
        pytest.skip("node is not installed — the language measurement needs it")
    harness = tmp_path / "i18n_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    opts = json.dumps({"languages": list(languages), "stored": stored,
                       "shell": shell, "language": language})
    proc = subprocess.run([NODE, str(harness), str(STATIC / "dashboard.js"),
                           str(I18N), opts],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def german(tmp_path_factory) -> dict:
    return _run(tmp_path_factory.mktemp("i18n-de"), languages=["de-AT"])


@pytest.fixture(scope="module")
def english(tmp_path_factory) -> dict:
    return _run(tmp_path_factory.mktemp("i18n-en"), languages=["en-GB"])


def test_first_start_follows_the_browser_language(german, english):
    """`de-AT` → German, `en-GB` → English; no stored choice in either run."""
    assert german["lang"] == "de" and german["stored"] is None
    assert english["lang"] == "en" and english["stored"] is None
    assert german["t_fleet"] == "Flotte"
    assert english["t_fleet"] == "Fleet"


def test_an_unsupported_language_falls_back_to_english(tmp_path):
    """`ja-JP` is not supported tonight — the page is English, not empty."""
    run = _run(tmp_path, languages=["ja-JP", "ko-KR"])
    assert run["lang"] == "en"
    assert run["t_fleet"] == "Fleet"


def test_navigator_language_alone_is_enough(tmp_path):
    """A browser that only sends `navigator.language` (no list) must still land
    in the right language — that is the shape the issue's acceptance names."""
    assert _run(tmp_path, language="de-AT")["lang"] == "de"
    assert _run(tmp_path, language="en-GB")["lang"] == "en"
    assert _run(tmp_path, language="ja-JP")["lang"] == "en"


def test_the_first_supported_entry_of_navigator_languages_wins(tmp_path):
    """A browser that prefers an unsupported language and accepts English next
    gets English, not the fallback because of the first entry."""
    run = _run(tmp_path, languages=["ja-JP", "de-CH", "en-US"])
    assert run["lang"] == "de"


def test_the_gui_choice_survives_a_reload(tmp_path):
    """`localStorage['ripple_lang']` beats the browser language — that is what
    "persistent until changed again" means, measured by starting over."""
    run = _run(tmp_path, languages=["en-GB"], stored="de")
    assert run["lang"] == "de"
    assert run["t_fleet"] == "Flotte"
    run = _run(tmp_path, languages=["de-AT"], stored="en")
    assert run["lang"] == "en"
    assert run["t_fleet"] == "Fleet"


def test_the_selector_switches_the_whole_page_and_stores_the_choice(german):
    """One call, no reload: the language, the numbers, the shell's nodes and the
    `<html lang>` all follow, and the choice is in localStorage."""
    after = german["after_switch"]
    assert after["lang"] == "en"
    assert after["stored"] == "en"
    assert after["t_fleet"] == "Fleet"
    assert after["shell_sec_fleet"] == "Fleet"        # not part of render()
    assert after["html_lang"] == "en"
    assert after["nf"] == "1,234.5"


def test_switching_back_works_the_same_way(english):
    after = english["after_switch"]
    assert after["lang"] == "de" and after["stored"] == "de"
    assert after["t_fleet"] == "Flotte"
    assert after["nf"] == "1.234,5"


def test_an_unsupported_choice_is_english_and_stored_as_such(english):
    """Defensive: the selector only offers supported languages, but a hand-edited
    `localStorage` value must not leave the page in no language at all."""
    assert english["after_unsupported"] == {"lang": "en", "stored": "en"}


# -- numbers and dates ------------------------------------------------------
def test_the_same_payload_reads_german_and_english(german, english):
    """The acceptance figure: `1.234,5` in German, `1,234.5` in English — one
    payload, two languages, no hardcoded locale left."""
    assert german["nf"] == ["1.234,5", "1.234.567,25", "0,5"]
    assert english["nf"] == ["1,234.5", "1,234,567.25", "0.5"]


def test_a_placeholder_carries_the_number_in_the_language(german, english):
    assert german["t_param"] == "geschätzt 453,6 GB"
    assert english["t_param"] == "estimated 453,6 GB"   # the value is the caller's


def test_the_snapshot_time_follows_the_language(german, english):
    """`Intl.DateTimeFormat`, not a fixed format: the German clock is 24-hour,
    the English one carries AM/PM — the same instant, two renderings."""
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", german["clock"])
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}\s?(AM|PM)", english["clock"])
    assert german["clock"] != english["clock"]
    assert german["clock"].startswith("03:13:20")       # the frozen instant


# -- fallback ---------------------------------------------------------------
def test_a_missing_id_falls_back_to_english_and_is_logged_once(german):
    """Never an empty string: an id nobody translated shows the English text and
    says so — once, not on every tick."""
    assert german["fallback"] == "Fleet"
    assert german["warnings_after_fallback"] == german["warnings_after_missing"] + 1
    assert any("sec.fleet" in w and "English" in w for w in german["warnings_all"])


def test_an_unknown_id_is_the_id_and_never_an_empty_string(german):
    assert german["t_missing"] == "no.such.message"
    assert german["warnings_after_missing"] == 1
    assert german["warnings_after_second_miss"] == 1     # logged once


# -- the shell is filled ----------------------------------------------------
def test_the_shell_is_filled_from_the_catalog(german, de):
    """The three attributes, filled at boot: text, HTML and aria-label."""
    shell = german["shell"]
    assert shell["sec_fleet"] == de["sec.fleet"]
    assert shell["app_snapshot"] == de["app.snapshot"]
    assert "<code>/healthz</code>" in shell["app_snap_src"]
    assert shell["lang_aria"] == de["lang.label"]


# -- nothing German left in the code ---------------------------------------
def _strip_comments(source: str, html: bool = False) -> str:
    """Comments are German by convention (and the CSS too); code is not."""
    if html:
        source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r"#[^\n]*", "", source)
    return source


def test_no_german_label_is_hardcoded_in_the_script(js):
    """The known-words check: a label typed into `dashboard.js` would be
    invisible to a translator and would win over the catalog."""
    code = _strip_comments(js)
    hits = [w for w in GERMAN if w in code]
    assert hits == []


def test_no_german_label_is_hardcoded_in_the_shell(page):
    """Same rule for `dashboard.py` — including its docstring, which is English
    and names the sections by id rather than by their German titles."""
    code = _strip_comments(page, html=True)
    hits = [w for w in GERMAN if w in code]
    assert hits == []


# -- FA-22: nothing from the network ---------------------------------------
def test_the_page_fetches_nothing_but_its_own_static_files(js, css):
    """The catalogs ship with the page: the only fetch targets are the three
    endpoints and `/static/i18n/`. No CDN bundle, no XHR, no websocket."""
    assert "XMLHttpRequest" not in js
    assert "new WebSocket" not in js and "EventSource" not in js
    for target in re.findall(r"fetch\('([^']+)'", js):
        assert target in ("/healthz", "/stats", "/config", "/static/i18n/"), target
    assert "fetch('/static/i18n/' + l.code + '.json')" in js
    assert "http://" not in js and "https://" not in js
    assert "http://" not in css and "https://" not in css
