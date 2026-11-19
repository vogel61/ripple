#!/usr/bin/env python3
"""Generate `docs/dashboard-mockup.html` from a running dashboard (issue #84).

The dashboard is one page with six tabs; this tool renders all six, stacks them
in reading order into a single self-contained file, and (optionally) takes the
screenshot the README shows. Nothing is edited by hand afterwards: whatever looks
wrong in the result is either a bug in this script or a bug in the style sheet.

What it does, in order:

1. Point it at an instance — either one it starts itself (`--config`, on
   `--port`, with a temporary copy of the config whose `listen_port`,
   `log_file`, `stats.file` and `fleet.file` point into a scratch directory, so
   the operator's own instance and state are never touched), or one that is
   already running (`--url`).
2. Drive a headless Chrome over CDP: set the viewport to `--width` x `--height`,
   open each tab by hash, `Page.reload` with `ignoreCache` before each
   measurement, and read the panel's `outerHTML` once the DOM has settled.
3. Assemble one HTML file: the real `ripple/static/dashboard.css` (the wordmark's
   `url()` rewritten to a data URI, so the file loads nothing), the captured
   masthead, tab bar, six panels and footer, plus a contents list and a per-tab
   heading. Inline event handlers and comments are dropped — a snapshot carries
   no JavaScript at all.
4. Verify the result by opening it from `file://`: no `<script>`, no resource
   request, no console error, six tabs present. A snapshot that fetches
   something is not a snapshot.
5. Stop Chrome, stop the instance it started.

Usage:

    ./.venv/bin/python tools/mockup_snapshot.py \
        --config /tmp/ripple-demo.yaml --port 8899 \
        --out docs/dashboard-mockup.html

    # the README picture, from the same instance
    ./.venv/bin/python tools/mockup_snapshot.py --url http://127.0.0.1:8899 \
        --out docs/dashboard-mockup.html \
        --screenshot docs/images/readme-fleet.png

    # every picture in docs/images/ — one run, one instance, one manifest
    ./.venv/bin/python tools/mockup_snapshot.py --manual-crops \
        --screenshot docs/images/readme-fleet.png \
        --out /tmp/dashboard-mockup.html

`--manual-crops` needs no configuration: it starts its own throwaway instance
from a generated file and answers `/healthz`, `/stats` and `/config` through a
fetch shim (`tools/mockup_fleet.py`), so the pictures show a documented test
fleet and not this machine. The regions are the named list `CROPS` below, each
one measured from the rendered DOM — a picture whose rectangle nobody can find
in the code is the next drift.

Why a tool and not a hand-made page: the page keeps changing (three layout
decisions landed within a day of this file being written), so a hand-maintained
"current" mock-up is stale the moment it is committed. Regenerating it is one
command; keeping it in sync by hand is not.

What this tool deliberately does not do: invent data. It renders whatever the
instance reports — `/healthz`, `/stats`, `/config`. If the picture should show a
draining node and a disabled one, the instance has to have them (that is demo
setup, not snapshotting).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
CSS_PATH = REPO / "ripple" / "static" / "dashboard.css"
FONT_PATH = REPO / "ripple" / "static" / "fonts" / "rubik-microbe-wordmark.woff2"

# The test fleet the manual's pictures show. Imported as a sibling module: the
# tool is run both as `python tools/mockup_snapshot.py` (tools/ is on sys.path)
# and as `tools.mockup_snapshot` from the tests (the repo root is).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO))
try:  # pragma: no cover - the import path is a packaging detail
    from tools import mockup_fleet as demo
except ImportError:  # running the file directly: `python tools/mockup_snapshot.py`
    import mockup_fleet as demo

# The six tabs, in the order of the tab bar, with the block numbers each one
# carries. The numbers are the page's own reading order (docs/dashboard-design.md
# §1); the tab number is a position, which is why tab 2 carries block 3.
TABS = [
    ("fleet", "Fleet", "blocks 1 · 2 · 2.1"),
    ("models", "Models", "block 3"),
    ("threads", "Threads", "block 4"),
    ("history", "History", "block 5"),
    ("ops", "Operations", "blocks 6 · 6.1 – 6.6"),
    ("nodes", "Nodes", "blocks 7 · 7.1 – 7.3"),
]

# What each tab needs before it counts as rendered. The page fills itself from
# `/healthz` after `DOMContentLoaded`, so "loaded" is not "rendered".
READY = {
    "fleet": "document.querySelectorAll('.card.host').length > 0"
             " && document.querySelectorAll('.legend-item').length === 7",
    "models": "document.querySelectorAll('#models .model-name, #models tbody tr').length > 0",
    "threads": "document.querySelectorAll('#threads .thread-item').length > 0",
    "history": "document.querySelectorAll('#log tbody tr, #log .log-row').length > 0",
    "ops": "document.querySelectorAll('#panel-ops .ops-block').length >= 6",
    "nodes": "document.querySelectorAll('#panel-nodes .node-row').length > 0",
}

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
    shutil.which("chromium-browser") or "",
]


# ── the parts that are pure text ─────────────────────────────────────────────

def strip_comments(html: str) -> str:
    """Drop HTML comments — the shell's implementation notes are not content."""
    return re.sub(r"\s*<!--.*?-->\s*", "\n", html, flags=re.S)


def strip_handlers(html: str) -> str:
    """Drop inline `on*=` attributes: a snapshot carries no JavaScript at all.

    The page's buttons call functions from `dashboard.js`; here they are the same
    buttons with nothing behind them, so a click does nothing instead of throwing
    a `ReferenceError`.
    """
    return re.sub(r'\s+on[a-z]+="[^"]*"', "", html)


def embed_font(css: str, data_uri: str) -> str:
    """Point the style sheet's wordmark at a data URI instead of the font file.

    The page serves `fonts/rubik-microbe-wordmark.woff2` beside the style sheet;
    a single file cannot. Nothing else in the sheet is touched — and a `url()`
    that is still not a data URI afterwards is an error, because that is exactly
    the fetch this file must not make.
    """
    out = css.replace("url(fonts/rubik-microbe-wordmark.woff2)", "url(%s)" % data_uri)
    leftovers = [m for m in re.findall(r"url\(([^)]*)\)", out) if not m.startswith("data:")]
    if leftovers:
        raise SystemExit("dashboard.css references %r, which this tool does not embed"
                         % leftovers[0])
    return out


