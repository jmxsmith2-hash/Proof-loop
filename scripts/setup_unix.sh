#!/usr/bin/env sh
# One-time setup on macOS/Linux:  sh scripts/setup_unix.sh
# Installs synthux and ClearLoop into one .venv here, so each can use the other.
set -e
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ needed"'
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e "packages/synthux[dev]" -e "packages/clearloop[dev]"
.venv/bin/python -m playwright install chromium
CFG=packages/synthux/config
[ -f $CFG/settings.yaml ] || cp $CFG/settings.example.yaml $CFG/settings.yaml
.venv/bin/synthux check
echo "Done. Open the apps with: .venv/bin/synthux app   or   .venv/bin/clearloop app"
echo "(or activate with: . .venv/bin/activate)"
