#!/usr/bin/env python3
"""Scan the tracked tree and the whole history for secrets (issue #65).

Why not gitleaks: it is not installed here, it is a download, and the one secret
this repository actually leaks is a four-character key that no entropy rule can
find. So this scanner does two things gitleaks would not do for us:

* `--key-file PATH` reads the real key (the oMLX key in `~/.omlx/api_key.txt` is
  four hex characters — far too short for a pattern) and searches every blob for
  that literal. The value is never printed: the report carries the commit, the
  path and a redacted sample.
* a small set of high-entropy patterns for the tokens that do have a shape
  (`sk-…`, `ghp_…`, `glpat-…`, long hex/base64 runs, private-key blocks).

Exit code 0 = nothing found, 1 = hits, 2 = the scan itself failed. A hit in the
history is not fixable by editing a file: the history has to be rewritten, which
is the operator's decision alone.

    tools/secret_scan.py                          # tree + history
    tools/secret_scan.py --key-file ~/.omlx/api_key.txt
    tools/secret_scan.py --tree-only
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# High-entropy shapes only. A rule that matches the word "password" produces
# noise and gets switched off, which is worse than not having it.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("openai-style", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"
                                r"|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("bearer", re.compile(r"\bBearer\s+([A-Za-z0-9_\-\.]{24,})")),
    ("long-hex", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("long-base64", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
]
# A hex run of 32+ is also a UUID without dashes, a git SHA in a document, a
# sha256 checksum. Those are not secrets, and a rule that cries wolf is dropped
# by the next reader. `long-hex` therefore needs a key-ish word nearby.
HEX_CONTEXT = re.compile(r"(?i)(key|token|secret|passw|credential|auth)")

#: An explicit, visible exception: the scanner's own fixtures and documentation
#: that has to show a key's shape carry this on the line.
ALLOW_MARKER = "secret-scan: allow"

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
                 ".bundle", ".pack", ".idx")


@dataclass(frozen=True)
class Hit:
    rule: str
    where: str          # "<commit> <path>" or "<path>"
    sample: str         # redacted

    def line(self) -> str:
        return f"{self.rule:14} {self.where:60} {self.sample}"


def redact(text: str) -> str:
    """Never echo a secret: keep enough to recognise it, nothing to use it."""
    text = text.strip()
    if len(text) <= 6:
        return "*" * len(text)
    return text[:4] + "*" * max(4, len(text) - 4)


def scan_line(line: str, literal: str | None) -> tuple[str, str] | None:
    """Return (rule, redacted sample) when the line carries a secret.

    A line carrying the marker `secret-scan: allow` is skipped. It exists for the
    scanner's own test fixtures and for documentation that has to show the shape
    of a key; the marker sits in the diff, so a reviewer sees it. It is not a
    pattern for a real key — nothing else is exempt.
    """
    if ALLOW_MARKER in line:
        return None
    if literal and literal in line:
        return "literal-key", redact(literal)
    for name, pattern in PATTERNS:
        for match in pattern.finditer(line):
            value = match.group(1) if match.groups() else match.group(0)
            if name in ("long-hex", "long-base64") and not HEX_CONTEXT.search(line):
                continue
            return name, redact(value)
    return None


def scan_text(text: str, where: str, literal: str | None) -> list[Hit]:
    hits: list[Hit] = []
    for line in text.split("\n"):
        found = scan_line(line, literal)
        if found is not None:
            hits.append(Hit(found[0], where, found[1]))
    return hits


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout


def scan_tree(literal: str | None) -> list[Hit]:
    """Every file `git ls-files` reports, i.e. exactly what would be published."""
    hits: list[Hit] = []
    for path in _git("ls-files", "-z").split("\0"):
        if not path or path.endswith(SKIP_SUFFIXES):
            continue
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        hits.extend(scan_text(text, path, literal))
    return hits


def scan_history(literal: str | None) -> list[Hit]:
    """One pass over `git log -p --all`; the repository is small (204 commits)."""
    hits: list[Hit] = []
    commit = "?"
    path = "?"
    for line in _git("log", "-p", "--all", "--no-color", "--no-ext-diff",
                     "--format=commit %H").split("\n"):
        if line.startswith("commit "):
            commit = line[7:13]
            continue
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if not line.startswith("+"):
            continue
        body = line[1:]
        found = scan_line(body, literal)
        if found is not None:
            hits.append(Hit(found[0], f"{commit} {path}", found[1]))
    return hits


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="secret_scan.py",
                                description="Scan tree and history for secrets.")
    p.add_argument("--key-file", help="file holding a secret to search for "
                                      "literally; the value is never printed")
    p.add_argument("--tree-only", action="store_true")
    p.add_argument("--history-only", action="store_true")
    p.add_argument("--quiet", action="store_true", help="counts only")
    args = p.parse_args(argv)

    literal = None
    if args.key_file:
        try:
            literal = Path(args.key_file).read_text().strip()
        except OSError as exc:
            print(f"cannot read --key-file: {exc}", file=sys.stderr)
            return 2
        if not literal:
            print("--key-file is empty", file=sys.stderr)
            return 2

    try:
        hits: list[Hit] = []
        if not args.history_only:
            hits += scan_tree(literal)
        if not args.tree_only:
            hits += scan_history(literal)
    except subprocess.CalledProcessError as exc:
        print(f"git failed: {exc}", file=sys.stderr)
        return 2

    seen: set[tuple[str, str]] = set()
    unique: list[Hit] = []
    for hit in hits:
        key = (hit.rule, hit.where)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)

    if args.quiet:
        print(f"{len(unique)} hit(s)")
        return 1 if unique else 0

    for hit in unique:
        print(hit.line())
    print(f"\n{len(unique)} hit(s)."
          + (" Nothing to publish yet." if unique else " Clean."))
    return 1 if unique else 0


if __name__ == "__main__":
    raise SystemExit(main())
