# ProofLoop

**Proof your designs. Loop your prints.** One app for designing, checking and printing physical
products on Bambu Lab printers: made for the P2S, with H-series profiles. It is free and runs on
your own computer.

ProofLoop opens as one window with three tabs:

| Tab | Inside it | What it does |
|---|---|---|
| **Check designs** | [synthux](packages/synthux/README.md) | Checks CAD files (STL, 3MF, OBJ or STEP from Fusion, Blender or Tinkercad) and sliced G-code before you print. It can slice headlessly to give a print-ready verdict, turns each problem into the slicer setting that fixes it, and checks parts for moulding and CNC. |
| **Loop files & printers** | [ClearLoop](packages/clearloop/README.md) | Makes loop files for FarmLoop auto-eject hardware (a push height for each part, a cooldown that's safe), checks them, and watches your printers: plate check, print history, filament and costs. |
| **Website tests** | [synthux](packages/synthux/README.md) | Synthetic user tests: personas use your website from screenshots, like people do, and you get a report with replays. |

### Runs in the background

Closing the window doesn't stop ProofLoop. It keeps watching your printers, with an icon next
to the clock, and starts again when you log in, so a Windows update overnight doesn't leave the
farm unwatched.

ProofLoop tells you through your notification targets when:
- it stops, including which printers were still printing;
- it starts again after an unexpected stop (a crash, a power cut, a restart), and how long the printers went unwatched.

The icon's menu has: open the window, phone access, *Start when I log in*, *Check my setup* and *Quit*.

### How the parts work together

- **Plates move between the tabs.**
  - A plate checked in *Check designs* has a **Fine-tune in ClearLoop** button. It opens the plate in *Make a loop file*, where you set push heights for each part, cooldown, costs and batches.
  - A plate in *Loop files & printers* has a **Full print check in synthux** button.
- **Quick loop files:** **Make it now** makes, checks and downloads a loop file in one step, with your saved ClearLoop settings (profile, sweep floor, push depth, cooldown).
- **One printer list:** the printers you add in ClearLoop's Monitor are the printers synthux checks for, listed first.
- **Learning from your printers:** synthux learns from ClearLoop's print history which flows, first layers and files fail on each printer.
- **Learning from you:** mark a finding *Not a problem*, and after two such marks that check counts one level lower for that printer and material. *It was right* undoes it.
- **One place for notifications:** the ntfy, Discord or webhook targets you set up once in ClearLoop are used for everything. The window also shows a desktop notice when a long check finishes; it asks once, with a button.

### Safety for a new setup

- **Safety pause:** a loop file pauses before its first sweep, after the parts have cooled and before the pusher moves, so you can check the push heights against your parts. Press Resume on the printer to carry on. This stays on until a loop has swept cleanly on one of your printers (*Safety pause before the first sweep* in *Make a loop file*), and the monitor knows this pause isn't the end of a loop.
- **Setup check:** **Setup check** in the window, or `proofloop doctor`, checks everything ProofLoop needs:
  - the slicer and its presets for your printer;
  - each printer: can it be reached, does it have a hardware profile and pictures of the empty plate;
  - sweep floor calibration, notifications, start at login and phone access.

  `proofloop doctor --slice-test --send-test` also slices a test part with your real slicer and sends a test notice.

### On your phone

Use the **Phone** button in the window. It shows *Loop files & printers* on your phone over your own Wi-Fi: printer status, plate checks, filament and costs. Scan the QR code, or type the six-digit code. Nothing goes through the internet, only paired phones can see it, and synthux and ProofLoop's settings are never shown on the phone. *Forget phones* unpairs them all. Windows may ask whether Python can use the network: choose *Private networks*.

## Install

**Windows:** double-click **`setup.bat`**.
- You don't need Python first. If it isn't installed, setup fetches [uv](https://docs.astral.sh/uv/) (installed for your user only, no admin rights) and a Python 3.12 just for this folder.
- It installs everything into `.venv` here, then:
  - puts a **ProofLoop** shortcut on the desktop;
  - sets ProofLoop to start in the background when you log in;
  - runs the setup check.
- Chromium for website tests isn't downloaded during setup. It downloads by itself, once (about 150 MB), the first time you test a website.
- Options for `scripts\setup_windows.ps1`:
  - `-NoClearLoop` installs synthux only.
  - `-NoShortcuts` skips the desktop shortcut.
  - `-NoAutostart` doesn't start ProofLoop at login.
  - `-WithBrowser` downloads Chromium now.
  - `-ForceUv` uses uv even when Python is installed.
- Windows SmartScreen or Defender may ask about the setup script, or about uv when it downloads. Both are run from this folder and are expected.

**Updating:** double-click **`Update.bat`**, or run `proofloop update`. It fetches the newest version from GitHub (sign in when Git asks; the repository is private), installs it and restarts ProofLoop if it was running. Your settings and data are kept.

**macOS / Linux:** run `sh scripts/setup_unix.sh` (set `NO_AUTOSTART=1` to skip start at login), then `.venv/bin/proofloop`.

**By hand**, into any environment:

```bash
pip install -e packages/synthux -e packages/clearloop -e packages/proofloop
proofloop
```

## Command line

```bash
proofloop                                   # open the window (starts ProofLoop in the background if needed)
proofloop stop | status                     # quit ProofLoop / is it running?
proofloop autostart on|off                  # start in the background when you log in
proofloop phone on|off|forget               # phone access (prints the code and a QR code)
proofloop doctor [--slice-test --send-test] # check the setup
proofloop update [--check]                  # get the newest ProofLoop
proofloop check part.stl --design part.yaml # check a CAD file (options after the file go to synthux)
proofloop check plate.gcode.3mf             # check sliced G-code; loop files get ClearLoop's checks too
proofloop loop plate.gcode.3mf --loops 5    # make a loop file (ClearLoop's make; --first-pause on|off|auto)
proofloop synthux physical learn            # any synthux command
proofloop clearloop history                 # any ClearLoop command
proofloop app                               # run in this terminal instead of the background, until Ctrl+C
```

`synthux` and `clearloop` still work as commands on their own, and ClearLoop still runs in Docker
(`cd packages/clearloop && docker compose up -d`). Run only one of ProofLoop and a separate
ClearLoop at a time, so your printers are watched once.

**Where things are kept:**
- ProofLoop's own settings and log: `~/.proofloop` (`PROOFLOOP_HOME` moves it).
- ClearLoop's settings, profiles and print history: `~/.clearloop` (`CLEARLOOP_DATA` or `--data` moves it).
- synthux's settings and data: `packages/synthux/config` and `packages/synthux/data`.

## Layout

```
setup.bat, ProofLoop.bat, Update.bat   Windows: set up once, open ProofLoop, update it
scripts/                               setup for Windows (setup_windows.ps1) and macOS/Linux
packages/proofloop/                    the window, background service, tray, phone access, setup check, updates
packages/synthux/                      synthux: src/, tests/, config/, examples/, studies/
packages/clearloop/                    ClearLoop: src/, tests/, docs/, tools/, Dockerfile
.github/workflows/ci.yml               one CI for all three
```

**How the packages fit together:**
- Each package has its own `pyproject.toml`, version, tests and README, and synthux and ClearLoop can each be installed alone.
- Neither lists the other as a dependency. Each imports the other only where a feature needs it, and carries on without it when it isn't installed.
- ProofLoop runs both in one background process. Its window answers only on this computer (`127.0.0.1`), and requests that use any other host name are refused. Phone access is a separate listener that serves ClearLoop alone, to paired phones only.

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
  - the ProofLoop tests: background service, phone pairing, handing plates both ways;
  - all ClearLoop tests, including the first-sweep safety pause;
  - all synthux unit, browser and end-to-end tests;
  - a UTF-8 check, because Windows reads text in another encoding by default;
  - the command line, run from the repository root.
- **`setup.bat` on a clean Windows machine**, once with Python and once without (the uv path). It checks the shortcut, start at login and the setup check, then starts ProofLoop in the background the way the shortcut does and stops it again.
- **A real slicer:** OrcaSlicer and Bambu Studio are downloaded onto Windows. The setup check slices a test part, then synthux goes from STL to a print-ready verdict, with a loop file and with the slicer-setting fixes applied.

## License

ClearLoop is AGPL-3.0-or-later ([packages/clearloop/LICENSE](packages/clearloop/LICENSE)).
synthux and ProofLoop have no license file yet, so all rights are reserved until one is added. If
you publish ProofLoop, licensing it AGPL-3.0 too keeps it simple.
