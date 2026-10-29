# Development

```bash
pip install -e ".[all,dev]"
ruff check src tests tools examples
pytest -q --ignore=tests/browser --ignore=tests/e2e     # everything but the browser, a few minutes
pytest -q tests/browser                                 # real Chromium against the demo shop (~3 min)
pytest -q tests/e2e                                     # whole website-test pipeline on both demo sites,
                                                        # and the CAD examples (~10 min)
PROOFLOOP_LIVE=1 ANTHROPIC_API_KEY=... pytest tests/e2e/test_demo_app_live.py   # the same bar on Claude (costs money)
```

The tests never touch your real `~/.proofloop`: every test gets an empty folder of its own
(`tests/conftest.py`). The code is laid out in [architecture.md](architecture.md).

| Folder | Tests |
|---|---|
| `tests/core`, `tests/personas` | settings and paths, the database, models and cost, notifications, persona generation |
| `tests/check` | CAD checks, the print-ready verdict (with a stand-in slicer, `tests/fixtures/fake_slicer.py`), G-code, learning |
| `tests/loopfiles` | making, checking and costing loop files on synthetic plates that mirror real P2S slices (`tests/plates.py`) |
| `tests/monitor` | the monitor's rule, the plate check, the journal, filament |
| `tests/app` | the window, the background service, phone pairing, plates handed between tabs, the two page servers |
| `tests/web`, `tests/cli` | website tests: runner, judge, aggregation, calibration, safety, report, dashboard, commands |
| `tests/browser`, `tests/e2e` | real Chromium; the end-to-end runs |

The website-test end-to-end runs use the demo sites in `src/proofloop/web/demo`. The demo shop
plants a hidden button, a confusing form, a slow page and an error with no explanation, next to two
clean control flows; the test requires every planted flaw to be found and **no promoted finding
that isn't planted**. Pinecone Home plants a newsletter pop-up that comes back on every page, a
return policy buried three pages deep and a menu link that does nothing, next to a cookie banner
that stays closed and a sign-up form with a dropdown and a terms checkbox.

Check the loop files against the real public FarmLoop P2S files too:

```bash
git clone https://github.com/Liaminamerica2/p2s-autofarm-playbook /tmp/pb
PROOFLOOP_EVIDENCE=/tmp/pb/evidence pytest -q tests/loopfiles/test_evidence.py
python tools/build_builtin_profiles.py /tmp/pb/evidence   # rebuilds the built-in P2S profiles byte for byte
```

`python examples/cad/make_examples.py` and `python examples/gcode/make_examples.py` regenerate the
example parts and G-code.

## CI

- **Light install** on Python 3.11 to 3.13: everything but website tests must work without
  Playwright or the on-screen text reader (that's what Docker runs), with ruff.
- **Evidence**: the built-in P2S profile against the public FarmLoop files it was learned from.
- **Everything** on Windows and Linux: every test, including the browser and end-to-end ones; a
  UTF-8 check, because Windows reads text in another encoding by default; the command line, run
  from the repository root.
- **`setup.bat` on a clean Windows machine**, once with Python and once without (the uv path). It
  checks the shortcut, start at login and the setup check, then starts ProofLoop in the background
  the way the shortcut does and stops it again.
- **A real slicer:** OrcaSlicer and Bambu Studio are downloaded onto Windows. The setup check
  slices a test part, then `proofloop check prepare` goes from STL to a print-ready verdict, with a
  loop file and with the slicer-setting fixes applied.

## History

ProofLoop joined two earlier projects: synthux (design checks and website tests) and ClearLoop
(loop files and the printer monitor). Their history is kept: `git log --follow` on a file shows its
commits from before the move.
