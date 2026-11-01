# Development

```bash
pip install -e ".[all,dev]" -c constraints.txt
ruff check src tests tools examples
mypy src                      # types: every module is checked
pytest -q                     # everything, a few minutes
pytest -q --cov=proofloop     # and how much of the code the tests run
pytest -q --ignore=tests/e2e  # without the CAD examples end to end
npm ci && npm run lint        # the page scripts (static/js and the scripts inside the pages)
pip install playwright && python -m playwright install chromium
pytest -m browser             # the window in a real browser, against `proofloop try` (not part of plain pytest)
```

The tests never touch your real `~/.proofloop`: every test gets an empty folder of its own
(`tests/conftest.py`). The code is laid out in [architecture.md](architecture.md).

| Folder | Tests |
|---|---|
| `tests/core`, `tests/personas` | settings and paths, the database, models and cost, notifications, persona generation |
| `tests/check` | CAD checks, the print-ready verdict (with a stand-in slicer, `tests/fixtures/fake_slicer.py`), G-code, learning, the `check` commands |
| `tests/loopfiles` | making, checking and costing loop files on synthetic plates that mirror real P2S slices (`tests/plates.py`) |
| `tests/monitor` | the monitor's rule, the plate check, the journal, filament, sending, the queue; and end to end against a pretend printer, the summary |
| `tests/app` | the window, the background service, phone pairing, plates handed between tabs, Check and Loop files & Printers on the window's server, backup, the MCP server (also with the official MCP client when it's installed) |
| `tests/test_json_output.py` | `--json` and the exit codes of every command |
| `tests/e2e` | the CAD examples end to end: every planted flaw found, the fixed version and the control part clean |

**The pretend printer** (`proofloop.monitor.printer.pretend`) speaks the Bambu LAN protocol on `127.0.0.1`:
MQTT over TLS, FTPS with TLS from the first byte, and a camera picture over HTTP, each on a free
port. `tests/monitor/test_pretend_printer.py` runs a queued job through it over real sockets: the
file is copied and started, each loop end's plate is checked with the camera, an empty plate is
resumed and one with parts left is not, and the job is ticked off when the printer finishes. It
also prints by itself for `proofloop try`, walking the file's G-code.

Check the loop files against the real public FarmLoop P2S files too:

```bash
git clone https://github.com/Liaminamerica2/p2s-autofarm-playbook /tmp/pb
PROOFLOOP_EVIDENCE=/tmp/pb/evidence pytest -q tests/loopfiles/test_evidence.py
python tools/build_builtin_profiles.py /tmp/pb/evidence   # rebuilds the built-in P2S profiles byte for byte
```

`python examples/cad/make_examples.py` and `python examples/gcode/make_examples.py` regenerate the
example parts and G-code.

## Versions

`constraints.txt` pins every package to the version CI tested, for every platform at once
(`uv pip compile pyproject.toml --extra all --universal --python-version 3.11 -o constraints.txt`).
`setup.bat`, `setup_unix.sh`, `proofloop update` and Docker install with it, so a new release of a
library can't change an install overnight. Dependabot opens a pull request each week that moves the
pins (and the CI actions) forward; CI tests it before it can be merged.

## Releases

Each version has a section in `CHANGELOG.md`, newest first (`## 0.4.0 · 2026-10-30`). To release:
set the version in `pyproject.toml` and `src/proofloop/__init__.py`, write its section, merge, then
push a tag `v0.4.0`. The release workflow checks that the tag, the version and the changelog agree,
runs the tests, builds the package and the Windows installer, and makes the GitHub release with that
section as its notes, the package, `ProofLoop-Setup-0.4.0.exe` and `SHA256SUMS.txt` attached.
`proofloop update` shows what an update brings before it installs it, and the window shows the
newest sections under *More › What's new* (the version badge lights up after an update).

### The Windows installer

`packaging/windows/proofloop.spec` has PyInstaller bundle ProofLoop and Python into one folder with
`proofloop.exe` (the command line) and `ProofLoopApp.exe` (opens the window, no console; the
shortcuts use it), and `packaging/windows/proofloop.iss` (Inno Setup) wraps that folder in an installer for
the current user, with no admin rights. To build it by hand on Windows:

