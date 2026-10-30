# Development

```bash
pip install -e ".[all,dev]"
ruff check src tests tools examples
pytest -q                     # everything, a few minutes
pytest -q --ignore=tests/e2e  # without the CAD examples end to end
```

The tests never touch your real `~/.proofloop`: every test gets an empty folder of its own
(`tests/conftest.py`). The code is laid out in [architecture.md](architecture.md).

| Folder | Tests |
|---|---|
| `tests/core`, `tests/personas` | settings and paths, the database, models and cost, notifications, persona generation |
| `tests/check` | CAD checks, the print-ready verdict (with a stand-in slicer, `tests/fixtures/fake_slicer.py`), G-code, learning, the `check` commands |
| `tests/loopfiles` | making, checking and costing loop files on synthetic plates that mirror real P2S slices (`tests/plates.py`) |
| `tests/monitor` | the monitor's rule, the plate check, the journal, filament, sending, the queue; and end to end against a pretend printer (`tests/fake_printer.py`) |
| `tests/app` | the window, the background service, phone pairing, plates handed between tabs, Check and Loop files & Printers on the window's server |
| `tests/e2e` | the CAD examples end to end: every planted flaw found, the fixed version and the control part clean |

**The pretend printer** (`tests/fake_printer.py`) speaks the Bambu LAN protocol on `127.0.0.1`:
MQTT over TLS, FTPS with TLS from the first byte, and a camera picture over HTTP, each on a free
port. `tests/monitor/test_pretend_printer.py` runs a queued job through it over real sockets: the
file is copied and started, each loop end's plate is checked with the camera, an empty plate is
resumed and one with parts left is not, and the job is ticked off when the printer finishes. It
needs `cryptography` (in `[dev]`) for the printer's certificate.

Check the loop files against the real public FarmLoop P2S files too:

```bash
git clone https://github.com/Liaminamerica2/p2s-autofarm-playbook /tmp/pb
PROOFLOOP_EVIDENCE=/tmp/pb/evidence pytest -q tests/loopfiles/test_evidence.py
python tools/build_builtin_profiles.py /tmp/pb/evidence   # rebuilds the built-in P2S profiles byte for byte
```

`python examples/cad/make_examples.py` and `python examples/gcode/make_examples.py` regenerate the
example parts and G-code.

## CI

CI runs once per change: on pull requests and on pushes to `main` (a newer push to a pull request
cancels the run it replaces). Start it by hand from the Actions tab with **Run workflow**.

- **Tests** on Python 3.11 to 3.13 with the plain install (what Docker runs), with ruff.
- **Evidence**: the built-in P2S profile against the public FarmLoop files it was learned from.
- **Everything** on Windows and Linux: every test, with `[all]` as setup installs it; a UTF-8
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
