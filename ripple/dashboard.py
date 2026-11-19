"""Read-only dashboard (FA-22) — the shell.

Served at `/`, no build step, no CDN, no framework — it has to open inside the
LAN with the internet cable pulled. The page is three files plus one message
catalog, and each has one job:

- `ripple/dashboard.py` (this module) — the shell: masthead, language selector,
  section frames, the state legend's container, and the two `<link>`/`<script>`
  tags. No data, and **no visible text**: every label carries `data-i18n`
  (plain text) or `data-i18n-html` (the few paragraphs with `<b>`/`<code>`) and
  is filled from the catalog at boot (issue #17). One HTML, no per-language
  copies.
- `ripple/static/i18n/en.json` — the message catalog, source of truth; `de.json`
  is the German translation of the same key set.
- `ripple/static/dashboard.css` — the approved design's style sheet
  (`docs/dashboard-mockup.html`, explained in `docs/dashboard-design.md`).
- `ripple/static/dashboard.js` — the behaviour: fetch `/healthz`, `/stats` and
  `/config`, render, and patch the DOM every two seconds.

The layout is the one approved in `docs/dashboard-mockup.html`: fleet cards with
the memory bar, the seven KPIs, the model matrix, the distribution, the thread
registry and the decision log. Every heading carries a number and none of them
leaves a gap (issue #19, #25); the numbers are the reading order inside the
tabs, so each tab counts up and the tabs themselves run 1 → 2 → 3 → 4 → 5:
`1` fleet with the KPIs, `2` distribution, `2.1` load per host, `3` models,
`4` threads, `5` last interaction round, `6` operations with `6.1` maintenance
… `6.6` restart. They are deliberately **not** the numbers of the FA-22 list —
that list has models second and distribution third, which would count
backwards on the fleet tab. Which block answers which FA-22 item is a small
table in `docs/dashboard-design.md` §1, so the page is checked against the
requirement there instead of against a screenshot. The numbers are layout,
not text: they stay in this file, the words come from the catalog.

Since issue #56 the page is tabs instead of one 2 785 px column, and since #73
there are five of them — `Flotte`, `Modelle`, `Threads`, `Verlauf`, `Betrieb`:
the tab bar is `nav.tabs`, every block sits in exactly one
`section.tab-panel`, and the open tab is the URL hash (`#fleet`, `#models`,
`#threads`, `#history`, `#ops`). `dashboard.js` reads the hash, never a
remembered state, so a reload and a language switch both land on the same
tab. The numbers stay in the headings as the reading order inside a tab — the
tabs carry the names.

Issue #71 adds a sixth, `Knoten` (`#nodes`), and it is appended rather than
squeezed in: adding a node is a change to the fleet, not a maintenance action
on a running node, so it does not belong in `Betrieb` (which keeps `6.1` …
`6.6`), and appending leaves the reading order 1 → 6 and every reference to it
in `docs/dashboard-design.md` exactly as they were. The tab is `7`, with `7.1`
add by address, `7.2` the sweep and `7.3` removal. Its three text fields live
in this file, not in the 2 s patch: the patch replaces nodes, and a field that
is re-created every two seconds loses what was typed into it.

Deliberately not part of it: load/unload buttons, configuration changes,
anything writing. The balancer does not touch oMLX (NZ-4); this is a window,
not a control panel. Two exceptions, both operator-initiated and both behind
the admin key:

- the `Calibrate` button per node, amended in `docs/requirements.md` for
  `docs/loading-state.md` §6.1 — it loads the node's own models one after
  another, times them and stores the measured rate;
- the maintenance control per node (issue #10) — it sets or clears the
  maintenance window of `ripple/maintenance.py`, which is routing state, not a
  second control plane: no model is loaded, unloaded or patched.
- the model policy field per (model, node) row (issue #11) — `disabled` or
  `hidden` in `ripple/model_policy.py`, the same kind of routing state one level
  deeper: it decides whether RIPPLE dispatches to that node and whether
  `/v1/models` still offers the model. `hidden` is client-visible (Hermes and
  opencode lose the model from their list), which is why the page asks first.
  A rule for a model the node no longer offers has no row in the table (the
  table is fed by `/healthz`), so the `6.2` block lists those rules beside the
  table, read from `GET /admin/nodes/{name}/models` when the operator block is
  opened (issue #16) — otherwise the rule would be invisible and impossible to
  undo.
- the replication panel (issue #13) — the third and last exception, and the
  only one that writes to another machine's filesystem: it copies one model from
  one node to another (`docs/replication.md`), reloads the target and writes the
  source's per-model settings back. The panel shows the plan (size, free space,
  destination path), asks before it starts, and reports bytes, seconds, MB/s and
  anything that could not be replicated.
- the restart control (issue #25, the dashboard half of #22) — the only action
  that ends the process it runs in. It has its own block (`6.6`) because it is
  not per-node maintenance: `POST /admin/restart` ends the process, launchd
  starts it again, and the page waits for the service to answer and then shows
  the new `build.commit`. The state it needs (`supervised`, `reason`,
  `inflight`, `pending`) arrives in `/healthz → restart`, without a key and
  without a request of its own, so the control can be disabled with its reason
  before anyone unlocks the page.
- adding and removing a node (issue #71) — the first action that changes the
  fleet itself rather than something inside it, which is why it has a tab of its
  own (`7`) and not a block in `Betrieb`. `POST /admin/nodes?dry_run=true` reads
  the node and writes nothing, the operator confirms weight, site and role, and
  only then does the real `POST` run; `DELETE /admin/nodes/{name}` is #70's
  drain, shown as a dialog that names what is in flight and how long it waits.

The admin key follows the rule of `ripple/keys_page.py`: it lives in
`sessionStorage` under `ripple_admin` for the duration of the tab, is sent as
`Authorization: Bearer <admin-key>` and is never put in the URL and never rendered into
the page. `Calibrate` was the first consumer; the maintenance toggle is the
second, the model policy the third, the replication panel the fourth and the
restart the fifth, which is why all of them go through `adminApi()` — and the
node tab the sixth, where the same key unlocks the preview, the sweep and the
removal.
"""