```bash
pip install ".[all]" pyinstaller -c constraints.txt
pyinstaller packaging/windows/proofloop.spec --noconfirm --distpath dist --workpath build/pyinstaller
ISCC /DAppVersion=0.4.0 /DIconFile=build\pyinstaller\proofloop.ico packaging\windows\proofloop.iss
```

`dist/ProofLoop/` runs as it is; the installer is `dist/installer/ProofLoop-Setup-0.4.0.exe`. The
release job checks the folder (`--version`, `doctor`, a G-code check and a loop-file check), then
installs the installer, runs it and uninstalls it. A new package that is imported only inside a function
may need adding to `hiddenimports` in the spec; `proofloop.exe` fails with `No module named ...` when it does.
The installer isn't code-signed, so SmartScreen warns the first time. To sign it, get a code-signing
certificate (or Azure Trusted Signing) and add a `signtool` step before the upload, and a
`SignTool=` line to the `.iss`.

### PyPI

`publish.yml` builds the package once and uploads it to PyPI with
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): GitHub proves to PyPI which workflow is
running, so no API token is stored in the repository. It runs when `release.yml` has made a release (only
if the repository variable `PUBLISH_TO_PYPI` is `true`), and from the Actions tab with **Run workflow**:
`testpypi` (the default) to try it, or `pypi`. Once, as the owner:

1. Check that the name is free: `https://pypi.org/project/proofloop/` must be a 404. (It is taken by
   an unrelated project, so choose another `name` in `pyproject.toml` first, and change the
   `url:` lines in `publish.yml` to match. The command stays `proofloop`.)
2. On pypi.org: your account, *Publishing*, *Add a new pending publisher*. Project name as in
   `pyproject.toml`, owner `jmxsmith2-hash`, repository `Proof-loop`, workflow `publish.yml`,
   environment `pypi`. A pending publisher creates the project on its first upload.
3. The same on test.pypi.org with environment `testpypi`, to try it first.
4. On GitHub: *Settings › Environments*, make `pypi` (and `testpypi`). On `pypi`, add yourself as a
   required reviewer if you want to approve each upload.
5. Run **publish** with `testpypi`, then `pip install -i https://test.pypi.org/simple/ <name>` in a clean
   environment. When that works, add the repository variable `PUBLISH_TO_PYPI` = `true` (*Settings ›
   Secrets and variables › Actions › Variables*): every later release goes to PyPI by itself.

A PyPI version can't be replaced or reused, so a bad release is fixed by releasing the next version.

## CI

CI runs once per change: on pull requests and on pushes to `main` (a newer push to a pull request
cancels the run it replaces). Start it by hand from the Actions tab with **Run workflow**.

- **Tests** on Python 3.11 to 3.13 with the plain install (what Docker runs), with ruff and mypy.
- **JS lint**: ESLint over the page scripts (`eslint.config.mjs` knows the names the files share).
- **Browser**: `pytest -m browser` drives the window, Loop files, Printers and Check in Chromium.
- **Evidence**: the built-in P2S profile against the public FarmLoop files it was learned from.
- **Everything** on Windows and Linux: every test, with `[all]` as setup installs it (on Linux with
  the coverage report in the run's summary); a UTF-8
  check, because Windows reads text in another encoding by default; the command line, run from the
  repository root.
- **`setup.bat` on a clean Windows machine**, once with Python and once without (the uv path). It
  checks the shortcut, start at login and the setup check, then starts ProofLoop in the background
  the way the shortcut does and stops it again.
- **A real slicer:** OrcaSlicer and Bambu Studio are downloaded onto Windows. The setup check
  slices a test part, then `proofloop check prepare` goes from STL to a print-ready verdict, with a
  loop file and with the slicer-setting fixes applied.

## History

ProofLoop joined two earlier projects: synthux (design checks, and website tests, since removed)
and ClearLoop (loop files and the printer monitor). Their history is kept: `git log --follow` on a
file shows its commits from before the move.
