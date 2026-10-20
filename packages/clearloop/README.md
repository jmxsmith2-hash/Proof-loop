# ClearLoop

**Open-source loop files for FarmLoop hardware on Bambu Lab printers, made for the P2S.**

ClearLoop replaces the FarmLoop app, not the kit. You keep your FarmLoop
Stage 1 parts or Stage 2 FarmBoard. ClearLoop writes the G-code that drives
them, with the documented problems fixed:

| FarmLoop app | ClearLoop |
|---|---|
| One push height for the whole plate (tallest part − 10 mm), so a 5 mm part next to a 170 mm part is never touched | The same rule **per part**. Every part is pushed at its own height, tallest first. |
| App 2.72 put a 2-hour "natural cooldown" **before** the print with the heaters on | Natural cooldown runs **after** the print with heaters off. Old FarmLoop files can be checked for this bug. |
| Nothing checks the plate before the next loop starts | Optional **fail-safe plate check**: the printer waits after each loop until the camera sees an empty plate |
| Subscription for Stage 2, cloud app | Free, open source, runs on your computer, no account |

Every value ClearLoop uses for a printer or kit has a public source, or is
measured on your machine with a guided test. Nothing is guessed. See
[docs/sources.md](docs/sources.md).

## Start in three minutes

```bash
pip install git+https://github.com/jmxsmith2-hash/Something-B
clearloop app
```

Your browser opens ClearLoop at `http://localhost:8484`. Or run it with Docker:
`docker compose up -d`.

1. **Make a loop file.** In Bambu Studio, slice your plate and use *File ›
   Export › Export plate sliced file*. Drop the `.gcode.3mf` into ClearLoop.
   It recognises the printer, shows every part with the height the pusher will
   reach it at, and tells you about anything it can't do safely.
2. **Download** the `FL_S2_…` file and send it the way you always do (Bambu
   Studio *Print plate*, Bambu Handy, or Bambuddy). Keep the file name:
   FarmBoard kits look for the prefix.
3. **Once per printer, calibrate.** The *Calibrate* tab turns a tiny test
   plate into a test print that measures your pusher's lowest safe height.
   Optional tests show how your bed cools and confirm the FarmBoard door and
   bender respond.

## Hardware

| Setup | Profile | Where its moves come from |
|---|---|---|
| **P2S + FarmLoop Stage 2** (FarmBoard door, bender, pusher) | built in | The FarmLoop app's own P2S output (app 2.72, FarmBoard firmware 2.94), copied verbatim. Turn off "Bender fitted" if you removed the bender. |
| **P2S + FarmLoop pusher, no FarmBoard** | built in | The same P2S sweep path. Keep the door open. |
| **Any printer, Stage 1 or Stage 2** (A1, A1 mini, P1S, X1C, H2…) | import | Your own FarmLoop file: slice a plate, process it in the FarmLoop app (the free Lite plan makes Stage 1 files), and give ClearLoop both files. It keeps your kit's moves exactly as FarmLoop wrote them and replaces only the cooldown and sweep heights. |

The P2S Stage 1 kit's own G-code hasn't been published. If you have that kit,
import one of your `FL_S1_` files to use its exact moves.

## The plate check (optional, fail-safe)

Turn on **Wait for ClearLoop Monitor between loops** when you make a file, and
add your printer in the *Monitor* tab. The printer needs LAN-only mode with
Developer Mode on ([setup guide](docs/bambu-setup.md)).

- After each sweep the printer pauses (`M400 U1`, Bambu's own pause code).
- The monitor takes a camera picture and compares it with your pictures of the
  empty plate. If it's empty, the monitor resumes the next loop.
- If parts are left, or the monitor isn't running, the printer **keeps
  waiting** with the heaters off. Clear the plate and press Resume. A crash or
  a missed message can never start a print on a dirty plate.
- Get alerts on your phone through ntfy, Discord or any webhook.

ClearLoop never uploads or starts prints. It only resumes a loop file that is
waiting for its plate check.

## Check any file before you run it

The *Check a file* tab (or `clearloop check FILE`) reads any loop file,
FarmLoop's or ClearLoop's, and reports known problems:

- a long wait before the print with the heaters on
- `M190 R`, which faults on a P2S
- parts that no sweep pass reaches
- G-code the printer will ignore
- moves after the steppers were switched off
- a bad checksum

## What ClearLoop puts in the file

1. **Start** (Stage 2): FarmLoop's FarmBoard reset, in the same place the
   FarmLoop app puts it.
2. **Your print**, untouched.
3. **After the print**, inside the printer's executable block:
   1. Steppers back on (the stock end code switches them off) and heaters off.
   2. Optional natural cooldown: door shut, fans off.
   3. FarmBoard T1: door opens, fan on.
   4. Wait until the bed reaches the release temperature (`M190 S`, as
      FarmLoop does; never `M190 R`).
   5. FarmBoard T2: bend, with FarmLoop's timing.
   6. The sweep: FarmLoop's lanes and feeds, each part pushed at its own height.
   7. Park, safety check, then T3 closes the door.
   8. Optional pause for the plate check.
4. **Loops** repeat the whole plate, the same layout the FarmLoop app uses.

## Command line

```bash
clearloop make plate.gcode.3mf --loops 5 --wait-for-monitor
clearloop make plate.gcode.3mf --profile p2s-sweep-only --floor 2.5
clearloop check FL_S2_plate.gcode.3mf
clearloop import-profile plain.gcode.3mf FL_S1_plain.gcode.3mf --name "A1 mini Stage 1"
clearloop calibrate sweep-floor tiny.gcode.3mf
clearloop profiles
```

Settings, imported profiles, empty-plate pictures and the event log live in
`~/.clearloop`. Change the location with `--data`.

## Before your first unattended run

- Watch the first loop of every new setup with a hand near the power switch.
- Run the sweep-floor test once per printer, and again after changing the pusher.
- Pin your printer firmware. Re-run the tests after an update.

## Development

```bash
pip install -e ".[dev]"
pytest -q          # synthetic plates that mirror real P2S slices
ruff check src tests tools

# Also check against the real public FarmLoop P2S files:
git clone https://github.com/Liaminamerica2/p2s-autofarm-playbook /tmp/pb
CLEARLOOP_EVIDENCE=/tmp/pb/evidence pytest -q tests/test_evidence.py
python tools/build_builtin_profiles.py /tmp/pb/evidence   # rebuilds the built-in P2S profiles byte for byte
```

The code is laid out in [docs/architecture.md](docs/architecture.md).

## License

AGPL-3.0-or-later. Camera handling follows [Bambuddy](https://github.com/maziggy/bambuddy)
(AGPL-3.0). "Bambu Lab", "P2S" and "FarmLoop" are trademarks of their owners;
ClearLoop is not affiliated with Bambu Lab or 3D Farmers. FarmLoop hardware
moves in the built-in profiles are reproduced from public files for
interoperability, with their source cited.
