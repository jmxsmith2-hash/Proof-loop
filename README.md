# ProofLoop

**Proof your designs. Loop your prints.** One app for designing, checking and printing physical
products on Bambu Lab printers: made for the P2S, with H-series profiles. It is free and runs on
your own computer.

ProofLoop opens as one window with two tabs:

| Tab | Inside it | What it does |
|---|---|---|
| **Check & test** | [synthux](packages/synthux/README.md) | Checks CAD files (STL, 3MF, OBJ or STEP from Fusion, Blender or Tinkercad) and sliced G-code before you print. It can slice headlessly to give a print-ready verdict, turns each problem into the slicer setting that fixes it, checks parts for moulding and CNC, and runs synthetic user tests of websites. |
| **Loop files & printers** | [ClearLoop](packages/clearloop/README.md) | Makes loop files for FarmLoop auto-eject hardware (a push height for each part, a cooldown that's safe), checks them, and watches your printers: plate check, print history, filament and costs. |

### How the two parts work together

- **Plates move between the tabs.**
  - A plate checked in *Check & test* has a **Fine-tune in ClearLoop** button. It opens the plate in *Make a loop file*, where you set push heights for each part, cooldown, costs and batches.
  - A plate dropped in *Loop files & printers* has a **Full print check in synthux** button. That check includes layer previews, flow, temperatures and the slicer settings that fix each problem.
- **Quick loop files:** **Make a loop file** on a checked plate makes, checks and downloads a loop file in one step.
- **Auto-eject check:** `--auto-eject`, or the *Printed in a FarmLoop loop* box, checks whether a part will eject cleanly with ClearLoop's sweep, using your calibrated floor and push depth.
- **Learning from your printers:** synthux learns from ClearLoop's print history which flows, first layers and files fail on each of your printers.
- **Checking loop files:** a loop file gets both tools' checks. ClearLoop checks the loop moves (cooldown, sweep) and synthux checks the print inside each loop.
- **One place for notifications:** the ntfy, Discord or webhook targets you set up once in ClearLoop are used by both tools. The ProofLoop window also shows a desktop notice when a long check finishes while it's in the background.

## Install

**Windows:** double-click **`setup.bat`**.
- You don't need Python first. If it isn't installed, setup fetches [uv](https://docs.astral.sh/uv/) (installed for your user only, no admin rights) and a Python 3.12 just for this folder.
- It installs everything into `.venv` here, downloads Chromium, checks the setup and puts a **ProofLoop** shortcut on the desktop. `ProofLoop.bat` in this folder does the same as the shortcut.
- Options for `scripts\setup_windows.ps1`:
  - `-NoClearLoop` installs synthux only.
  - `-NoShortcuts` skips the desktop shortcut.
  - `-ForceUv` uses uv even when Python is installed.

**macOS / Linux:** run `sh scripts/setup_unix.sh`, then `.venv/bin/proofloop`.

**By hand**, into any environment:

```bash
pip install -e packages/synthux -e packages/clearloop -e packages/proofloop
python -m playwright install chromium
proofloop
```

## Command line

```bash
proofloop                                   # open the ProofLoop window (same as: proofloop app)
proofloop check part.stl --design part.yaml # check a CAD file (options after the file go to synthux)
proofloop check plate.gcode.3mf             # check sliced G-code; loop files get ClearLoop's checks too
proofloop loop plate.gcode.3mf --loops 5    # make a loop file (ClearLoop's make)
proofloop synthux physical learn            # any synthux command
proofloop clearloop history                 # any ClearLoop command
```

`synthux` and `clearloop` still work as commands on their own, and so do their apps (`synthux app`,
`clearloop app`). `clearloop app --host 0.0.0.0` is how you open ClearLoop from a phone on your
network, and ClearLoop also runs in Docker (`cd packages/clearloop && docker compose up -d`).
Run only one of ProofLoop and a separate ClearLoop at a time, so your printers are watched once.

## Layout

```
setup.bat, ProofLoop.bat     Windows: set up once, then open ProofLoop
scripts/                     setup for Windows (setup_windows.ps1) and macOS/Linux
packages/proofloop/          the ProofLoop window and command (small: it joins the two below)
packages/synthux/            synthux: src/, tests/, config/, examples/, studies/
packages/clearloop/          ClearLoop: src/, tests/, docs/, tools/, Dockerfile
.github/workflows/ci.yml     one CI for all three
```

**How the packages fit together:**
- Each package has its own `pyproject.toml`, version, tests and README, and synthux and ClearLoop can each be installed alone.
- Neither lists the other as a dependency. Each imports the other only where a feature needs it, and carries on without it when it isn't installed.
- ProofLoop runs both in one process:
  - ClearLoop is served from the window's own address.
  - synthux runs on a private port and allows only the ProofLoop window to show it in a frame.
  - Plates go from one to the other through two endpoints that only the window may call.
  - Everything listens on this computer only (`127.0.0.1`), and requests that use any other host name are refused.

The history of the two original repositories (Something-C for synthux, Something-B for ClearLoop)
is kept: `git log -- packages/synthux` and `git log -- packages/clearloop` show each one's commits.

## Development

```bash
cd packages/proofloop && pytest -q && ruff check src tests
cd packages/clearloop && pytest -q && ruff check src tests tools
cd packages/synthux   && pytest tests/unit -q && ruff check --select F,E9 src tests
```

CI runs:
- **ClearLoop alone** on Python 3.11 to 3.13, with ruff. It also re-checks the built-in P2S profile against the public FarmLoop files it was learned from.
- **synthux alone** on Python 3.11. Everything except loop files must work without ClearLoop.
- **Everything together** on Windows and Linux:
  - the ProofLoop tests, including handing plates both ways;
  - all ClearLoop tests;
  - all synthux unit, browser and end-to-end tests;
  - a UTF-8 check, because Windows reads text in another encoding by default;
  - the command line, run from the repository root.
- **`setup.bat` on a clean Windows machine**, once with Python and once without (the uv path). It checks the commands, the desktop shortcut, and that the ProofLoop window starts with both tabs.

## License

ClearLoop is AGPL-3.0-or-later ([packages/clearloop/LICENSE](packages/clearloop/LICENSE)).
synthux and the ProofLoop window have no license file yet, so all rights are reserved until one is
added. If you publish ProofLoop, licensing it AGPL-3.0 too keeps it simple.
