#!/usr/bin/env bash
# Run on the headless dashboard machine. All state is disposable and private.
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="${SMOKE_PYTHON:-python3}"
if ! command -v -- "$PYTHON" >/dev/null 2>&1; then
    echo "Python not found. Install Python >=3.11 with venv support, or set SMOKE_PYTHON to its executable path." >&2
    exit 1
fi
exec "$PYTHON" -I "$ROOT/scripts/smoke_test.py" "$@"
