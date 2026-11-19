"""Which build is running: the git commit of the working tree, read once.

`ripple.__version__` is the package version and has not changed since the first
commit, so it cannot answer the operator's question "is the new code live?".
The commit can — and the `dirty` flag matters, because an edited tree is not the
commit it claims to be.

Read once per process (`lru_cache`), never at import: a test run or a packaged
install without `.git` must not pay for it, and nothing here may raise.
"""

from __future__ import annotations

import functools
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("ripple.build")

# `ripple/build.py` → the repository root (the parent of the package directory).
REPO = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str | None:
    """`git` output, or None when git is missing, too slow or not a repo."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), *args],
                             capture_output=True, text=True, timeout=2.0,
                             check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("build: git unavailable (%s)", exc)
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


@functools.lru_cache(maxsize=1)
def build_info() -> dict[str, str | bool | None]:
    """`{commit, date, dirty}` — all None when the tree carries no git.

    `dirty` is True when tracked files differ from the commit, which is exactly
    the case where the reported commit would be a half-truth.
    """
    commit = _git("rev-parse", "--short", "HEAD")
    if commit is None:
        return {"commit": None, "date": None, "dirty": None}
    return {
        "commit": commit,
        "date": _git("show", "-s", "--format=%cs", "HEAD"),
        "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
    }
