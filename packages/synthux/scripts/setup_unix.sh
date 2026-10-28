#!/usr/bin/env sh
# One-time setup on macOS/Linux:  sh scripts/setup_unix.sh
set -e
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ needed"'
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
[ -f config/settings.yaml ] || cp config/settings.example.yaml config/settings.yaml
.venv/bin/synthux check
echo "Done. Activate with: . .venv/bin/activate"
