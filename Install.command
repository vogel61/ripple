#!/usr/bin/env bash
# Double-click this file in the Finder: macOS opens it in Terminal and runs it.
# It is only a wrapper — the work is in install.sh, which is also the file the
# documentation tells people to run.
cd "$(dirname "$0")" || exit 1
exec bash ./install.sh
