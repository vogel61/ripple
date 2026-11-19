"""`tools/mockup_snapshot.py` — the assembly, checked without a browser.

The tool has two halves. The second one — Chrome, CDP, a running instance — can
only be exercised on a machine with a browser and a fleet; that is what the tool
itself is for, and it verifies its own output at the end. The first half is the
part that decides what the committed file says, and it is pure text:

- the six panels are stacked in reading order, each under its tab number and
  name, with a contents list that points at them;
- no JavaScript survives: no `<script>`, no inline `on*=` handler;
- the wordmark is embedded exactly once, as a data URI, and nothing else in the
  style sheet is rewritten;
- the head names the commit and the one command that regenerates the file.

The last group checks the committed `docs/dashboard-mockup.html` against the same
rules, so a hand-edited snapshot (or a regenerated one with a broken frame) fails
here instead of in a browser.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import mockup_snapshot as ms  # noqa: E402

COMMAND = ("./.venv/bin/python tools/mockup_snapshot.py --url http://127.0.0.1:8899"
           " --out docs/dashboard-mockup.html")
COMMIT = "099c1de"

PANELS = {
    slug: ('<section class="tab-panel" id="panel-%s" role="tabpanel" data-tab="%s">\n'
           '  <!-- a shell comment that must not survive -->\n'
           '  <div class="block-head"><h2><span class="idx">1</span>%s</h2></div>\n'
           '  <button type="button" class="btn" onclick="doThing(1)">go</button>\n'
           '  <span class="host-src" data-src="config" data-tip="from config.yaml">'
           'config</span>\n'
           '</section>' % (slug, slug, name))
    for slug, name, _ in ms.TABS
}

MASTHEAD = ('<header class="masthead"><h1>RIPPLE</h1><select id="langSel">'
            '<option value="de" lang="de">🇩🇪 Deutsch</option>'
            '<option value="en" lang="en">🇬🇧 English</option></select></header>')
TABS_BAR = '<nav class="tabs" role="tablist"><button role="tab">Fleet</button></nav>'
FOOTER = '<footer><span>FA-22</span></footer>'
CSS = ('/* wordmark */\n@font-face{font-family:\'Rubik Microbe\';'
       'src:url(fonts/rubik-microbe-wordmark.woff2) format(\'woff2\')}\n'
       '.idx{color:var(--maint)}')
DATA_URI = "data:font/woff2;base64,AAAA"


def build(**overrides) -> str:
    kwargs = dict(panels=PANELS, masthead=MASTHEAD, tabs=TABS_BAR, footer=FOOTER,
                  css=ms.embed_font(CSS, DATA_URI), commit=COMMIT, command=COMMAND,
                  width=1440, height=900, fleet_note="node-a (draining), node-b (disabled)")
    kwargs.update(overrides)
    return ms.build_document(**kwargs)


# ── the frame ────────────────────────────────────────────────────────────────

def test_the_six_tabs_are_stacked_in_reading_order():
    html = build()
    ids = re.findall(r'<section class="snap-tab" id="tab-(\d)"', html)
    assert ids == ["1", "2", "3", "4", "5", "6"]
    # every panel is present exactly once, in the order of the tab bar
    for slug, _, _ in ms.TABS:
        assert html.count('id="panel-%s"' % slug) == 1
    positions = [html.index('id="panel-%s"' % slug) for slug, _, _ in ms.TABS]
    assert positions == sorted(positions)


def test_every_tab_carries_its_number_and_name_and_the_contents_list_links_to_it():
    html = build()
    for i, (_, name, blocks) in enumerate(ms.TABS, start=1):
        assert '<span class="kicker">Tab %d of 6</span>' % i in html
        assert '<a href="#tab-%d">' % i in html
        assert blocks in html
    assert '<h2>Operations</h2>' in html


def test_a_missing_panel_is_refused():
    partial = dict(PANELS)
    partial.pop("nodes")
    with pytest.raises(ValueError, match="nodes"):
        build(panels=partial)


# ── no JavaScript ────────────────────────────────────────────────────────────

def test_no_script_and_no_inline_handler_survives():
    html = build()
    assert "<script" not in html.lower()
    assert not re.search(r"\son[a-z]+=", html)
    assert "javascript:" not in html.lower()
    # …while the data attributes the page explains itself with are untouched
    assert 'data-src="config"' in html
    assert 'data-tip="from config.yaml"' in html


def test_the_shell_comments_are_dropped_but_the_head_comment_stays():
    html = build()
    assert "a shell comment that must not survive" not in html
    assert html.startswith("<!DOCTYPE html>\n<!-- docs/dashboard-mockup.html")


def test_the_language_chooser_shows_english():
    html = build()
    assert '<option value="en" lang="en" selected>' in html


# ── the style sheet ──────────────────────────────────────────────────────────

def test_the_wordmark_is_embedded_exactly_once():
    html = build()
    assert html.count("url(%s)" % DATA_URI) == 1
    # the font file is mentioned in prose (where it lives), never as a reference
    assert "url(fonts/rubik-microbe-wordmark.woff2)" not in html


def test_embedding_the_font_twice_is_harmless_and_an_unknown_reference_raises():
    once = ms.embed_font(CSS, DATA_URI)
    assert ms.embed_font(once, DATA_URI) == once
    with pytest.raises(SystemExit):
        ms.embed_font("@font-face{src:url(wordmark.woff2)}", DATA_URI)


def test_the_page_style_sheet_is_carried_unchanged_apart_from_the_font():
    html = build()
    assert ".idx{color:var(--maint)}" in html
    assert "/* wordmark */" in html


# ── the head ─────────────────────────────────────────────────────────────────

def test_the_head_names_the_commit_and_the_one_command():
    html = build()
    head = html[:html.index("<html")]
    assert "Generated against commit %s" % COMMIT in head
    assert COMMAND in head
    assert "do not edit" in head.lower()
    assert "snapshot of the built dashboard, not a design" in head.lower()
    assert "Regenerate with" in head


def test_a_dirty_tree_is_named_as_such():
    assert "dirty tree" not in build()
    assert "dirty tree" in build(dirty=True)


def test_the_instance_that_was_captured_is_named():
    assert "node-a (draining), node-b (disabled)" in build()


# ── the committed file ───────────────────────────────────────────────────────

ARTIFACT = ROOT / "docs" / "dashboard-mockup.html"


def test_the_committed_snapshot_is_self_contained():
    html = ARTIFACT.read_text()
    body = re.sub(r"<!--.*?-->", "", html, flags=re.S)  # prose may name a path
    assert "<script" not in body.lower()
    assert not re.search(r"\son[a-z]+=", body)
    assert body.count("url(data:font/woff2;base64,") == 1
    assert body.count('<section class="snap-tab"') == len(ms.TABS)
    assert '<html lang="en">' in body
    assert "Generated against commit" in html
    assert "Regenerate with" in html
    # No resource reference at all: every `src`/`href`/`url()` is a data URI or
    # absent. (Plain `http://` inside the rendered text — the nodes' addresses —
    # is content, not a fetch.)
    assert not re.search(r'(?:src|href)="(?:https?:)?//', body)
    # `url()` written in a comment is prose; a `url(something)` is a fetch.
    assert not re.search(r"url\((?!data:)(?!\))", body)
    assert "@import" not in body
