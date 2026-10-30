# ProofLoop

**Proof your designs. Loop your prints.** One app for designing, checking and printing physical
products on Bambu Lab printers, made for the P2S, with H-series profiles. It is free and runs on
your own computer.

ProofLoop opens as one window with three tabs:

| Tab | What it does |
|---|---|
| **Check** | Checks CAD files (STL, 3MF, OBJ or STEP from Fusion, Blender or Tinkercad) and sliced G-code before you print. It can slice headlessly for a print-ready verdict, turns each problem into the slicer setting that fixes it, and checks parts for moulding and CNC. [More](docs/check.md) |
| **Loop files** | Makes loop files for FarmLoop auto-eject hardware: a push height for each part, a cooldown that's safe, a first-sweep safety pause. Checks any loop file, costs a plate, and makes calibration prints. [More](docs/loop-files.md) |
| **Printers** | Watches your printers: live status, a camera check that the plate is empty before the next loop, print history, filament and spools. [More](docs/printers.md) |

Under **More** there are **Get set up**, **Phone** and **Setup check**.

## Getting set up

The first time you open ProofLoop, a **Get set up** panel lists what to do before your printers
run unattended: add the printer, connect to it, take pictures of the empty plate, pick the hardware
for your kit, measure the sweep floor, set up alerts and start at login (and, optionally, install
Bambu Studio for print-ready verdicts). Each step ticks itself off as you do it, and **Show me**
opens the right tab and outlines the exact form. Open it again any time from *More › Get set up*.

## How the tabs work together

- **Plates move between tabs.** A plate checked under *Check* has **Make it now** (a loop file with
  your saved settings, checked and ready to download) and **Fine-tune in Loop files**, which opens
  it in *Make a loop file* to set push heights per part, cooldown, costs and batches. A plate in
  *Make a loop file* has **Full print check**, which sends it back to *Check*.
- **One list of printers.** The printers you add under *Printers › Monitor* are the ones designs
  are checked for, listed first.
- **Learning from your printers.** *Check* reads the print history and learns, per printer and
  filament, which flows, first layers and files fail on your machines.
- **Learning from you.** Mark a finding *Not a problem*, and after two such marks that check counts
  one level lower for that printer and material. *It was right* undoes it.
- **One place for notifications.** The ntfy, Discord or webhook targets you set up under
  *Printers › Monitor* are used for everything: printer alerts, long checks that have finished, and
  a printer coming free for its next queued job. The window also shows a desktop notice when a check
  finishes; it asks once, with a button.
