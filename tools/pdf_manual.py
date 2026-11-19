#!/usr/bin/env python3
"""`docs/manual-en.md` to `dist/ripple-manual-en.pdf`, in one command (issue #109).

The manual is the document an operator prints and hands to a colleague; until now
there was no way to get it out of Markdown. This tool is that way, and it is the
only one: no hand step, no "open it in Chrome and press Print", no committed
binary. Whatever looks wrong in the PDF is a bug in this file or in
`tools/pdf/manual.css`.

What it does, in order:

1. **Read the manual and check its pictures.** Every `![…](images/x.png)` in
   `docs/manual-en.md` must exist under `docs/`. A missing file is a dead link in
   the PDF, and a dead link in a printed manual is a blank rectangle nobody can
   click — so the run stops before pandoc starts.
2. **Markdown → HTML, with the pictures inside the HTML.** `pandoc
   --embed-resources` inlines every image as a `data:` URI and the style sheet as
   a `<style>` block, so the file Chrome prints fetches nothing: no path can be
   wrong at print time, and "the image is embedded" is a property of the HTML, not
   a hope. The HTML is written into a scratch directory and deleted afterwards.
3. **Chrome headless → PDF.** Chrome starts on a kernel-assigned port
   (`--remote-debugging-port=0`) with its own `--user-data-dir` in the scratch
   directory, so it cannot collide with the operator's browser or with anything
   else on this machine, and it is terminated in a `finally` block. The PDF comes
   from `Page.printToPDF` over CDP rather than from `--print-to-pdf` on the
   command line, because the command line cannot draw a page footer: a manual
   without "page 7 of 41" is a manual whose pages end up out of order on the
   printer. CDP's `footerTemplate` can, and the CDP client already exists in
   `tools/mockup_snapshot.py` — imported, not copied.
4. **Write `dist/ripple-manual-en.pdf`** and report pages, bytes and seconds.

`dist/` is in `.gitignore` (`build artefacts`): the PDF is a build product, and a
committed binary would be stale the day after it was made. The tool is what is
versioned; `tests/test_pdf_manual.py` checks the product against the manual.

Usage:

    ./.venv/bin/python tools/pdf_manual.py
    ./.venv/bin/python tools/pdf_manual.py --out /tmp/manual.pdf --html /tmp/manual.html

Requirements: `pandoc` 3.x (for `--embed-resources`) and Google Chrome. Both are
system tools; neither is a Python dependency, and this file imports nothing that
is not in the standard library.
"""

from __future__ import annotations

import argparse
import base64
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
SOURCE = DOCS / "manual-en.md"
STYLESHEET = Path(__file__).resolve().parent / "pdf" / "manual.css"
DEFAULT_OUT = REPO / "dist" / "ripple-manual-en.pdf"

# The title the PDF carries in its metadata and in Chrome's own header. It is the
# manual's first line, repeated here because a PDF's document title is not read
# from the body.
TITLE = "RIPPLE — Operator's Manual"

# Where pandoc and Chrome usually are. `shutil.which` comes first in both lists;
# the explicit paths are for a machine whose PATH is not the login shell's (a
# launchd job, a CI runner, this operator's Anaconda install).
PANDOC_CANDIDATES = ["/opt/anaconda3/bin/pandoc", "/opt/homebrew/bin/pandoc",
                     "/usr/local/bin/pandoc"]
PAGE_COUNT_TOOLS = ["/opt/homebrew/bin/qpdf", "/usr/local/bin/qpdf"]

# The page footer, drawn by Chrome into the bottom margin. Font and colour have to
# be set inline: Chrome does not let a footer template inherit the page's style.
# `<span class="pageNumber">` and `<span class="totalPages">` are filled in by
# Chrome per page.
FOOTER = (
    '<div style="width:100%;margin:0 20mm;font-family:Helvetica,Arial,sans-serif;'
    'font-size:8pt;color:#666;display:flex;justify-content:space-between;">'
    '<span>RIPPLE — Operator\'s Manual</span>'
    '<span>page <span class="pageNumber"></span> of '
    '<span class="totalPages"></span></span>'
    '</div>'
)
# An empty header: Chrome reserves the top margin for one either way, and the
# manual's own first line is a better title than a repeated running head.
HEADER = '<div style="font-size:0;height:0;"></div>'


# ── the inputs ───────────────────────────────────────────────────────────────

