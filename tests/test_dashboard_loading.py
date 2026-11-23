"""The dashboard page for wave W2b (docs/loading-state.md §6.1).

String-level checks on the rendered page: the page is hand-written HTML/CSS/JS
with no build step, so the honest unit is "does the page contain the behaviour".
Since issue #2 the behaviour lives in `ripple/static/dashboard.js` and the shell
only links it — `page()` therefore concatenates the three files. No browser, no
server, no network.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from ripple.dashboard import render_dashboard

STATIC = pathlib.Path(__file__).resolve().parent.parent / "ripple" / "static"


@pytest.fixture(scope="module")
def page() -> str:
    """The shell plus its two assets — what the browser ends up executing."""
    return (render_dashboard()
            + (STATIC / "dashboard.css").read_text(encoding="utf-8")
            + (STATIC / "dashboard.js").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def de() -> dict:
    """The German catalog — since issue #17 the page's words live there, not in
    the JavaScript, so the sentences are asserted where they are written."""
    return json.loads((STATIC / "i18n" / "de.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def en() -> dict:
    return json.loads((STATIC / "i18n" / "en.json").read_text(encoding="utf-8"))


# -- calibrate button -------------------------------------------------------
def test_page_has_a_calibrate_button(page, de):
    assert "t('fleet.calibrate')" in page
    assert de["fleet.calibrate"] == "Kalibrieren"


def test_calibrate_calls_the_calibration_endpoints(page):
    # The endpoint is actually called, not just mentioned in a comment.
    assert "'/admin/nodes/'" in page
    assert "'/calibrate'" in page
    assert "'/calibration'" in page


def test_calibrate_posts_and_polls(page):
    assert "adminApi('POST', base + '/calibrate')" in page
    assert "adminApi('GET', base + '/calibration')" in page


def test_calibrate_asks_before_loading_anything(page, de):
    # window.confirm, and the dialog names the node and the consequence.
    assert "window.confirm(" in page
    assert "t('calib.confirm', {node: name" in page
    assert de["calib.confirm"].startswith("„Kalibrieren“ für {node}?")
    assert "may evict" in de["calib.confirm"].replace("kann andere verdrängen", "may evict")


def test_calibrate_is_disabled_while_the_node_holds_a_thread(page, de):
    assert "t('fleet.cal_held')" in page
    # the sentence grew the force hint in #128, so the test pins the reason and
    # not the wording: a translated string is not a contract, its first clause is
    assert de["fleet.cal_held"].startswith("der Knoten hält einen aktiven Thread")
    assert "force" in de["fleet.cal_held"]
    assert "x.thread_slots" in page
    assert "disabled" in page


def test_calibrate_cannot_fire_twice(page):
    # A second click while a job runs must be impossible: early return plus a
    # disabled button that survives the 2 s re-render. #128 added the model lock
    # as a third reason, so the guard is a list and not a pair.
    assert "if (calibBusy[name]) return;" in page
    assert "const calibBusy = {}" in page
    assert re.search(r"busy \|\| held( \|\| locked\.length)? \? ' disabled'", page)


def test_calibration_panel_shows_models_and_evictions(page, de):
    assert "post.models" in page
    assert "post.evicts" in page
    assert "t('calib.may_evict'" in page
    assert de["calib.may_evict"].startswith("darf verdrängen:")


def test_calibration_panel_shows_job_state_and_step(page, de):
    assert "t('calib.job_state', {state: job.state, step: job.step || '—'})" in page
    assert de["calib.job_state"] == "job {state} · step {step}"


def test_calibration_panel_shows_samples_and_source(page, de):
    assert "samples.map(s =>" in page
    assert "s.size_gb" in page
    assert "s.seconds" in page
    assert "t('calib.source', {v: (d && d.source) || '—'})" in page
    assert de["calib.source"] == "Quelle {v}"


def test_calibration_panel_shows_the_intercept_unrounded(page, de):
    # The negative intercept is the interesting number — it stays as measured.
    assert "intercept: num(f.intercept)" in page
    assert "{intercept} s" in de["calib.fit"]
    assert "f.intercept < 0" in page


def test_calibration_reports_a_409_with_the_json_message(page, de):
    assert "e.status === 409" in page
    assert "t('calib.busy_409', {msg: e.message})" in page
    assert de["calib.busy_409"] == "409 — Knoten beschäftigt: {msg}"


# -- admin key --------------------------------------------------------------
def test_page_reads_the_admin_key_from_session_storage(page):
    assert "sessionStorage.getItem('ripple_admin')" in page


def test_admin_key_is_sent_as_a_bearer_header(page):
    assert "'Authorization': 'Bearer ' + key" in page


def test_dashboard_never_renders_the_admin_key(page):
    # Bis #130 galt hier „die Seite speichert den Schlüssel nie". Seit dem
    # Masthead-Feld speichert sie ihn — aber genau einmal und erst hinter einer
    # bestandenen Prüfung; diese Reihenfolge nagelt
    # `test_dashboard.py::test_the_key_is_written_in_one_place_and_only_after_the_check`
    # fest, und `test_a_rejected_key_is_not_stored_and_the_field_says_so` den 401.
    # Was hier bleibt und weiter gilt: der Schlüssel kommt nicht ins Markup und
    # nicht in eine URL.
    assert "sessionStorage.setItem('ripple_admin'" not in page   # nur über ADMIN_STORE
    assert "Authorization: Bearer ' + adminKey()" not in page
    assert "?key=" not in page and "&key=" not in page


def test_missing_key_explains_where_to_set_it(page, de):
    assert "t('key.help')" in page
    assert "auf der Schlüsselseite (/keys)" in de["key.help"]


# -- loading state and ETA --------------------------------------------------
def test_loading_line_reads_the_healthz_loading_field(page):
    assert "x.loading" in page
    assert "L.eta_seconds" in page
    assert "L.size_gb" in page


def test_loading_line_shows_model_size_and_eta(page, de):
    assert "t('fleet.loading', {model: esc(L.model)})" in page
    assert de["fleet.loading"] == "lädt <b>{model}</b>"
    # Issue #36: `size_gb` is already GB — `gb()` would divide by 1e9 again.
    assert "${nf(L.size_gb, 1)} GB" in page
    # Issue #34: the fixed ETA became a deadline the page counts down itself.
    assert "data-deadline-kind=\"load\"" in page
    assert "deadlineText('load', at - pageNow())" in page


def test_the_five_states_are_rendered_as_one_chip_each(page, de):
    # Since #2 the state is a chip with a glyph per state (design §3), not a
    # coloured word: the legend names all five, the card uses the same chip.
    for state in ("healthy", "loading", "degraded", "maintenance", "down"):
        assert "'%s'" % state in page, state
        assert "state." + state in de
    assert 'data-state="' in page


def test_the_chip_prefers_the_new_state_name(page):
    # /healthz reports `state`; `status` stays for compatibility.
    assert "x.state || x.status" in page


def test_loading_and_degraded_stay_distinguishable(page, de):
    # `loading` is not a health verdict and `degraded` still takes work: both
    # have their own legend sentence, so neither reads as `down`.
    assert de["state.loading"] == "lädt ein Modell, nimmt Arbeit an"
    assert de["state.degraded"] == "antwortet, aber über Schwelle oder mit Fehlern"


# -- parameter table --------------------------------------------------------
def test_params_show_the_loading_block(page):
    assert "add('placement', 'loading'," in page
    assert "L.safety_factor" in page
    assert "L.max_seconds" in page
    assert "L.samples" in page
    assert "L.calibration_file" in page


def test_params_show_the_flap_guard(page):
    assert "add('placement', 'flap_guard_failures'," in page
    assert "p.flap_guard_failures" in page


# -- offline (FA-22) --------------------------------------------------------
def test_page_references_no_external_url(page):
    assert "http://" not in page
    assert "https://" not in page


def test_page_uses_no_eval(page):
    assert "eval(" not in page


# -- the existing page still works -----------------------------------------
def test_existing_sections_are_untouched(page):
    for anchor in ('id="fleet"', 'id="models"', 'id="dist"',
                   'id="log"', 'id="params"'):
        assert anchor in page


def test_existing_functions_keep_their_names(page):
    for fn in ('function render(', 'function renderParams(', 'async function tick('):
        assert fn in page
    assert "setInterval(tick, 2000)" in page

def test_the_legend_is_collapsed_until_someone_opens_it(tmp_path, en, de):
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    """Issue #181: the first screen shows the fleet, not the explanation of it.

    The legend keeps its place above the cards — collapsing it is what makes the
    cards the first thing an operator sees, without losing the legend for the one
    who wants it. The choice is remembered, or every reload undoes it.
    """
    assert "LEGEND_OPEN_KEY" in js and "localStorage" in js
    assert "ripple_legend_open" in js, "the choice needs a stable key"
    # collapsed is the default: the body carries `hidden` unless the store says 1
    assert "localStorage.getItem(LEGEND_OPEN_KEY) === '1'" in js
    assert "'</button><div class=\"legend-body\" id=\"legendBody\"' +" in js
    assert "(open ? '' : ' hidden')" in js
    # the button says what it does, in both directions, in the operator's language
    assert en["legend.show"] and en["legend.hide"]
    assert de["legend.show"] and de["legend.hide"]
    assert en["legend.show"] != en["legend.hide"]
    # …and the note stays inside the collapsible part, not beside it: it is the
    # promise about shapes that #70 broke once, so it must travel with the rest.
    start = js.index("function renderLegend()")
    body = js[start:js.index("\n}", start)]
    assert "legend-body" in body and "legend-note" in body, body
    assert body.index("legend-note") > body.index("legend-body"), body
