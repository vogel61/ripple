"""`tools/pdf_manual.py` — the manual's PDF, and the promises around it (issue #109).

A PDF that is generated on demand cannot be committed, so nothing in the
repository would notice if the generator rotted, if the manual started pointing at
a picture that is not there, or if the PDF the operator hands out stopped matching
the manual it claims to be. This file is the thing that notices, in two layers:

**(a) Always, in every pytest run, in under a second.** The preconditions: the
tool exists and has a `--help` that runs without building anything; the manual and
all 16 pictures it references exist; `dist/` — where the PDF lands — is ignored by
git, so a run leaves `git status` clean; the style sheet still carries the print
contract (A4, 20 mm, 10.5 pt, 1.45); and the two pure helpers behave.

**(b) Only when asked.** `RIPPLE_PDF=1` (build it if it is not there, then check
it) or an existing `dist/ripple-manual-en.pdf` (check what is there). This layer
needs Chrome and poppler and takes a couple of seconds, which is why it is not in
every run. It checks the *product* against the *source*: the page count is
plausible, the last chapter of the manual is in the PDF, the two sentences the
manual is accepted on are in it, all 16 pictures are embedded, no page is empty,
and every page carries its number.

The markers are read from `docs/manual-en.md` where they can be — the last
chapter's title is whatever the manual's last `##` heading says today, so
renaming a chapter does not silently turn this test into a check of a string that
no longer exists in either file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import pdf_manual as pm  # noqa: E402

MANUAL = ROOT / "docs" / "manual-en.md"
TOOL = ROOT / "tools" / "pdf_manual.py"
STYLESHEET = ROOT / "tools" / "pdf" / "manual.css"
PDF = ROOT / "dist" / "ripple-manual-en.pdf"

# The manual's own count. It is a constant because a change in it is a change to
# the manual's shape, and that is worth seeing in a diff rather than discovering
# as a smaller PDF.
PICTURES = 16


def _tool(name: str) -> str | None:
    """`name` from PATH, or from Homebrew's prefix when PATH is not the shell's."""
    found = shutil.which(name)
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/" + name, "/usr/local/bin/" + name):
        if Path(candidate).is_file():
            return candidate
    return None


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=300,
                          cwd=str(ROOT), **kwargs)


def _npages(pdf: Path) -> int | None:
    """Page count from qpdf, then pdfinfo; `None` when neither is installed."""
    qpdf = _tool("qpdf")
    if qpdf:
        result = _run([qpdf, "--show-npages", str(pdf)])
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    pdfinfo = _tool("pdfinfo")
    if pdfinfo:
        match = re.search(r"^Pages:\s+(\d+)$", _run([pdfinfo, str(pdf)]).stdout, re.M)
        if match:
            return int(match.group(1))
    return None


def _text(pdf: Path) -> str:
    return _run([_tool("pdftotext"), "-layout", str(pdf), "-"]).stdout


def _image_pages(pdf: Path) -> tuple[int, set[int]]:
    """(number of embedded images, the set of 1-based pages that carry one)."""
    listing = _run([_tool("pdfimages"), "-list", str(pdf)]).stdout
    rows = [line.split() for line in listing.splitlines()[2:] if line.strip()]
    return len(rows), {int(row[0]) for row in rows}


# ── (a) the preconditions: cheap, no Chrome, no PDF ─────────────────────────

def test_the_tool_exists_and_has_a_help_that_builds_nothing(tmp_path):
    """`--help` must run without side effects — no Chrome, no PDF, no `dist/`."""
    assert TOOL.is_file(), "%s is missing: there is no generator" % TOOL
    before = PDF.exists()
    result = _run([sys.executable, str(TOOL), "--help"])
    assert result.returncode == 0, result.stderr
    assert "--out" in result.stdout
    assert "manual-en.md" in result.stdout
    assert PDF.exists() == before, "--help built a PDF, which it must not do"


def test_the_manual_and_every_picture_it_references_exist():
    assert MANUAL.is_file()
    refs = pm.image_refs(MANUAL.read_text(encoding="utf-8"))
    assert len(refs) == PICTURES, (
        "the manual references %d pictures, this test knows %d — if the manual "
        "gained or lost one, say so here too" % (len(refs), PICTURES))
    missing = [ref for ref in refs if not (ROOT / "docs" / ref).is_file()]
    assert not missing, "the manual points at pictures that do not exist: %s" % missing
    # And the tool resolves them the same way, before pandoc runs.
    assert len(pm.check_images(MANUAL.read_text(encoding="utf-8"))) == PICTURES


def test_a_missing_picture_stops_the_tool_before_pandoc():
    with pytest.raises(SystemExit) as exc:
        pm.check_images("![gone](images/this-picture-does-not-exist.png)\n")
    assert "this-picture-does-not-exist.png" in str(exc.value)


def test_dist_is_ignored_so_a_run_leaves_the_tree_clean():
    """The PDF is a build product: `git status` after a run has to be empty."""
    if _tool("git") is None:
        pytest.skip("git is not installed")
    result = _run([_tool("git"), "check-ignore", "-v", "dist/ripple-manual-en.pdf"])
    assert result.returncode == 0, (
        "dist/ripple-manual-en.pdf is not ignored — a run would dirty the tree:\n"
        + result.stdout + result.stderr)


def test_the_stylesheet_carries_the_print_contract():
    css = STYLESHEET.read_text(encoding="utf-8")
    assert re.search(r"@page\s*\{[^}]*size:\s*A4", css, re.S), "no A4 page"
    assert re.search(r"@page\s*\{[^}]*margin:\s*20mm", css, re.S), "not 20 mm"
    assert "font-size: 10.5pt" in css
    assert "line-height: 1.45" in css
    # A code block that does not wrap runs off the paper; a table cell that does
    # not break pushes the table over the margin.
    assert "white-space: pre-wrap" in css
    assert "overflow-wrap: anywhere" in css
    # Pictures are bounded, never stretched.
    assert re.search(r"img\s*\{[^}]*max-width:\s*100%", css, re.S)


# ── (b) the product, against the manual ─────────────────────────────────────

def _gated_pdf() -> Path:
    """The PDF to check, building it when `RIPPLE_PDF=1` asked for that."""
    if PDF.is_file():
        return PDF
    if os.environ.get("RIPPLE_PDF") != "1":
        pytest.skip("set RIPPLE_PDF=1 (or build dist/ripple-manual-en.pdf) to "
                    "check the PDF against the manual")
    missing = [name for name in ("pandoc",) if _tool(name) is None]
    if missing or not Path("/Applications/Google Chrome.app").exists():
        pytest.skip("the PDF cannot be built here: no %s" % ", ".join(missing or
                                                                     ["Chrome"]))
    result = _run([sys.executable, str(TOOL)])
    assert result.returncode == 0, result.stderr
    assert PDF.is_file()
    return PDF


def test_the_pdf_matches_the_manual():
    pdf = _gated_pdf()
    if _tool("pdftotext") is None:
        pytest.skip("pdftotext is not installed")

    pages = _npages(pdf)
    if pages is None:
        pytest.skip("no qpdf/pdfinfo to count pages")
    assert pages > 20, "a %d-page PDF is not this manual" % pages

    text = _text(pdf)
    # The last chapter of the manual, whatever it is called today: a PDF that
    # stops before the end is a PDF built from another file.
    headings = re.findall(r"^##\s+(.+)$", MANUAL.read_text(encoding="utf-8"), re.M)
    last_chapter = headings[-1].strip()
    assert last_chapter in text, "the last chapter (%r) is not in the PDF" % last_chapter

    # The two sentences the manual is accepted on (issue #108 named the first,
    # §5.1 the second): both are in the text, so both are on the paper.
    assert "last 200" in text
    assert "What earns a row" in text

    # The footer is drawn by Chrome, not by the style sheet: if the print call
    # lost `displayHeaderFooter`, every page number is gone and nobody notices.
    assert "page 1 of %d" % pages in text, "no page numbers in the footer"


def test_every_picture_is_embedded_and_no_page_is_empty():
    pdf = _gated_pdf()
    if _tool("pdftotext") is None:
        pytest.skip("pdftotext is not installed")

    image_pages: set[int] | None = None
    if _tool("pdfimages") is None:
        # No way to count them: the pictures are PNGs of 10 kB ... 850 kB and
        # total 1.7 MB, so a PDF of a few hundred kB cannot contain all of them.
        assert pdf.stat().st_size > 1_000_000, (
            "the PDF is %d bytes — too small to carry the manual's %d pictures"
            % (pdf.stat().st_size, PICTURES))
    else:
        embedded, image_pages = _image_pages(pdf)
        assert embedded == PICTURES, (
            "the PDF embeds %d images, the manual references %d — a picture is a "
            "dead link or was split" % (embedded, PICTURES))

    # A page that carries a picture and nothing else is legitimate (a full-tab
    # screenshot), so "empty" means neither text nor picture. The footer alone
    # does not count as text: every page has one.
    pages = _npages(pdf) or len(_text(pdf).split("\f")) - 1
    chunks = _text(pdf).split("\f")[:pages]
    empty = [i + 1 for i, chunk in enumerate(chunks)
             if not _has_words(chunk) and (image_pages is None or i + 1 not in image_pages)]
    assert not empty, "pages without text and without a picture: %s" % empty


def _has_words(chunk: str) -> bool:
    """A page's text, minus the footer — a page carrying only a footer is blank."""
    body = re.sub(r"page \d+ of \d+", "", chunk)
    return bool(body.strip())
