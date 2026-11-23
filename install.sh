#!/usr/bin/env bash
# RIPPLE — install into a virtual environment next to this repository.
#
#   ./install.sh            create .venv, install RIPPLE, offer `ripple init`
#   ./install.sh --check    only report what is missing, write nothing
#
# Written for someone who has never installed a Python program: it says what is
# missing and prints the line to copy, instead of failing with
# `command not found` (issue #178). RIPPLE's own dependencies (fastapi,
# uvicorn, httpx, PyYAML) are installed by pip; Python itself is the one thing
# nobody installs, and macOS has shipped none since 12.3.
set -u

cd "$(dirname "$0")" || exit 1
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '%s\n' "$*"; }
fail() { printf '\n%s\n' "$*" >&2; }

# The commands a person can copy. Two ways, because both exist on macOS.
how_to_get_python() {
    cat <<'EOF'

RIPPLE needs Python 3.9 or newer, and this Mac has none it can use.

Two ways to get it — copy one line into Terminal, press Enter:

  xcode-select --install          # Apple's Command Line Tools, ~1.5 GB, brings python3
  brew install python@3.13        # Homebrew, smaller and faster, if you have brew

After that, run ./install.sh again.
EOF
}

# Find an interpreter: python3 first, then the versioned names, because a
# Homebrew python may be installed without the plain `python3` symlink.
PYTHON=""
for candidate in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    fail "install.sh: no python3 found on this Mac."
    how_to_get_python
    exit 1
fi

# The version, asked of the interpreter itself — `--version` output differs
# between versions, so it is parsed rather than trusted.
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    fail "install.sh: $PYTHON is $(command -v "$PYTHON") but older than 3.9."
    how_to_get_python
    exit 1
fi

VERSION="$("$PYTHON" -c 'import platform; print(platform.python_version())')"
say "python: $VERSION ($(command -v "$PYTHON"))"

if [ "$CHECK_ONLY" = "1" ]; then
    say "check only: nothing was written."
    exit 0
fi

# A virtual environment keeps the install out of the system Python; `.venv` is
# the name the rest of the documentation uses.
if [ ! -x .venv/bin/python ]; then
    say "creating .venv …"
    if ! "$PYTHON" -m venv .venv; then
        fail "install.sh: could not create .venv — is python3 able to write here?"
        exit 1
    fi
fi

say "installing …"
# An old venv ships pip 21 and setuptools 58; a plain `pip install .` still
# works there, but upgrading first makes the build quiet and reproducible.
./.venv/bin/python -m pip install -q -U pip setuptools >/dev/null 2>&1 || true
if ! ./.venv/bin/python -m pip install -q .; then
    fail "install.sh: pip could not install RIPPLE — the output above names the reason."
    exit 1
fi

say ""
say "RIPPLE is installed: ./.venv/bin/ripple"
say ""

# The next step is a question, not an instruction: someone who double-clicked
# this file should not have to read the documentation to get further.
if [ -t 0 ]; then
    printf 'Configure it now (ripple init)? [Y/n] '
    read -r answer
    case "${answer:-y}" in
        [nN]*) say "later: ./.venv/bin/ripple init"; exit 0 ;;
    esac
    exec ./.venv/bin/ripple init
fi

say "next: ./.venv/bin/ripple init"
