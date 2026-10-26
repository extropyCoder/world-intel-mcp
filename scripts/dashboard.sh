#!/usr/bin/env bash
# Persistent loopback dashboard, separate from the disposable smoke runner.
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="${DASHBOARD_PYTHON:-python3}"
command -v -- "$PYTHON" >/dev/null 2>&1 || { echo "Select installed Python >=3.11 using DASHBOARD_PYTHON." >&2; exit 1; }
exec "$PYTHON" -I -B "$ROOT/scripts/dashboard_manager.py" "$@"