def find_pandoc(explicit: str | None = None) -> str:
    """The pandoc binary, or a clear error — never a silent skip."""
    if explicit:
        return explicit
    found = shutil.which("pandoc")
    if found:
        return found
    for candidate in PANDOC_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("pandoc not found — install pandoc 3.x (brew install pandoc) "
                     "or pass --pandoc /path/to/pandoc")


def image_refs(markdown: str) -> list[str]:
    """Every image path the manual references, in order, as written.

    Markdown images only (`![alt](path)`); the manual has no raw `<img>`, and if
    one appeared it would be a picture pandoc embeds without this tool knowing —
    which the "all embedded" check in `check_images` would then catch.
    """
    return re.findall(r"!\[[^\]]*\]\(([^)\s]+)", markdown)


def check_images(markdown: str) -> list[Path]:
    """Resolve every image against `docs/` and fail if one is missing.

    Run *before* pandoc: pandoc's own behaviour with a missing image is a warning
    and a broken `<img>`, and a broken `<img>` prints as nothing at all.
    """
    refs = image_refs(markdown)
    if not refs:
        raise SystemExit("%s references no image at all — that is a wrong file, "
                         "not a manual without pictures" % SOURCE)
    missing = [ref for ref in refs if not (DOCS / ref).is_file()]
    if missing:
        raise SystemExit("%s references %d image(s) that do not exist under %s: %s"
                         % (SOURCE.name, len(missing), DOCS,
                            ", ".join(sorted(set(missing)))))
    return [DOCS / ref for ref in refs]


# ── markdown → one self-contained HTML file ──────────────────────────────────

