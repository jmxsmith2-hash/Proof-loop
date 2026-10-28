# synthux + ClearLoop

Two tools for designing, testing and printing physical products on Bambu Lab printers (made for
the P2S, with H-series profiles), kept in one repository so they are always in step:

| | What it does |
|---|---|
| **[synthux](packages/synthux/README.md)** | Checks CAD files (Fusion, Blender, Tinkercad exports: STL, 3MF, OBJ, STEP) and sliced G-code before you print, slices headlessly for a print-ready verdict, turns problems into slicer settings, checks parts for moulding and CNC, and runs synthetic user tests of websites. Free and offline by default. |
| **[ClearLoop](packages/clearloop/README.md)** | Makes loop files for FarmLoop auto-eject hardware (per-part push heights, safe cooldown), checks them, watches your printers (plate check, print history, filament, notifications). |

They work on their own, and better together. Installed side by side:

* synthux checks ClearLoop's loop files with ClearLoop's own checks, and its app window gets a
  **Make a loop file** button for any checked Bambu plate;
* synthux checks whether a part will eject cleanly with ClearLoop's sweep (`--auto-eject`), using
  your calibrated floor and push depth;
* synthux learns from ClearLoop's print history which flows, first layers and files fail on each
  of your printers (`synthux physical learn`);
* ClearLoop's *Check a file* also runs synthux's print checks on the print inside each loop;
* both send "done" and "failed" notifications to the same ntfy / Discord / webhook targets.

## Install

**Windows:** double-click **`setup.bat`**. No Python needed beforehand: if none is installed it
fetches [uv](https://docs.astral.sh/uv/) (for your user only, no admin rights) and a Python 3.12
for this folder. It installs both tools into `.venv` here, downloads Chromium, checks the setup
and adds **Synthux** and **ClearLoop** shortcuts to the desktop (also `Synthux.bat` and
`ClearLoop.bat` here). Switches for `scripts\setup_windows.ps1`: `-NoClearLoop`, `-NoShortcuts`,
`-ForceUv`.

**macOS / Linux:** `sh scripts/setup_unix.sh`, then `.venv/bin/synthux app` or `.venv/bin/clearloop app`.

**By hand**, into any environment:

```bash
pip install -e "packages/synthux[dev]" -e "packages/clearloop[dev]"
python -m playwright install chromium
```

Or one of them straight from GitHub (you must be signed in to GitHub while the repository is private):

```bash
pip install "synthux @ git+https://github.com/jmxsmith2-hash/synthux-clearloop#subdirectory=packages/synthux"
pip install "clearloop @ git+https://github.com/jmxsmith2-hash/synthux-clearloop#subdirectory=packages/clearloop"
```

ClearLoop also runs in Docker: `cd packages/clearloop && docker compose up -d`.

## Layout

```
setup.bat, Synthux.bat, ClearLoop.bat   Windows: set up once, then open either app
scripts/                                setup for Windows (setup_windows.ps1) and macOS/Linux
packages/synthux/                       the synthux package: src/, tests/, config/, examples/, studies/
packages/clearloop/                     the ClearLoop package: src/, tests/, docs/, tools/, Dockerfile
.github/workflows/ci.yml                one CI for both
```

Each package keeps its own `pyproject.toml`, version, tests and README, and can be installed
alone. Neither lists the other as a dependency: each imports the other only where a feature
needs it, inside a guarded import, and carries on without it when it isn't installed.

The history of both original repositories (Something-C for synthux, Something-B for ClearLoop) is
kept: `git log -- packages/synthux` and `git log -- packages/clearloop` show each one's commits.

## Development

```bash
cd packages/clearloop && pytest -q && ruff check src tests tools
cd packages/synthux   && pytest tests/unit -q && ruff check --select F,E9 src tests
```

CI runs:

* ClearLoop alone on Python 3.11 to 3.13, with ruff, plus a check of its built-in P2S profile
  against the public FarmLoop files it was learned from;
* synthux alone on Python 3.11 (everything but loop files must work without ClearLoop);
* both together on Windows and Linux: all ClearLoop tests, all synthux unit, browser and
  end-to-end tests, a UTF-8 check (Windows reads text in another encoding by default), and the
  command line run from the repository root;
* `setup.bat` on a clean Windows machine, once with Python and once without (the uv path),
  checking that both tools and both desktop shortcuts work.

## License

ClearLoop is AGPL-3.0-or-later ([packages/clearloop/LICENSE](packages/clearloop/LICENSE)).
synthux has no license file yet, so all rights are reserved until one is added.