from __future__ import annotations

SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title data-i18n="app.title"></title>
<link rel="stylesheet" href="/static/dashboard.css">
</head>
<body>
<div class="wrap">

  <header class="masthead">
    <div>
      <h1>RIPPLE</h1>
      <p class="tagline" data-i18n="app.tagline"></p>
    </div>
    <div class="snapshot">
      <!-- Sprachwahl (issue #17): Flagge als Emoji (das System-Font liefert sie,
           FA-22 verbietet ein Icon-Set) und der Eigenname der Sprache — der
           wird nicht übersetzt. Die Wahl liegt in localStorage['ripple_lang']. -->
      <div class="lang">
        <select id="langSel" data-i18n-aria="lang.label" autocomplete="off">
          <option value="de" lang="de">🇩🇪 Deutsch</option>
          <option value="en" lang="en">🇬🇧 English</option>
          <option value="tr" lang="tr">🇹🇷 Türkçe</option>
          <option value="fr" lang="fr">🇫🇷 Français</option>
          <option value="it" lang="it">🇮🇹 Italiano</option>
          <option value="es" lang="es">🇪🇸 Español</option>
          <option value="pt" lang="pt">🇵🇹 Português</option>
          <option value="ru" lang="ru">🇷🇺 Русский</option>
          <option value="zh" lang="zh">🇨🇳 中文</option>
          <option value="pl" lang="pl">🇵🇱 Polski</option>
          <option value="hu" lang="hu">🇭🇺 Magyar</option>
          <option value="cs" lang="cs">🇨🇿 Čeština</option>
          <option value="sk" lang="sk">🇸🇰 Slovenčina</option>
          <option value="sl" lang="sl">🇸🇮 Slovenščina</option>
          <option value="hr" lang="hr">🇭🇷 Hrvatski</option>
          <option value="el" lang="el">🇬🇷 Ελληνικά</option>
        </select>
      </div>
      <!-- Uhr und Beschriftung stehen nebeneinander in der Kopfzeile (#57). Die
           Herkunftszeile (`app.snap_src`) war mit 53 px der höchste Teil des
           Kopfs und stand unter der Uhr; sie ist jetzt deren Tooltip. Der Text
           bleibt dafür im DOM (`.snap-src`, unsichtbar) — er kommt aus dem
           Katalog und soll dort bleiben, wo er übersetzt wird. -->
      <div class="snap-when" id="snapWhen">
        <span class="snap-label" data-i18n="app.snapshot"></span>
        <span class="snap-time num" id="snapTime">—</span>
      </div>
      <div class="snap-src" data-i18n-html="app.snap_src"></div>
    </div>
  </header>

  <div id="banners" class="banners"></div>

  <!-- ── Tabs (issue #56, five since #73) ────────────────────────────────────
       Fünf Tabs statt 2 785 px Seite, die Namen kommen aus dem Katalog
       (`tab.fleet`, `tab.models`, `tab.threads`, `tab.history`, `tab.ops`),
       die Nummern bleiben als Lesereihenfolge in den Überschriften. Der
       Zustand ist der URL-Hash (`#fleet`, `#models`, `#threads`, `#history`,
       `#ops`) — er überlebt Reload und Sprachwechsel, und `dashboard.js`
       liest ihn, statt sich etwas zu merken.
       Die Leiste klebt am Seitenkopf (`position:sticky`), damit man beim
       Scrollen noch sieht, wo man ist; deshalb rückt `.log thead th` um
       `--tabbar-h` nach unten (siehe dashboard.css).
       Die Thread-Registry (`4`) hat seit #73 einen eigenen Tab: sie wächst mit
       der Zahl der Gespräche und hat die Übersichtsseite auseinandergezogen.
       `Auslastung` (`2.1`) bleibt bei der Übersicht: sie beantwortet dieselbe
       Frage wie `2` — was die Rechner gerade tun. -->
  <nav class="tabs" role="tablist">
    <button type="button" role="tab" id="tab-fleet" data-tab="fleet"
            aria-controls="panel-fleet" aria-selected="true"
            data-i18n="tab.fleet"></button>
    <button type="button" role="tab" id="tab-models" data-tab="models"
            aria-controls="panel-models" aria-selected="false" tabindex="-1"
            data-i18n="tab.models"></button>
    <button type="button" role="tab" id="tab-threads" data-tab="threads"
            aria-controls="panel-threads" aria-selected="false" tabindex="-1"
            data-i18n="tab.threads"></button>
    <button type="button" role="tab" id="tab-history" data-tab="history"
            aria-controls="panel-history" aria-selected="false" tabindex="-1"
            data-i18n="tab.history"></button>
    <button type="button" role="tab" id="tab-ops" data-tab="ops"
            aria-controls="panel-ops" aria-selected="false" tabindex="-1"
            data-i18n="tab.ops"></button>
    <button type="button" role="tab" id="tab-nodes" data-tab="nodes"
            aria-controls="panel-nodes" aria-selected="false" tabindex="-1"
            data-i18n="tab.nodes"></button>
  </nav>

  <section class="tab-panel" id="panel-fleet" role="tabpanel" data-tab="fleet"
           aria-labelledby="tab-fleet">
    <div class="legend" id="legend"></div>

    <section>
      <div class="block-head">
        <h2><span class="idx">1</span><span data-i18n="sec.fleet"></span></h2>
        <p class="hint" data-i18n="sec.fleet.hint"></p>
      </div>
      <div class="fleet" id="fleet"></div>
      <div class="kpis" id="kpis"></div>
    </section>

    <!-- 2 Verteilung (issue #73): ein Block **je Modell** — in dieser
         Installation einer für DeepSeek, einer für Qwen — nebeneinander und auf
         schmalen Fenstern untereinander. Der Modellname ist die Überschrift des
         Blocks; vorher stand jedes Modell unter dem vorigen im selben Block,
         und der Vergleich, um den es hier geht, lag zwei Bildschirmhälften
         auseinander. -->
    <section>
      <div class="block-head">
        <h2><span class="idx">2</span><span data-i18n="sec.dist"></span></h2>
        <p class="hint" data-i18n="sec.dist.hint"></p>
      </div>
      <div class="panel" id="dist"></div>
    </section>

    <!-- 2.1 Auslastung je Host (issue #33): was die Maschinen getan haben, seit
         RIPPLE läuft — Leerlauf, Laden, Prefill, Decode als exklusive
         Sekunden, daneben die Arbeit (Anfragen, Tokens) und darunter die
         eigene Zählung der Maschinen als Gegenprobe. Die Zahlen kommen aus
         /healthz → load; das Zurücksetzen ist die einzige Aktion und braucht
         den Admin-Schlüssel.
         Seit #73 steht sie in einer eigenen Zeile über die volle Breite: in
         der schmalen Spalte (≈ 1/3 der Seite) passten sieben Zahlenspalten
         nicht (#37), und als zweites Kind des alten Zweispalten-Rasters ließ
         sie unter der kürzeren Nachbarspalte eine Lücke — die „Zahnlücke". -->
    <section>
      <div class="block-head">
        <h2><span class="idx">2.1</span><span data-i18n="sec.load"></span></h2>
        <p class="hint" data-i18n="sec.load.hint"></p>
      </div>
      <div class="panel table-wrap" id="load"></div>
    </section>
  </section>

  <section class="tab-panel" id="panel-models" role="tabpanel" data-tab="models"
           aria-labelledby="tab-models" hidden>
    <section>
      <div class="block-head">
        <h2><span class="idx">3</span><span data-i18n="sec.models"></span></h2>
        <p class="hint" data-i18n="sec.models.hint"></p>
      </div>
      <div class="panel table-wrap" id="models"></div>
    </section>
  </section>

  <section class="tab-panel" id="panel-threads" role="tabpanel" data-tab="threads"
           aria-labelledby="tab-threads" hidden>
    <!-- 4 Thread-Registry (issue #73): ein eigener Tab, weil die Liste mit der
         Zahl der Gespräche wächst. Sie steht in der vollen Breite und ohne
         eigenes Scrollfenster — die Seite scrollt, wie im fünften Tab. -->
    <section>
      <div class="block-head">
        <h2><span class="idx">4</span><span data-i18n="sec.threads"></span></h2>
        <p class="hint" data-i18n="sec.threads.hint"></p>
      </div>
      <div class="panel" id="threads"></div>
    </section>
  </section>

  <section class="tab-panel" id="panel-history" role="tabpanel" data-tab="history"
           aria-labelledby="tab-history" hidden>
    <section>
      <div class="block-head">
        <h2><span class="idx">5</span><span data-i18n="sec.log"></span></h2>
        <p class="hint" data-i18n="sec.log.hint"></p>
      </div>
      <!-- The filter bar sits in the shell and therefore OUTSIDE `#log` (issue
           #101): `apply('log', …)` replaces the children of that container every
           two seconds, and a field that is rebuilt loses its text and its
           cursor. The filter state lives in `dashboard.js` (`logFilter`), not in
           the DOM. The two selects carry only the "all" option here; their
           values come from `fillLogFilters()`, because they change with the
           traffic. -->
      <div class="log-filters" id="logFilters">
        <label class="lf">
          <span class="lf-label" data-i18n="log.th_sid"></span>
          <input id="logFilterSid" type="text" autocomplete="off" spellcheck="false"
                 placeholder="sid:…" data-i18n-aria="log.th_sid">
        </label>
        <label class="lf">
          <span class="lf-label" data-i18n="log.th_reason"></span>
          <select id="logFilterReason" data-i18n-aria="log.th_reason">
            <option value="" data-i18n="log.filter_all">All</option>
          </select>
        </label>
        <label class="lf">
          <span class="lf-label" data-i18n="log.th_node"></span>
          <select id="logFilterHost" data-i18n-aria="log.th_node">
            <option value="" data-i18n="log.filter_all">All</option>
          </select>
        </label>
        <span class="hint lf-hint" data-i18n="log.filter_hint"></span>
      </div>
      <div class="panel log" id="log"></div>
    </section>
  </section>

  <section class="tab-panel" id="panel-ops" role="tabpanel" data-tab="ops"
           aria-labelledby="tab-ops" hidden>
  <details class="params" id="ops">
    <summary><span class="idx">6</span><span data-i18n="sec.ops"></span> <span class="hint" data-i18n="sec.ops.hint"></span></summary>

    <div class="ops-block">
      <div class="block-head">
        <h2><span class="idx">6.1</span><span data-i18n="sec.maint"></span></h2>
        <p class="hint" data-i18n="sec.maint.hint"></p>
      </div>
      <p class="hint" data-i18n-html="sec.maint.body"></p>
      <div id="maintPanel" class="calib-panel" data-i18n="panel.maint.idle"></div>
    </div>

    <div class="ops-block">
      <div class="block-head">
        <h2><span class="idx">6.2</span><span data-i18n="sec.policy"></span></h2>
        <p class="hint" data-i18n="sec.policy.hint"></p>
      </div>
      <p class="hint" data-i18n-html="sec.policy.body"></p>
      <p class="hint" data-i18n-html="sec.policy.body2"></p>
      <div id="policyPanel" class="calib-panel" data-i18n="panel.policy.idle"></div>
    </div>

    <div class="ops-block">
      <div class="block-head">
        <h2><span class="idx">6.3</span><span data-i18n="sec.calib"></span></h2>
        <p class="hint" data-i18n="sec.calib.hint"></p>
      </div>
      <p class="hint" data-i18n="sec.calib.body"></p>
      <div id="calibPanel" class="calib-panel" data-i18n="panel.calib.idle"></div>
    </div>

    <div class="ops-block">
      <div class="block-head">
        <h2><span class="idx">6.4</span><span data-i18n="sec.repl"></span></h2>
        <p class="hint" data-i18n="sec.repl.hint"></p>
      </div>
      <p class="hint" data-i18n-html="sec.repl.body"></p>
      <div id="replPanel" class="calib-panel" data-i18n="panel.repl.idle"></div>
    </div>

    <div class="ops-block">
      <div class="block-head">
        <h2><span class="idx">6.5</span><span data-i18n="sec.params"></span></h2>
        <p class="hint" data-i18n="sec.params.hint"></p>
      </div>
      <p class="hint" data-i18n-html="sec.params.body"></p>
      <div id="params"></div>
    </div>

    <div class="ops-block">
      <div class="block-head">
        <h2><span class="idx">6.6</span><span data-i18n="sec.restart"></span></h2>
        <p class="hint" data-i18n="sec.restart.hint"></p>
      </div>
      <p class="hint" data-i18n-html="sec.restart.body"></p>
      <div id="restartPanel" class="calib-panel" data-i18n="panel.restart.idle"></div>
    </div>
  </details>
  </section>

  <!-- ── 7 Rechner aufnehmen und entfernen (issue #71) ─────────────────────────
       Der sechste Tab, nicht ein Block im Wartungs-Tab (#71 sagt es
       ausdrücklich): einen Rechner aufzunehmen ist keine Wartung an einem
       laufenden Rechner, sondern die Änderung des Pools selbst. Angehängt
       statt eingeschoben, damit die Lesereihenfolge 1–6 und jeder Verweis
       darauf (docs/dashboard-design.md) unverändert gilt; die Leiste scrollt
       auf schmalen Fenstern waagerecht, ein sechstes Wort kostet keine Zeile.

       Die Eingabefelder stehen hier in der Shell und nicht im Patch: der
       2-s-Takt ersetzt Elemente, und ein Feld, das dabei neu entsteht, verliert
       Text und Cursor. Was der Tick anfasst, sind nur die vier Container
       darunter (`nodePreview`, `nodeFound`, `nodeDrain`, `nodeMsg`, `nodeList`)
       — und die auch nur, wenn sich ihr Inhalt geändert hat. -->
  <section class="tab-panel" id="panel-nodes" role="tabpanel" data-tab="nodes"
           aria-labelledby="tab-nodes" hidden>
    <section>
      <div class="block-head">
        <h2><span class="idx">7</span><span data-i18n="sec.nodes"></span></h2>
        <p class="hint" data-i18n="sec.nodes.hint"></p>
      </div>
      <p class="hint" data-i18n-html="sec.nodes.body"></p>

      <div class="ops-block">
        <div class="block-head">
          <h2><span class="idx">7.1</span><span data-i18n="sec.nodes.add"></span></h2>
          <p class="hint" data-i18n="sec.nodes.add.hint"></p>
        </div>
        <p class="hint" data-i18n-html="sec.nodes.add.body"></p>
        <form class="node-form" id="nodeForm" onsubmit="return false">
          <label class="node-field">
            <span data-i18n="nodes.address"></span>
            <input id="nodeAddress" type="text" autocomplete="off" spellcheck="false"
                   inputmode="url">
          </label>
          <button type="button" class="btn" id="nodePreviewBtn"
                  onclick="nodePreview()" data-i18n="nodes.preview"></button>
        </form>
        <div id="nodePreview" class="calib-panel" data-i18n="panel.nodes.idle"></div>
      </div>

      <div class="ops-block">
        <div class="block-head">
          <h2><span class="idx">7.2</span><span data-i18n="sec.nodes.scan"></span></h2>
          <p class="hint" data-i18n="sec.nodes.scan.hint"></p>
        </div>
        <p class="hint" data-i18n-html="sec.nodes.scan.body"></p>
        <form class="node-form" onsubmit="return false">
          <label class="node-field">
            <span data-i18n="nodes.cidr"></span>
            <input id="nodeCidr" type="text" autocomplete="off" spellcheck="false">
          </label>
          <label class="node-field">
            <span data-i18n="nodes.port"></span>
            <input id="nodePort" type="number" min="1" max="65535" step="1" value="8000">
          </label>
          <button type="button" class="btn" id="nodeScanBtn"
                  onclick="nodeScan()" data-i18n="nodes.scan"></button>
        </form>
        <div id="nodeFound" class="node-found"></div>
      </div>

      <div class="ops-block">
        <div class="block-head">
          <h2><span class="idx">7.3</span><span data-i18n="sec.nodes.remove"></span></h2>
          <p class="hint" data-i18n="sec.nodes.remove.hint"></p>
        </div>
        <p class="hint" data-i18n-html="sec.nodes.remove.body"></p>
        <div id="nodeList" class="calib-panel" data-i18n="panel.nodes.empty"></div>
      </div>

      <div id="nodeDrain" class="node-drain"></div>
      <div id="nodeMsg" class="node-msg"></div>
    </section>
  </section>

  <footer>
    <span>FA-22 · <code>/healthz</code> + <code>/stats</code> + <code>/config</code></span>
    <span data-i18n="foot.source"></span>
    <span data-i18n="foot.readonly"></span>
    <span id="conn" data-i18n="conn.connecting"></span>
  </footer>
</div>
<script src="/static/dashboard.js"></script>
</body>
</html>
"""


def render_dashboard() -> str:
    """The page shell. Data comes from `/healthz` and `/stats` in the browser.

    The visible text does not come from here: the shell carries message ids
    (`data-i18n`, `data-i18n-html`, `data-i18n-aria`) and `dashboard.js` fills
    them from `ripple/static/i18n/<lang>.json` at boot (issue #17).
    """
    return SHELL
