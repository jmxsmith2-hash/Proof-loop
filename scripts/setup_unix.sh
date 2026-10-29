#!/usr/bin/env sh
# One-time setup on macOS/Linux:  sh scripts/setup_unix.sh
# Installs ProofLoop (synthux, ClearLoop and the window that shows both) into one .venv here.
set -e
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ needed"'
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e "packages/synthux[dev]" -e "packages/clearloop[dev]" -e "packages/proofloop[dev]"
# Chromium for website tests downloads by itself the first time you test a website.
CFG=packages/synthux/config
[ -f $CFG/settings.yaml ] || cp $CFG/settings.example.yaml $CFG/settings.yaml
.venv/bin/synthux check
[ -n "$NO_AUTOSTART" ] || .venv/bin/proofloop autostart on
.venv/bin/proofloop doctor || true
echo "Done. Open ProofLoop with: .venv/bin/proofloop  (it keeps running when you close the window; it starts"
echo "when you log in unless you ran this with NO_AUTOSTART=1). Update later with: .venv/bin/proofloop update"
echo "(or activate with: . .venv/bin/activate)"