def header_comment(*, commit: str, command: str, width: int, height: int,
                   dirty: bool = False, fleet_note: str = "") -> str:
    """The file's head: what it is, which commit it came from, how to redo it."""
    fleet_line = ("\n     The instance reported: %s.\n" % fleet_note) if fleet_note else ""
    return f"""<!-- docs/dashboard-mockup.html — generated, do not edit.

     A snapshot of the built dashboard, not a design. The six tabs of the running
     page, captured one after the other at {width} x {height} and stacked in reading
     order under the headings 1 ... 6: one file, no server, no script, no network.
     The style sheet below is ripple/static/dashboard.css, unchanged except for
     the wordmark's `url()`, which points at a data URI so that opening this file
     loads nothing. The DOM is what the page rendered; the frame around it (the
     contents list, the per-tab headings) is all this tool adds.

     Generated against commit {commit}{" (dirty tree)" if dirty else ""}.{fleet_line}
     Regenerate with:

         {command}

     That command is the whole procedure — it starts (or attaches to) the instance,
     renders the six tabs, and writes this file. Do not edit this file by hand and
     do not "keep it up to date": after every visible change to the dashboard,
     regenerate it. What it renders is whatever that instance reports, so the
     picture's data (nodes, threads, states) comes from the instance, not from
     here.

     The numbering is the page's own, and it is the reading order inside the tabs:
     1 fleet with the KPIs, 2 distribution, 2.1 load per host, 3 models, 4 thread
     registry, 5 last interaction round, 6 operations with 6.1 ... 6.6, 7 nodes
     with 7.1 ... 7.3. The tab bar counts 1 ... 6 and that is a position, not a
     block number — tab 2 (Models) carries block 3.

     What this file is not: the approved design. That is docs/dashboard-design.md,
     which explains the layout, the state system and the measurements behind it.

     Language: English, as the public repository is. The language chooser in the
     masthead is the page's own control and inert here — a snapshot has no script
     to switch it.
-->"""


CHROME_CSS = """
  /* ── Snapshot frame ───────────────────────────────────────────────────────
     Not part of the page. Everything below exists because six tab panels stand
     on one page here instead of one at a time: a contents list, the per-tab
     headings, and a tab bar that cannot switch anything. Nothing in this block
     reaches into the page's own layout. */
  .snap-intro{margin:var(--s5) 0 0;padding:var(--s4) var(--s5);border:1px solid var(--line);
    border-left:2px solid var(--accent);border-radius:var(--radius);background:var(--surface);
    font-size:var(--t-s);line-height:1.55;color:var(--fg-2)}
  .snap-intro p{margin:0}
  .snap-intro b{color:var(--fg);font-weight:600}
  .snap-intro code{font-family:var(--mono);font-size:var(--t-xs);color:var(--fg-2)}
  .snap-toc{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0 var(--s5);
    margin:var(--s4) 0 0;padding:0;list-style:none;font-size:var(--t-s)}
  .snap-toc a{display:flex;gap:var(--s3);padding:7px 0;border-bottom:1px solid var(--line);
    color:var(--fg-2);text-decoration:none}
  .snap-toc a:hover{color:var(--fg);border-bottom-color:var(--line-3)}
  .snap-toc .n{color:var(--fg-3);font-variant-numeric:tabular-nums;min-width:1.1em}
  .snap-toc .b{margin-left:auto;color:var(--fg-3);font-size:var(--t-xs)}
  .snap-frame .tabs{position:static}
  .snap-tab{margin-top:var(--s6);padding-top:var(--s5);border-top:1px solid var(--line-2)}
  .snap-head{display:flex;align-items:baseline;gap:var(--s3);margin:0 0 var(--s2);flex-wrap:wrap}
  .snap-head .kicker{font-size:var(--t-xs);font-weight:600;letter-spacing:.10em;
    text-transform:uppercase;color:var(--fg-3)}
  .snap-head h2{margin:0;font-size:var(--t-2xl);font-weight:600;color:var(--fg);
    letter-spacing:-.01em}
  .snap-head .blk{margin-left:auto;font-size:var(--t-xs);color:var(--fg-3);
    font-variant-numeric:tabular-nums}
  .snap-tab .tab-panel{margin-top:var(--s4)}
  @media (max-width:900px){.snap-toc{grid-template-columns:minmax(0,1fr)}}
"""


def build_document(*, panels: dict[str, str], masthead: str, tabs: str, footer: str,
                   css: str, commit: str, command: str, width: int, height: int,
                   dirty: bool = False, fleet_note: str = "",
                   meta: list[tuple[str, str, str]] | None = None) -> str:
    """The whole file, from the six captured panels and the page's own style sheet.

    Pure: no I/O, no browser. `panels` maps the tab slug to the `outerHTML` of its
    `section.tab-panel`; `css` is `dashboard.css` with the wordmark already
    embedded; `commit` and `command` go into the head comment.
    """
    meta = meta or TABS
    missing = [slug for slug, _, _ in meta if slug not in panels]
    if missing:
        raise ValueError("no panel captured for: " + ", ".join(missing))

    toc = "\n".join(
        '      <li><a href="#tab-%d"><span class="n">%d</span><span>%s</span>'
        '<span class="b">%s</span></a></li>' % (i + 1, i + 1, name, blocks)
        for i, (slug, name, blocks) in enumerate(meta))

    sections = []
    for i, (slug, name, blocks) in enumerate(meta):
        panel = strip_handlers(strip_comments(panels[slug]))
        sections.append(
            '    <section class="snap-tab" id="tab-%d">\n'
            '      <div class="snap-head">\n'
            '        <span class="kicker">Tab %d of %d</span>\n'
            '        <h2>%s</h2>\n'
            '        <span class="blk">%s</span>\n'
            '      </div>\n'
            '%s\n'
            '    </section>' % (i + 1, i + 1, len(meta), name, blocks, panel))

    masthead = strip_handlers(strip_comments(masthead))
    # The chooser is the page's own control and there is no script here: without
    # `selected` the browser would show the first option (German) in an otherwise
    # English snapshot.
    masthead = masthead.replace('<option value="en" lang="en">',
                                '<option value="en" lang="en" selected>')

    return f"""<!DOCTYPE html>
{header_comment(commit=commit, command=command, width=width, height=height,
                             dirty=dirty, fleet_note=fleet_note)}
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RIPPLE — Fleet overview (snapshot of the built page)</title>
<style>
  /* ── ripple/static/dashboard.css, verbatim ──────────────────────────────────
     The only change is the wordmark's `url()`: it points at a data URI instead
     of fonts/rubik-microbe-wordmark.woff2, so this one file loads nothing.
     Rubik Microbe is under the SIL Open Font License 1.1
     (ripple/static/fonts/OFL.txt). */
{css}
{CHROME_CSS}
</style>
</head>
<body>
<div class="wrap snap-frame">

  <div class="snap-intro">
    <p><b>A snapshot of the built page, not a design.</b> The six tabs of the
    running dashboard, captured at {width} × {height} and stacked in reading order —
    one file, no script, no server, no network. The tab bar below is the page's
    own; it cannot switch anything here, and neither can the language chooser
    (this snapshot is English). Each tab carries its number and its name, and the
    block numbers inside are the page's own reading order.</p>
    <ol class="snap-toc">
{toc}
    </ol>
  </div>

{masthead}

{strip_handlers(strip_comments(tabs))}

{chr(10).join(sections)}

{strip_handlers(strip_comments(footer))}
</div>
</body>
</html>
"""


