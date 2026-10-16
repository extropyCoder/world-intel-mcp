#!/usr/bin/env bash
# Run on the headless dashboard machine. All state is disposable and private.
# Missing Debian/Ubuntu venv support is offered interactively, default NO.
# Never run this repository script with sudo; only the approved system apt-get
# command is elevated by the helper when necessary.
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="${SMOKE_PYTHON:-python3}"
if ! command -v -- "$PYTHON" >/dev/null 2>&1; then
    echo "Python not found. Install Python >=3.11 with venv support, or set SMOKE_PYTHON to its executable path." >&2
    exit 1
fi
exec "$PYTHON" -I "$ROOT/scripts/smoke_test.py" "$@"