- **From plate to printers.** *Plan a production run* spreads the loops you need over the printers
  that can print the plate, so they finish together, and can keep that as a queue under *Printers ›
  Overview*. With **sending to printers** turned on (off unless you turn it on), a loop file goes to
  a printer and starts in one click, after you confirm the printer, and only when the printer is
  connected, idle, the right model and has the right filament loaded. Nothing in the queue starts by
  itself. [More](docs/printers.md#queue)

## Runs in the background

Closing the window doesn't stop ProofLoop. It keeps watching your printers, with an icon next to
the clock, and starts again when you log in, so a Windows update overnight doesn't leave the farm
unwatched. It tells you through your notification targets when it stops (with the printers still
printing) and when it starts again after an unexpected stop, with how long the printers went
unwatched. The icon's menu has: open the window, phone access, *Start when I log in*, *Check my
setup* and *Quit*.

## Safety for a new setup

- **Safety pause:** a loop file pauses before its first sweep, after the parts have cooled and
  before the pusher moves, so you can check the push heights against your parts. Press Resume on
  the printer to carry on. This stays on until a loop has swept cleanly on one of your printers.
- **Setup check** (*More › Setup check*, or `proofloop doctor`) checks the slicer and its presets
  for your printer; each printer (reachable, hardware profile, pictures of the empty plate); sweep
  floor calibration, notifications, start at login and phone access. `proofloop doctor --slice-test
  --send-test` also slices a test part with your real slicer and sends a test notice.

## On your phone

*More › Phone* shows *Loop files* and *Printers* on your phone over your own Wi-Fi: printer status,
plate checks, filament and costs. Scan the QR code, or type the six-digit code. Nothing goes through
the internet, only paired phones can see it, and *Check* and the window's settings are never shown
on the phone. *Forget phones* unpairs them all. Windows may ask whether Python can
use the network: choose *Private networks*.

## Install

**Windows:** double-click **`setup.bat`**.
- You don't need Python first. If it isn't installed, setup fetches [uv](https://docs.astral.sh/uv/)
  (installed for your user only, no admin rights) and a Python 3.12 just for this folder.
- It installs ProofLoop into `.venv` here, then puts a **ProofLoop** shortcut on the desktop, sets
  ProofLoop to start in the background when you log in, and runs the setup check.
- Options for `scripts\setup_windows.ps1`: `-NoShortcuts`, `-NoAutostart`, `-ForceUv` (use uv even
  when Python is installed).
- Windows SmartScreen or Defender may ask about the setup script, or about uv when it downloads.
  Both are run from this folder and are expected.

**macOS / Linux:** `sh scripts/setup_unix.sh` (set `NO_AUTOSTART=1` to skip start at login), then
`.venv/bin/proofloop`.

**By hand**, into any environment: `pip install -e ".[all]"`, then `proofloop`. Without `[all]`
you get everything except reading STEP files directly.

**Docker**, for an always-on machine that watches the printers: `docker compose up -d` serves
*Loop files* and *Printers* at `http://<host>:8484`, with your data in `./proofloop-data`. Run only
one ProofLoop per printer farm, so your printers are watched once.

**Updating:** double-click **`Update.bat`**, or run `proofloop update`. It fetches the newest
version from GitHub, installs it and restarts ProofLoop if it was running. Your settings and data are kept.

## Command line

```bash
proofloop                                    # open the window (starts ProofLoop in the background if needed)
proofloop check part.stl --design part.yaml  # check a CAD file
proofloop check plate.gcode.3mf              # check sliced G-code; loop files get the loop-file checks too
proofloop check prepare part.stl --loops 5   # design check, slice, G-code check, and a loop file
proofloop loop plate.gcode.3mf --loops 5     # make a loop file (--first-pause on|off|auto)
proofloop loop check FL_S2_plate.gcode.3mf   # check any loop file
proofloop printers history                   # how recent prints ended
proofloop doctor [--slice-test --send-test]  # check the setup
proofloop stop | status                      # quit ProofLoop / is it running?
proofloop autostart on|off                   # start in the background when you log in
proofloop phone on|off|forget                # phone access (prints the code and a QR code)
proofloop update [--check]                   # get the newest ProofLoop
proofloop app                                # run in this terminal instead of the background, until Ctrl+C
```

Each part has more: `proofloop check --help`, `proofloop loop --help`, `proofloop printers --help`.

## Where things are kept

Everything is in one folder, `~/.proofloop` (`PROOFLOOP_HOME` moves it):

- `settings.json`: what you set in the window (printers, notifications, loop-file defaults, costs, filament);
- `config/`: optional YAML for advanced options. A file here replaces the built-in one of the same
  name (see `src/proofloop/data`), and `settings.yaml` is merged over the built-in settings, so it
  only needs what you change;
- `printers.db` and `results.db`: print history and events; design checks and what was learned;
- `reports/`, `profiles/`, `files/`, `queue/`: reports, imported hardware profiles, the loop files
  ProofLoop made, queued runs;
- `proofloop.log`: the background service's log.

## More

- [Check](docs/check.md): CAD, the print-ready verdict, G-code, moulding and machining, product predictions
- [Loop files](docs/loop-files.md): what goes in a loop file, checking, costs, calibration, hardware profiles
- [Printers](docs/printers.md): the plate check, print history, filament, notifications, Docker
- [Printer setup](docs/bambu-setup.md), [where every printer and kit value comes from](docs/sources.md)
- [Architecture](docs/architecture.md) and [development](docs/development.md)

## License

AGPL-3.0-or-later ([LICENSE](LICENSE)). Camera handling follows
[Bambuddy](https://github.com/maziggy/bambuddy) (AGPL-3.0). "Bambu Lab", "P2S" and "FarmLoop" are
trademarks of their owners; ProofLoop is not affiliated with Bambu Lab or 3D Farmers. FarmLoop
hardware moves in the built-in profiles are reproduced from public files for interoperability,
with their source cited.
