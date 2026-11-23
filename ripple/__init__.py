"""RIPPLE — Router Inference Policy Proxy for LLM Engines."""

import sys as _sys

__version__ = "0.2.3"

# The floor `pyproject.toml` declares, said in words instead of a SyntaxError.
# `pip install` refuses an older interpreter on its own; this catches the other
# way in — `python -m ripple` from a checkout, or a script that put the package
# on PYTHONPATH. It is here and not in `__main__.py` because the import of
# `ripple.config` would already fail with a SyntaxError before `main()` runs.
if _sys.version_info < (3, 9):
    raise SystemExit(
        "RIPPLE needs Python 3.9 or newer — this is %d.%d.%d.\n"
        "Install a newer interpreter and run RIPPLE with it, e.g.\n"
        "  uv python install 3.12 && uv venv --python 3.12 .venv\n"
        "  .venv/bin/pip install .\n"
        "Nothing else is required (no sudo, no Homebrew)."
        % _sys.version_info[:3])