def build_html(pandoc: str, html_out: Path, *, timeout: int = 120) -> str:
    """Run pandoc over the manual and return the HTML it wrote.

    `--embed-resources` needs `--standalone`, so the file is a whole document;
    `--metadata pagetitle` sets `<title>` without making pandoc emit a title block
    (the manual already starts with its own `# RIPPLE — Operator's Manual`, and
    the template would print the heading twice).
    """
    command = [
        pandoc, str(SOURCE),
        "--from", "markdown+smart",
        "--to", "html5",
        "--standalone",
        "--embed-resources",
        "--resource-path", str(DOCS),
        "--css", str(STYLESHEET),
        "--metadata", "pagetitle=%s" % TITLE,
        "--metadata", "lang=en",
        "--output", str(html_out),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise SystemExit("pandoc failed (%d):\n%s" % (result.returncode,
                                                      (result.stderr or "").strip()))
    html = html_out.read_text(encoding="utf-8")

    # The two promises `--embed-resources` is supposed to keep. Checked here
    # because a `src="images/…"` that survived would print as a hole, and a
    # `<link rel=stylesheet>` that survived would print the manual unstyled.
    dead = re.findall(r'src="(?!data:)[^"]{0,120}', html)
    if dead:
        raise SystemExit("pandoc left %d external image reference(s) in the HTML: %s"
                         % (len(dead), ", ".join(dead[:3])))
    if "<link" in html:
        raise SystemExit("pandoc left a <link> in the HTML — the style sheet was "
                         "not inlined")
    if "<style" not in html:
        raise SystemExit("%s was not inlined into the HTML" % STYLESHEET)
    return html


# ── Chrome → PDF ─────────────────────────────────────────────────────────────

def _cdp():
    """The CDP client and Chrome launcher from `tools/mockup_snapshot.py`.

    Imported rather than re-implemented: that file already starts Chrome on a
    kernel-assigned port with a throwaway profile and shuts it down again, and two
    copies of a WebSocket client would be two places to fix the same bug.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(REPO))
    try:  # as `python tools/pdf_manual.py`
        from tools import mockup_snapshot as ms
    except ImportError:  # pragma: no cover - only when tools/ is the script's dir
        import mockup_snapshot as ms
    return ms


def render_pdf(html_path: Path, pdf_out: Path, *, chrome: str | None = None,
               scratch: Path | None = None, timeout: float = 120.0) -> None:
    """Print `html_path` to `pdf_out` with headless Chrome, then stop Chrome.

    The page is printed only once every image has decoded (`naturalWidth > 0`):
    a data URI that has not finished decoding prints as an empty box, and that is
    the one failure this tool could produce that looks like a layout bug.
    """
    ms = _cdp()
    chrome_path = ms.find_chrome(chrome)
    profile = (scratch or Path(tempfile.mkdtemp(prefix="ripple-pdf-"))) / "chrome"
    proc, ws_url = ms.launch_chrome(chrome_path, profile)
    cdp = ms.CDP(ws_url)
    try:
        cdp.call("Page.enable")
        cdp.call("Page.navigate", url=html_path.resolve().as_uri())
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = cdp.eval("document.readyState")
            if state == "complete" and cdp.eval(
                    "Array.from(document.images).every(i => i.complete && "
                    "i.naturalWidth > 0)"):
                break
            time.sleep(0.1)
        else:
            raise SystemExit("Chrome did not finish loading %s within %.0f s"
                             % (html_path, timeout))
        cdp.eval("document.fonts.ready.then(() => true)")
        # The page's own paper: A4 and the 20 mm margins come from `@page` in
        # tools/pdf/manual.css, which is why `preferCSSPageSize` is on — one
        # source of truth for the page geometry, the style sheet.
        result = cdp.call("Page.printToPDF",
                          printBackground=True,
                          preferCSSPageSize=True,
                          displayHeaderFooter=True,
                          headerTemplate=HEADER,
                          footerTemplate=FOOTER,
                          marginTop=0.79, marginBottom=0.94,
                          marginLeft=0.79, marginRight=0.79)
    finally:
        cdp.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    data = base64.b64decode(result["data"])
    if not data.startswith(b"%PDF-"):
        raise SystemExit("Chrome returned %d bytes that are not a PDF" % len(data))
    pdf_out.parent.mkdir(parents=True, exist_ok=True)
    pdf_out.write_bytes(data)


# ── what came out ────────────────────────────────────────────────────────────

def page_count(pdf: Path) -> int | None:
    """Pages in the PDF, or `None` if no tool here can count them.

    `qpdf --show-npages` is exact and is what the test uses; `pdfinfo` is the
    second choice (same poppler install as `pdftotext`). The raw-bytes fallback
    counts `/Type /Page` objects, which is right for an uncompressed page tree and
    is only used when neither tool is installed.
    """
    for tool in ["qpdf"] + PAGE_COUNT_TOOLS:
        found = shutil.which(tool) or (tool if Path(tool).is_file() else None)
        if not found:
            continue
        if found.endswith("qpdf"):
            result = subprocess.run([found, "--show-npages", str(pdf)],
                                    capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip().isdigit():
                return int(result.stdout.strip())
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        result = subprocess.run([pdfinfo, str(pdf)], capture_output=True, text=True)
        match = re.search(r"^Pages:\s+(\d+)$", result.stdout, re.M)
        if match:
            return int(match.group(1))
    raw = pdf.read_bytes()
    found = len(re.findall(rb"/Type\s*/Page[^s]", raw))
    return found or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render docs/manual-en.md to a PDF (pandoc + headless Chrome).",
        epilog="Writes dist/ripple-manual-en.pdf by default and prints pages, "
               "bytes and the time it took.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="where to write the PDF (default dist/ripple-manual-en.pdf)")
    ap.add_argument("--html", type=Path, default=None,
                    help="keep the intermediate HTML here instead of the scratch "
                         "directory (for debugging the style sheet)")
    ap.add_argument("--pandoc", default=None, help="path to pandoc")
    ap.add_argument("--chrome", default=None, help="path to Chrome")
    args = ap.parse_args(argv)

    started = time.monotonic()
    if not SOURCE.is_file():
        raise SystemExit("%s does not exist" % SOURCE)
    pandoc = find_pandoc(args.pandoc)
    markdown = SOURCE.read_text(encoding="utf-8")
    images = check_images(markdown)

    scratch = Path(tempfile.mkdtemp(prefix="ripple-pdf-"))
    try:
        html_path = args.html or (scratch / "manual.html")
        html_path.parent.mkdir(parents=True, exist_ok=True)
        build_html(pandoc, html_path)
        render_pdf(html_path, args.out, chrome=args.chrome, scratch=scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    elapsed = time.monotonic() - started
    size = args.out.stat().st_size
    pages = page_count(args.out)
    if pages is None:
        print("pages:   unknown (no qpdf/pdfinfo on this machine)")
    else:
        print("pages:   %d" % pages)
    print("bytes:   %d" % size)
    print("seconds: %.2f" % elapsed)
    print("wrote %s (%d images embedded, %d pages, %d bytes, %.2f s)"
          % (args.out, len(images), pages or 0, size, elapsed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