# ── a minimal CDP client (stdlib only, so the tool needs no dependency) ──────

class CDPError(RuntimeError):
    pass


class WebSocket:
    """Just enough WebSocket to speak CDP: text frames, client-side masking."""

    def __init__(self, url: str, timeout: float = 60.0):
        u = urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + ("?" + u.query if u.query else "")
        self.sock.sendall((
            "GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
            % (path, u.hostname, u.port, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake: connection closed")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise CDPError("websocket handshake failed: %r" % head[:200])
        self._buf = rest

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, payload: str) -> None:
        data = payload.encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = int.from_bytes(self._read(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read(8), "big")
            if b2 & 0x80:  # masked server frame: not allowed, but skip the key
                self._read(4)
            payload = self._read(length)
            if opcode == 0x8:
                raise CDPError("websocket closed by peer")
            if opcode == 0x9:  # ping → pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """One page target: `call()` for commands, `eval()` for expressions."""

    def __init__(self, ws_url: str):
        self.ws = WebSocket(ws_url)
        self._id = 0

    def call(self, method: str, **params):
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise CDPError("%s: %s" % (method, msg["error"].get("message")))
                return msg.get("result", {})

    def eval(self, expression: str):
        res = self.call("Runtime.evaluate", expression=expression,
                        returnByValue=True, awaitPromise=True)
        if res.get("exceptionDetails"):
            raise CDPError("evaluate failed: %s" % res["exceptionDetails"].get("text"))
        return res["result"].get("value")

    def close(self) -> None:
        self.ws.close()


def find_chrome(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("no Chrome found — pass --chrome /path/to/Chrome")


def launch_chrome(chrome: str, profile: Path) -> tuple[subprocess.Popen, str]:
    """Start headless Chrome and return (process, page websocket URL)."""
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--remote-debugging-port=0",
         "--user-data-dir=%s" % profile, "--no-first-run",
         "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars",
         "--allow-file-access-from-files", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    active = profile / "DevToolsActivePort"
    for _ in range(150):
        if active.is_file() and active.read_text().strip():
            break
        if proc.poll() is not None:
            raise SystemExit("Chrome exited before it was ready")
        time.sleep(0.1)
    else:
        proc.kill()
        raise SystemExit("Chrome did not open its debugging port")
    port = int(active.read_text().split()[0])
    for _ in range(100):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
                targets = json.load(r)
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return proc, target["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.1)
    proc.kill()
    raise SystemExit("Chrome has no page target")


# ── the instance ─────────────────────────────────────────────────────────────

def wait_for_instance(base: str, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=3) as r:
                return json.load(r)
        except OSError:
            time.sleep(0.3)
    raise SystemExit("instance at %s did not answer /healthz" % base)


def start_instance(config: Path, port: int, scratch: Path) -> subprocess.Popen:
    """Start a throwaway RIPPLE on `port` from a copy of `config`.

    The copy exists so nothing the tool does can reach the operator's instance or
    its state: `listen_port` moves to `port`, and the log, the ledger and the
    fleet overlay point into `scratch`.
    """
    text = config.read_text()
    text = re.sub(r"(?m)^listen_port:.*$", "listen_port: %d" % port, text)
    text = re.sub(r"(?m)^listen_host:.*$", "listen_host: 127.0.0.1", text)
    text = re.sub(r"(?m)^log_file:.*$", "log_file: %s" % (scratch / "ripple.log"), text)
    text = re.sub(r"(?m)^(\s*)file:\s*\S*host_stats\.json\s*$",
                  r"\1file: %s" % (scratch / "host_stats.json"), text)
    text = re.sub(r"(?m)^(\s*)file:\s*\S*fleet\.yaml\s*$",
                  r"\1file: %s" % (scratch / "fleet.yaml"), text)
    for key in ("maintenance_file", "model_policy_file", "calibration_file"):
        text = re.sub(r"(?m)^(\s*)%s:\s*\S+.*$" % key,
                      r"\1%s: %s" % (key, scratch / (key.replace("_file", "") + ".yaml")),
                      text)
    if "listen_port: %d" % port not in text:
        raise SystemExit("could not set listen_port in %s" % config)
    copy = scratch / "config.yaml"
    copy.write_text(text)
    return subprocess.Popen([sys.executable, "-m", "ripple", "--config", str(copy)],
                            cwd=str(REPO), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


# ── rendering ────────────────────────────────────────────────────────────────

def capture(cdp: CDP, base: str, width: int, height: int, lang: str,
            admin_key: str | None, settle: float = 1.2) -> dict:
    """Render every tab at `width` x `height` and return the captured fragments."""
    cdp.call("Page.enable")
    cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
             deviceScaleFactor=1, mobile=False)
    _hide_scrollbars(cdp)
    cdp.call("Page.navigate", url=base + "/#fleet")
    _wait_ready(cdp, "document.readyState === 'complete'")
    cdp.eval("localStorage.setItem('ripple_lang', %s);" % json.dumps(lang))
    if admin_key:
        cdp.eval("sessionStorage.setItem('ripple_admin', %s);" % json.dumps(admin_key))

    fragments = {}
    for slug, _, _ in TABS:
        # The viewport is set before every measurement, and every tab is a fresh
        # load: the page keeps its tab in the URL hash, so a reload lands on the
        # same one.
        cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                 deviceScaleFactor=1, mobile=False)
        cdp.call("Page.navigate", url="%s/#%s" % (base, slug))
        cdp.call("Page.reload", ignoreCache=True)
        _wait_ready(cdp, "document.readyState === 'complete'")
        try:
            _wait_ready(cdp, READY.get(slug, "true"), timeout=20)
        except CDPError:
            print("warning: %s did not reach its ready state, capturing anyway" % slug,
                  file=sys.stderr)
        time.sleep(settle)
        fragments[slug] = cdp.eval(
            "document.querySelector('#panel-%s').outerHTML" % slug)
        if not fragments[slug] or "<section" not in fragments[slug]:
            raise SystemExit("panel %s came back empty" % slug)

    if "fleet" in fragments:
        frag = cdp.eval("""(() => JSON.stringify({
            masthead: document.querySelector('header.masthead').outerHTML,
            tabs: document.querySelector('nav.tabs').outerHTML,
            footer: document.querySelector('footer').outerHTML}))()""")
        fragments.update(json.loads(frag))
    return fragments


def _hide_scrollbars(cdp: CDP) -> None:
    """No scrollbar gutter in the capture — the width is the width."""
    try:
        cdp.call("Emulation.setScrollbarsHidden", hidden=True)
    except CDPError:
        pass  # an older Chrome; the gutter is cosmetic


def _wait_ready(cdp: CDP, expression: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if cdp.eval("Boolean(%s)" % expression):
                return
        except CDPError:
            pass
        time.sleep(0.2)
    raise CDPError("condition never became true: %s" % expression)


def screenshot(cdp: CDP, base: str, out: Path, width: int, height: int,
               scale: int, lang: str, admin_key: str | None, settle: float = 2.0) -> None:
    """The README picture: the fleet tab, full height, at `scale` device pixels."""
    cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
             deviceScaleFactor=scale, mobile=False)
    _hide_scrollbars(cdp)
    cdp.call("Page.navigate", url=base + "/#fleet")
    _wait_ready(cdp, "document.readyState === 'complete'")
    cdp.eval("localStorage.setItem('ripple_lang', %s);" % json.dumps(lang))
    if admin_key:
        cdp.eval("sessionStorage.setItem('ripple_admin', %s);" % json.dumps(admin_key))
    cdp.call("Page.reload", ignoreCache=True)
    _wait_ready(cdp, "document.readyState === 'complete'")
    _wait_ready(cdp, READY["fleet"], timeout=20)
    time.sleep(settle)
    size = cdp.call("Page.getLayoutMetrics")["cssContentSize"]
    data = cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                    clip={"x": 0, "y": 0, "width": size["width"],
                          "height": size["height"], "scale": 1})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(data["data"]))
    print("screenshot %s (%d x %d css px, %d x %d device px)"
          % (out, size["width"], size["height"], size["width"] * scale,
             size["height"] * scale))


# ── the manual's pictures: named crops, measured from the DOM ────────────────
# Every picture under docs/images/ comes out of one run of this tool. The list
# below is the whole truth about what is on which picture and why that
# rectangle: the rectangle itself is *measured* (getBoundingClientRect of the
# named element), never written down as a number — a crop whose rectangle
# nobody can find in the code is the next drift.
#
# `mode="full"` is the viewport, 1440 x 1000 at 1 device pixel: the tab as an
# operator sees it on a normal screen. `mode="detail"` is one panel, clipped
# from its own rectangle and rendered at `--crop-scale` (2 by default), which
# is what makes the 11 px labels of the card readable in the manual.
DEMO_CONFIG = """\
# Throwaway configuration for the pictures (issue #104). The fleet in the
# pictures does not come from here: `tools/mockup_fleet.py` answers
# /healthz, /stats and /config through a fetch shim. This file only has to let
# an instance start on a free port — the addresses point at a closed port so
# start-up does not wait for a timeout.
listen_host: 127.0.0.1
listen_port: {port}
log_level: warning
log_file: {scratch}/ripple.log
sites:
  - id: hub
    name: Hub
    networks: ["192.0.2.0/24"]
  - id: lab
    name: Lab
    networks: ["198.51.100.0/24"]
hosts:
  - name: node-a
    role: entry
    site: hub
    weight: 3
    addresses: ["http://127.0.0.1:9"]
  - name: node-b
    role: worker
    site: lab
    weight: 2
    addresses: ["http://127.0.0.1:9"]
stats:
  file: {scratch}/host_stats.json
fleet:
  file: {scratch}/fleet.yaml
placement:
  maintenance_file: {scratch}/maintenance.yaml
  model_policy_file: {scratch}/models.yaml
  loading:
    calibration_file: {scratch}/calibration.yaml
"""

# The admin key the pictures are taken with. It is not a secret and it is not
# the instance's key: the shim answers every admin call, and the page only needs
# *a* value in `sessionStorage` to draw its controls unlocked.
DEMO_ADMIN_KEY = "demo-admin-key"

# The one element every picture's tooltip is shown from: the measured memory
# number of the first card, whose tooltip names the definition and the field
# (`/healthz → hosts[].used_bytes_measured`) — the manual's §5.1 example.
TIP_OWNER = ".card.host .mem-amount b.num"


@dataclass(frozen=True)
class Crop:
    """One picture: which tab, which element, and what it is for."""

    name: str                 # the file name, without .png
    tab: str                  # fleet | models | threads | history | ops | nodes | keys
    what: str                 # the line the manifest prints
    selector: str | tuple[str, ...] | None = None   # None = the whole viewport
    setup: str = ""           # JavaScript run after the tab rendered
    mode: str = "detail"      # "detail" (clip, 2x) | "full" (viewport, 1x)
    height: int = 0           # full views: viewport height (default 1000)
    pad: int = 12             # detail crops: breathing room around the element
    lang: str = ""            # "" = --lang
    fixed: bool = False       # element is position:fixed — viewport coords, no scroll


def _open_ops() -> str:
    """Operations is a `<details>`; a closed one has nothing to photograph."""
    return ("(() => { const d = document.querySelector('#ops');"
            " if (d) d.open = true; return true; })()")


def _preview() -> str:
    """`Preview` on the machine that is only ever previewed (manual §5.6.1)."""
    return ("(async () => { const el = document.querySelector('#nodeAddress');"
            " if (el) el.value = 'http://192.0.2.30:8000';"
            " await RIPPLE.nodePreview('http://192.0.2.30:8000'); return true; })()")


def _ops_maintenance() -> str:
    """Open Operations and set the window through the page's own control.

    §6.1 is a sentence about a window that is *set*, so the picture sets one:
    `maintenance('node-b', 30)` asks (the shim answers yes), posts, and prints
    the answer under 6.1. The card in the Fleet tab reads the same window from
    `/healthz`, which is why both pictures agree.
    """
    return ("(async () => { const d = document.querySelector('#ops');"
            " if (d) d.open = true;"
            " await RIPPLE.maintenance('node-b', 30); return true; })()")


def _scan() -> str:
    """`Find in the network` over the documentation range (manual §5.6.2)."""
    return ("(async () => { const el = document.querySelector('#nodeCidr');"
            " if (el) el.value = '192.0.2.0/28';"
            " await RIPPLE.nodeScan(); return true; })()")


def _drain_open() -> str:
    """Open the drain dialog for the draining node — the page's own function,
    fed by `/healthz → hosts[].inflight` and `models[].hosts[].inflight`."""
    return "(() => { RIPPLE.nodeAskRemove('node-a'); return true; })()"


def _drain_timeout() -> str:
    """Confirm the drain; the shim answers `409 drain_timeout`, so the page
    prints the sentence and keeps the dialog with the fresh numbers."""
    return ("(async () => { RIPPLE.nodeAskRemove('node-a');"
            " await RIPPLE.nodeDrainConfirm(); return true; })()")


CROPS: tuple[Crop, ...] = (
    # The tab bar itself: six numbered tabs and the language chooser's
    # neighbours. Narrow on purpose — the picture answers "which tabs are there
    # and in which order", nothing else.
    Crop("manual-tabs", "fleet", "the tab bar, six numbered tabs",
         selector="nav.tabs", pad=10),

    # The whole Fleet tab at 1440 x 1000: legend, both cards, KPI strip.
    Crop("manual-fleet", "fleet", "the Fleet tab: legend, two cards, KPI strip",
         mode="full"),

    # One node card, clipped: the two memory numbers, the bar with the 95 %
    # marker, the four metrics and the maintenance row are the §5.1 argument,
    # and at 2x the 11 px labels are readable.
    Crop("manual-fleet-card", "fleet", "one node card: memory bar and metrics",
         selector=".card.host", pad=14),

    # The tooltip layer, shown over the measured memory number. The box is the
    # page's own `#tip-layer`, positioned by the page's own code — the crop is
    # the tooltip and nothing else, because that is what the manual shows.
    Crop("manual-tooltip", "fleet", "a tooltip: definition and source",
         selector="#tip-layer", pad=8, fixed=True,
         setup="(() => { window.scrollTo(0, 0);"
               " const el = document.querySelector(%r);"
               " if (!el) return 'no owner';"
               " el.dispatchEvent(new PointerEvent('pointerover', {bubbles: true}));"
               " return true; })()" % TIP_OWNER),

    # The Models tab: loaded rows, the dimmed fold and the policy fields.
    Crop("manual-models", "models", "the Models tab: rows, fold, policy fields",
         mode="full"),

    # The Threads tab: six conversations, one of them working.
    Crop("manual-threads", "threads", "the Threads tab: six conversations",
         mode="full"),

    # The one busy thread, clipped at 2x: green model name, `working`, and the
    # delete button off — §5.3's second picture.
    Crop("manual-thread-busy", "threads", "one thread with a request in flight",
         selector='#threads .thread-item[data-busy="true"]', pad=12),

    # The History tab: the filter bar, the rows with the sid column, newest
    # first, the footer with median/p90.
    Crop("manual-history", "history", "the History tab: filters, rows, footer",
         mode="full"),

    # The Operations tab with the maintenance sentence of 6.1 set: the window
    # is opened through the page's own control, so the picture shows what the
    # page prints after the operator set it.
    Crop("manual-ops", "ops", "the Operations tab with a maintenance window set",
         mode="full", setup=_ops_maintenance()),

    # Block 6.6 alone, at 2x: the restart control and the reasons it can be off.
    # The crop is the whole block (`:has()` finds the one that carries the
    # panel), because the heading "6.6 Restart" is part of what the manual says
    # the block is.
    Crop("manual-restart", "ops", "the restart block (6.6)",
         selector=".ops-block:has(#restartPanel)", pad=14, setup=_open_ops()),

    # The Nodes tab with a preview filled in: 7.1 read a node and wrote nothing.
    Crop("manual-nodes", "nodes", "the Nodes tab, with a preview",
         mode="full", setup=_preview()),

    # 7.2's answer list alone: three candidates, one of them already in the
    # fleet. Clipped to `#nodeFound`, because the block's forms are the same
    # fields the picture above already shows.
    Crop("manual-nodes-scan", "nodes", "a sweep over a documentation range",
         selector="#nodeFound", pad=12, setup=_scan()),

    # The drain dialog: what is in flight, on which model, and the deadline.
    Crop("manual-node-drain", "nodes", "the drain dialog",
         selector="#nodeDrain", pad=12, setup=_drain_open()),

    # The deadline passing: the same dialog with the fresh numbers and the
    # page's sentence under it. Both elements, because the sentence is half of
    # what the manual claims ("nothing is removed").
    Crop("manual-node-drain-timeout", "nodes", "the deadline passing",
         selector=("#nodeDrain", "#nodeMsg"), pad=12, setup=_drain_timeout()),

    # The key page: four keys, masks only, one disabled. Its own page, its own
    # height — the table is ~430 px tall and a 1000 px picture would be half
    # empty.
    Crop("manual-keys", "keys", "the key page (/keys)",
         mode="full", height=500, setup="(() => load())()"),

    # The four pictures the install guides carry: the same two tabs, in English
    # and in German. They exist so install.md and install-de.md show the same
    # build the manual does, not a snapshot from another week.
    Crop("dashboard-fleet", "fleet", "the Fleet tab, for install.md",
         mode="full"),
    Crop("dashboard-fleet-de", "fleet", "the Fleet tab, German, for install-de.md",
         mode="full", lang="de"),
    Crop("dashboard-models", "models", "the Models tab, for install.md",
         mode="full"),
    Crop("dashboard-models-de", "models", "the Models tab, German, for install-de.md",
         mode="full", lang="de"),
)


def write_demo_config(scratch: Path, port: int) -> Path:
    """The throwaway configuration `--manual-crops` starts its instance with."""
    path = scratch / "demo.yaml"
    path.write_text(DEMO_CONFIG.format(port=port, scratch=scratch))
    return path


def install_shim(cdp: CDP, now: float | None = None) -> None:
    """Answer the page's `/healthz`, `/stats` and `/config` from the test fleet.

    `Page.addScriptToEvaluateOnNewDocument` runs before any page script on every
    navigation, so `dashboard.js` never sees the instance's own (empty) fleet and
    the pictures cannot depend on what happens to be running on this machine.
    The commit the page prints in its footer is the repository's, not a made-up
    hash: the picture names the build it came from.
    """
    commit, dirty = repo_commit()
    cdp.call("Page.addScriptToEvaluateOnNewDocument",
             source=demo.shim_script(now, commit=commit, dirty=dirty))


def measure(cdp: CDP, crop: Crop) -> dict:
    """The rectangle of a crop's element(s), in document coordinates.

    Measured, not guessed: `getBoundingClientRect` of the selector the crop
    names, scrolled into view first so the element has been laid out and
    painted. Several selectors are unioned — the drain-timeout picture is the
    dialog *and* the sentence under it.
    """
    selectors = ([crop.selector] if isinstance(crop.selector, str)
                 else list(crop.selector))
    scroll = ("" if crop.fixed
              else "els[0].scrollIntoView({block: 'center', inline: 'nearest'});")
    offset = ("" if crop.fixed else " + window.scrollX")
    offset_y = ("" if crop.fixed else " + window.scrollY")
    report = json.loads(cdp.eval("""(() => {
      const sels = %s;
      const els = sels.map(s => document.querySelector(s));
      const missing = sels.filter((s, i) => !els[i]);
      if (missing.length) return JSON.stringify({missing: missing});
      %s
      const rects = els.map(el => {
        const r = el.getBoundingClientRect();
        return {x: r.left%s, y: r.top%s, w: r.width, h: r.height};
      });
      const de = document.documentElement;
      return JSON.stringify({rects: rects, docW: de.scrollWidth, docH: de.scrollHeight});
    })()""" % (json.dumps(selectors), scroll, offset, offset_y)))
    if report.get("missing"):
        raise SystemExit("crop %s: no element matches %s — the picture's "
                         "rectangle must exist in the page"
                         % (crop.name, ", ".join(report["missing"])))
    rects = report["rects"]
    x0 = min(r["x"] for r in rects) - crop.pad
    y0 = min(r["y"] for r in rects) - crop.pad
    x1 = max(r["x"] + r["w"] for r in rects) + crop.pad
    y1 = max(r["y"] + r["h"] for r in rects) + crop.pad
    x0, y0 = max(0, x0), max(0, y0)
    x1 = min(report["docW"], x1)
    y1 = min(report["docH"], y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise SystemExit("crop %s: the element has no area (%d x %d)"
                         % (crop.name, x1 - x0, y1 - y0))
    return {"x": round(x0), "y": round(y0),
            "width": round(x1 - x0), "height": round(y1 - y0)}


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from the PNG header — the file, not the intention."""
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("%s is not a PNG" % path)
    return struct.unpack(">II", head[16:24])


def capture_crops(cdp: CDP, base: str, outdir: Path, *, width: int, height: int,
                  lang: str, admin_key: str | None, scale: int,
                  only: list[str] | None = None,
                  settle: float = 1.0) -> list[dict]:
    """Render every crop in `CROPS` and return what was written.

    One fresh load per picture: the page keeps its state in the DOM and in
    `nodeState`, and a drain dialog left open would appear in the next tab's
    picture. The viewport is set before every measurement and every load is a
    `Page.reload` with `ignoreCache`, because a stale window size is measured as
    if it were the current one.
    """
    cdp.call("Page.enable")
    written: list[dict] = []
    wanted = [c for c in CROPS if not only or c.name in only]
    unknown = sorted(set(only or ()) - {c.name for c in CROPS})
    if unknown:
        raise SystemExit("no such crop: " + ", ".join(unknown))
    for crop in wanted:
        crop_lang = crop.lang or lang
        url = base + ("/keys" if crop.tab == "keys" else "/#" + crop.tab)
        view_h = (crop.height or 1000) if crop.mode == "full" else height
        cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=view_h,
                 deviceScaleFactor=1, mobile=False)
        _hide_scrollbars(cdp)
        cdp.call("Page.navigate", url=url)
        _wait_ready(cdp, "document.readyState === 'complete'")
        cdp.eval("localStorage.setItem('ripple_lang', %s);" % json.dumps(crop_lang))
        key = admin_key or DEMO_ADMIN_KEY
        cdp.eval("sessionStorage.setItem('ripple_admin', %s);" % json.dumps(key))
        cdp.call("Page.reload", ignoreCache=True)
        _wait_ready(cdp, "document.readyState === 'complete'")
        if crop.tab != "keys":
            _wait_ready(cdp, READY.get(crop.tab, "true"), timeout=25)
        time.sleep(settle)
        if crop.setup:
            result = cdp.eval(crop.setup)
            if result is False:
                raise SystemExit("crop %s: its setup did not run" % crop.name)
            time.sleep(0.6)
        if crop.mode == "full":
            clip = {"x": 0, "y": 0, "width": width,
                    "height": crop.height or 1000, "scale": 1}
        else:
            box = measure(cdp, crop)
            time.sleep(0.15)
            clip = {"x": box["x"], "y": box["y"], "width": box["width"],
                    "height": box["height"], "scale": scale}
        data = cdp.call("Page.captureScreenshot", format="png",
                        # A fixed element (the tooltip) is positioned against the
                        # viewport: capturing beyond it re-lays the page out and
                        # moves the box away from the clip.
                        captureBeyondViewport=not crop.fixed, clip=clip)
        path = outdir / (crop.name + ".png")
        outdir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data["data"]))
        w, h = png_size(path)
        size = path.stat().st_size
        if size < 2000:
            raise SystemExit("crop %s: %d bytes — that is not a picture"
                             % (crop.name, size))
        print("%-26s %4d x %-4d %7d B  %s" % (crop.name, w, h, size, crop.what))
        written.append({"name": crop.name + ".png", "w": w, "h": h, "bytes": size,
                        "what": crop.what, "clip": clip})
    return written


def write_manifest(path: Path, *, command: str, commit: str, dirty: bool,
                   rows: list[dict]) -> None:
    """`docs/images/MANIFEST.md`: which run produced these files, and how big.

    The point of the file is not bookkeeping: a picture that is older than the
    code is a claim nobody can check, and one that is older than the manifest is
    one nobody can dispute. Name the commit, name the command, list the pixels.
    """
    date = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "# The pictures in docs/images — generated, do not edit",
        "",
        "Every file in this directory comes out of one run of",
        "`tools/mockup_snapshot.py`. The run starts a throwaway instance on a free",
        "port, answers `/healthz`, `/stats` and `/config` from the test fleet in",
        "`tools/mockup_fleet.py` (documentation addresses, node-a/node-b/node-c,",
        "invented model names), renders the tabs in headless Chrome and writes the",
        "pictures and this file. Nothing here was cut, scaled or retouched by hand;",
        "a picture that looks wrong is a bug in the tool or in the style sheet.",
        "",
        "| | |",
        "|---|---|",
        "| **Command** | `%s` |" % command,
        "| **Commit** | `%s`%s |" % (commit, " (dirty tree)" if dirty else ""),
        "| **Date** | %s |" % date,
        "| **Files** | %d |" % len(rows),
        "",
        "| file | pixels | bytes | shows |",
        "|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: r["name"]):
        lines.append("| `%s` | %d × %d | %d | %s |"
                     % (row["name"], row["w"], row["h"], row["bytes"], row["what"]))
    lines += [
        "",
        "Detail crops are clipped from the rectangle of the panel they show",
        "(`getBoundingClientRect`, plus a few pixels of air) and rendered at 2×;",
        "full views are the 1440 × 1000 viewport at 1×. `readme-fleet.png` is the",
        "whole Fleet tab at 2×, the picture the README carries.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


def repo_commit() -> tuple[str, bool]:
    """`git rev-parse --short HEAD` and whether the tree is dirty.

    The pictures this run writes are excluded from the dirtiness check: a run
    that only differs from `HEAD` in `docs/images/` is exactly the run that is
    supposed to produce them, and calling that tree dirty would put a warning on
    every honest manifest.
    """
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO),
                                capture_output=True, text=True, check=True).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", ".", ":(exclude)docs/images"],
            cwd=str(REPO), capture_output=True, text=True, check=True).stdout
        return commit, bool(status.strip())
    except (OSError, subprocess.CalledProcessError):
        return "unknown", False


def command_line() -> str:
    """The command that reproduces this run, with the venv interpreter.

    The arguments are the ones this process was started with, quoted — not a
    sentence written next to the code that could drift from it.
    """
    return " ".join(["./.venv/bin/python tools/mockup_snapshot.py"]
                    + [shlex.quote(a) for a in sys.argv[1:]])


def verify(cdp: CDP, path: Path) -> None:
    """Open the written file from disk and prove it is self-contained."""
    cdp.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
             deviceScaleFactor=1, mobile=False)
    cdp.call("Page.navigate", url=path.resolve().as_uri())
    _wait_ready(cdp, "document.readyState === 'complete'")
    time.sleep(0.5)
    report = json.loads(cdp.eval("""(() => JSON.stringify({
        scripts: document.scripts.length,
        resources: performance.getEntriesByType('resource').map(e => e.name),
        tabs: document.querySelectorAll('.snap-tab').length,
        legend: document.querySelectorAll('.legend-item').length,
        font: document.fonts.check('16px "Rubik Microbe"')}))()"""))
    problems = []
    if report["scripts"]:
        problems.append("%d <script> element(s)" % report["scripts"])
    if report["resources"]:
        problems.append("fetches %r" % report["resources"])
    if report["tabs"] != len(TABS):
        problems.append("%d tabs instead of %d" % (report["tabs"], len(TABS)))
    if not report["font"]:
        problems.append("the embedded wordmark did not load")
    if problems:
        raise SystemExit("the snapshot is not self-contained: " + "; ".join(problems))
    print("verified: %d tabs, %d legend entries, no script, no request"
          % (report["tabs"], report["legend"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render the six dashboard tabs into one self-contained HTML file.")
    ap.add_argument("--config", type=Path,
                    help="configuration of a throwaway instance to start")
    ap.add_argument("--port", type=int, default=8899,
                    help="port for that instance (default 8899)")
    ap.add_argument("--url", help="use an instance that is already running")
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "dashboard-mockup.html")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--admin-key", default=os.environ.get("RIPPLE_ADMIN_KEY"),
                    help="unlock the operator controls in the capture "
                         "(default: $RIPPLE_ADMIN_KEY)")
    ap.add_argument("--screenshot", type=Path,
                    help="also write the fleet tab as a PNG (the README picture)")
    ap.add_argument("--screenshot-scale", type=int, default=2,
                    help="device pixel ratio for --screenshot (default 2)")
    ap.add_argument("--manual-crops", action="store_true",
                    help="also write every picture of docs/images/ (the manual's "
                         "15 crops, the install guides' 4 views) from the test "
                         "fleet in tools/mockup_fleet.py")
    ap.add_argument("--crop-out", type=Path, default=REPO / "docs" / "images",
                    help="directory for --manual-crops (default docs/images)")
    ap.add_argument("--crop-scale", type=int, default=2,
                    help="device pixel ratio for detail crops (default 2)")
    ap.add_argument("--crop-only", nargs="+", metavar="NAME", default=None,
                    help="with --manual-crops: render only these crops")
    ap.add_argument("--manifest", type=Path, default=None,
                    help="write the picture manifest here "
                         "(default <crop-out>/MANIFEST.md)")
    ap.add_argument("--chrome", help="path to Chrome (default: the usual places)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the file:// self-containment check")
    args = ap.parse_args(argv)

    if not args.config and not args.url and not args.manual_crops:
        ap.error("either --config or --url is required (or --manual-crops, which "
                 "starts its own instance from a throwaway configuration)")

    scratch = Path(tempfile.mkdtemp(prefix="ripple-snapshot-"))
    instance = None
    chrome_proc = None
    cdp = None
    rows: list[dict] = []
    try:
        base = args.url.rstrip("/") if args.url else "http://127.0.0.1:%d" % args.port
        config = args.config
        if not args.url and not config:
            if not args.manual_crops:
                ap.error("either --config or --url is required")
            # The pictures do not need an operator's configuration: the shim
            # answers the three endpoints, so the instance only has to serve
            # the static files and the shell. It gets its own throwaway file.
            config = write_demo_config(scratch, args.port)
            print("starting an instance on :%d from a throwaway configuration"
                  % args.port)
        elif not args.url:
            print("starting an instance on :%d from %s" % (args.port, config))
        if not args.url:
            instance = start_instance(config, args.port, scratch)
            time.sleep(0.5)
            if instance.poll() is not None:
                raise SystemExit("the instance exited at once — is :%d already in "
                                 "use?" % args.port)
        health = wait_for_instance(base)
        if not args.url:
            # Prove the thing we are photographing is the instance this run
            # started, not something that happened to listen on the port.
            with urllib.request.urlopen(base + "/config", timeout=5) as r:
                source = (json.load(r) or {}).get("source")
            expected = str(scratch / "config.yaml")
            if source != expected:
                raise SystemExit(":%d is not this run's instance (its config is %r, "
                                 "not %r)" % (args.port, source, expected))
        build = health.get("build") or {}
        commit = build.get("commit") or "unknown"
        dirty = bool(build.get("dirty"))

        chrome_proc, ws_url = launch_chrome(find_chrome(args.chrome), scratch / "chrome")
        cdp = CDP(ws_url)
        if args.manual_crops:
            # Before any page script runs, on every navigation: the pictures show
            # the test fleet, never whatever this machine happens to serve.
            install_shim(cdp)

        fragments = capture(cdp, base, args.width, args.height, args.lang, args.admin_key)
        css = embed_font(CSS_PATH.read_text(),
                         "data:font/woff2;base64,"
                         + base64.b64encode(FONT_PATH.read_bytes()).decode())
        out_arg = (str(args.out.relative_to(REPO))
                   if args.out.is_absolute() and REPO in args.out.parents else str(args.out))
        if args.url:
            command = ("./.venv/bin/python tools/mockup_snapshot.py --url %s --out %s"
                       % (base, out_arg))
        elif args.manual_crops and not args.config:
            # The throwaway configuration is written by the tool itself, so
            # `--manual-crops` alone reproduces this file. Naming the generated
            # path (or `--config None`) would print a command that does not run,
            # which is worse than no command at all.
            command = ("./.venv/bin/python tools/mockup_snapshot.py --manual-crops"
                       " --port %d --out %s" % (args.port, out_arg))
        else:
            command = ("./.venv/bin/python tools/mockup_snapshot.py --config %s --port %d"
                       " --out %s" % (args.config, args.port, out_arg))
        if args.admin_key:
            # The key is what unlocks the operator controls in the capture; it is
            # read from the environment so it never lands in this file.
            command += ' --admin-key "$RIPPLE_ADMIN_KEY"'
        # Which fleet the head names: with the shim installed the page never saw
        # the instance's own (empty) fleet, so the note has to come from the same
        # payload the page rendered. Naming the throwaway instance's nodes there
        # would describe a different page than the one below it.
        reported = (demo.healthz(commit=commit, dirty=dirty)["hosts"]
                    if args.manual_crops else (health.get("hosts") or []))
        fleet_note = ", ".join("%s (%s)" % (h.get("name"), h.get("state"))
                               for h in reported)
        document = build_document(panels=fragments, masthead=fragments["masthead"],
                                  tabs=fragments["tabs"], footer=fragments["footer"],
                                  css=css, commit=commit, command=command,
                                  width=args.width, height=args.height, dirty=dirty,
                                  fleet_note=fleet_note)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(document)
        print("wrote %s (%d bytes) against commit %s" % (args.out, len(document), commit))

        if args.screenshot:
            screenshot(cdp, base, args.screenshot, args.width, args.height,
                       args.screenshot_scale, args.lang, args.admin_key)
            w, h = png_size(args.screenshot)
            rows.append({"name": args.screenshot.name, "w": w, "h": h,
                         "bytes": args.screenshot.stat().st_size,
                         "what": "the Fleet tab, full height (the README picture)"})
        if args.manual_crops:
            rows += capture_crops(cdp, base, args.crop_out, width=args.width,
                                  height=args.height, lang=args.lang,
                                  admin_key=args.admin_key, scale=args.crop_scale,
                                  only=args.crop_only)
            manifest = args.manifest or (args.crop_out / "MANIFEST.md")
            run_commit, run_dirty = repo_commit()
            write_manifest(manifest, command=command_line(), commit=run_commit,
                           dirty=run_dirty, rows=rows)
            print("wrote %s (%d files, commit %s%s)"
                  % (manifest, len(rows), run_commit, ", dirty tree" if run_dirty else ""))
        if not args.no_verify:
            verify(cdp, args.out)
    finally:
        if cdp is not None:
            cdp.close()
        if chrome_proc is not None and chrome_proc.poll() is None:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()
        if instance is not None and instance.poll() is None:
            instance.terminate()
            try:
                instance.wait(timeout=15)
            except subprocess.TimeoutExpired:
                instance.kill()
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
