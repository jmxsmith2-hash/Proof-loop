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
pip install "clearloop @ git+https://github.com/jmxsmith2-hash/Proof-loop#subdirectory=packages/clearloop"
clearloop app
```

On Windows, `setup.bat` at the root of the [ProofLoop](../../README.md) repository installs
ClearLoop together with synthux and adds a **ProofLoop** desktop shortcut: ClearLoop is its
*Loop files & printers* tab, next to synthux's checks.

Your browser opens ClearLoop at `http://localhost:8484`. Or run it with Docker:
`docker compose up -d`.

1. **Make a loop file.** In Bambu Studio, slice your plate and use *File ›
   Export › Export plate sliced file*. Drop the `.gcode.3mf` into ClearLoop.
   It recognises the printer, shows every part with the height the pusher will
   reach it at, and tells you about anything it can't do safely. Every
   setting has a slider. **Push depth** sets how far below each part's top the
   pusher hits (FarmLoop always uses 10 mm), and each part has its own slider
   if one needs a different height. Nothing ever goes below your calibrated
   sweep floor, and a height that would miss a part is refused.
2. **Download** the `FL_S2_…` file and send it the way you always do (Bambu
   Studio *Print plate*, Bambu Handy, or Bambuddy). Keep the file name:
   FarmBoard kits look for the prefix.
3. **Once per printer, calibrate.** The *Calibrate* tab turns a tiny test
   plate into a test print that measures your pusher's lowest safe height.
   Optional tests show how your bed cools and confirm the FarmBoard door and
   bender respond.

## More things it does

- **Watch the sweep before you print.** Press *Play the sweep* to see the
  exact pusher moves from the file, in the top and side views, at 5–40×.
- **Plan a run.** Enter "I need 40 parts" and ClearLoop works out the loops.
  Once the Monitor has timed your cooldowns, it also tells you when the run
  will finish.
- **Batch files.** Put different plates in one file, for example 3× plate A
  then 2× plate B. Each plate keeps its own sweep and its own per-part push
  heights, the checker checks every plate against its own sweep, and the
  Monitor knows which plate each loop prints, so the plate check, filament
  counting and loop count stay right even when the plates have different
  layer counts. ClearLoop refuses plates that don't share the same filament
  slots, build plate and nozzle, because the printer maps filament once per
  file. The printer's screen shows the first plate's picture and time for
  the whole file (as with FarmLoop's multi-loop files), and ClearLoop tells
  you when plates share object ids, so you know not to use the printer's
  "skip object" on that file.
- **See exactly what was added.** *What ClearLoop adds to your file* shows
  the inserted G-code blocks, with copy buttons. Your slicer's G-code is
  untouched.
- **Find printers automatically.** *Monitor › Find printers* listens for the
  printers' own network announcements and fills in the IP, serial and model.
  You still type the access code.
- **Live loop status.** For each printer the Monitor shows the loop number,
  the current phase (printing, cooling, bending, door), average cooldown and
  loop times measured on your printer, and a plate-bend counter you reset
  when you fit a new plate.
- **Quiet hours.** Plates are still checked overnight, but the next loop waits
  until morning, with the printer paused and its heaters off.
- **Photo alerts.** When parts are left behind, the camera picture arrives
  with the ntfy or Discord notification.
- **Know what every part costs.** The *Costs* tab works out the cost per
  part, per loop and per run: filament for each slot (grams from the slicer,
  priced from your own spools), electricity, printer wear and maintenance,
  build plate and kit wear, labour, overhead, failed loops and packaging. Enter
  a selling price and it shows profit, margin, break-even price, the price for
  your target margin after tax, marketplace, payment and shipping fees, and
  what your printers can make in a day or a month. Loop times use the
  cooldown the Monitor measured on your printer, or your own estimate.
  ClearLoop never invents a price: anything you haven't entered is listed as
  missing rather than guessed. The Make tab shows the per-part cost too, and
  batches are costed plate by plate.

- **Overview.** One screen with every printer's run (loop 7 of 10, done
  around 09:24 based on its measured loop times), parts made today, this week
  and this month, stopped loops, filament used, spools running low and
  anything waiting for you.
- **Backup and restore.** One file with your settings, imported profiles,
  spools, usage, production log and measured timings.

## Filament

The *Filament* tab keeps track of every spool you own:

- **Your spools**: material, colour, brand, price paid, filament when new,
  the empty-spool weight, what's left, where it's kept, when it was opened
  and dried, and notes. Each has a fill bar, its value, and how many days it
  lasts at the rate you've been using it. Search, filter and sort them; buy
  another of the same spool in one click, or print a spool label.
- **What's in each AMS**, live from the printer: type, colour, how full the
  AMS thinks each spool is, the tray being fed, humidity and temperature.
  Bambu spools link themselves from their RFID tag. Link any other spool by
  picking it for its tray, or add a tray's spool to your inventory in one
  click.
- **Every loop is counted.** When the Monitor sees a loop of a file this
  ClearLoop made finish, its filament (the slicer's grams per slot) is taken
  off the spool that fed it. A loop that's cancelled or fails part-way is
  charged what its finished layers used, worked out from the file's own
  G-code. If ClearLoop can't tell which spool fed a loop (two trays with the
  same colour, neither linked), the grams wait in a list for you to assign.
  Nothing is charged to a guess, and files ClearLoop didn't make are never
  charged.
- **Weigh-ins.** Enter the empty-spool weight once, then type the scale
  reading. You can also set what's left directly, or log filament used
  outside ClearLoop. Every entry can be undone.
- **No run comes up short.** The Make tab shows each slot's grams per loop
  and for the whole run, which spool feeds it, and how many loops your spools
  cover, counting a second matching spool in another AMS tray. If they don't
  cover the run, one click sets the loops to what they do cover. With the
  plate check on, a loop-end pause **stays waiting** when the spool can't
  cover the next loop, and you get an alert.
- **Low-stock alerts and a shopping list.** Set a warning weight per spool,
  or one for all spools. The shopping list shows what's low, plus anything
  your last 14 days of use would empty within two weeks.
- **Your slicer's preset, read from the file**: preset name, vendor, density,
  diameter, nozzle range and Bambu Studio's AMS drying settings fill in a new
  spool in one click.
- **Usage history**: a chart of grams per day by material, and a log of every
  loop, weigh-in and correction. Export spools and usage as CSV.
- **Spoolman.** Import your spools from a Spoolman server, and optionally
  report each loop's usage and every weigh-in back to it.
- **Costs use your real spools.** Filament cost per part comes from what you
  paid for the spool that feeds the plate.

## Hardware

| Setup | Profile | Where its moves come from |
|---|---|---|
| **P2S + FarmLoop Stage 2** (FarmBoard door, bender, pusher) | built in | The FarmLoop app's own P2S output (app 2.72, FarmBoard firmware 2.94), copied verbatim. Turn off "Bender fitted" if you removed the bender. |
| **P2S + FarmLoop pusher, no FarmBoard** | built in | The same P2S sweep path. Keep the door open. |
| **Any printer, Stage 1 or Stage 2** (A1, A1 mini, P1S, X1C, H2…) | import | Your own FarmLoop file: slice a plate, process it in the FarmLoop app (the free Lite plan makes Stage 1 files), and give ClearLoop both files. It keeps your kit's moves exactly as FarmLoop wrote them and replaces only the cooldown and sweep heights. |

The P2S Stage 1 kit's own G-code hasn't been published. If you have that kit,
import one of your `FL_S1_` files to use its exact moves.

## Safety pause before the first sweep

On a new setup the first loop pauses after the parts have cooled, just before the pusher moves
(`M400 U1`), so you can check the push heights against your parts and press Resume. Later loops
don't stop. It's automatic: on until a loop has been swept cleanly on one of your printers, then
off. *Safety pause before the first sweep* in *Make a loop file* turns it on or off for a plate
(`clearloop make --first-pause on|off|auto`). A signal just before the pause (`M104 S6`, like
FarmBoard's own S1 to S5) tells the monitor this pause is waiting for you: it never resumes it,
and doesn't mistake it for the end of a loop.

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

### Print history

The Monitor records how every print on your printers ended, not only loop files: finished,
failed or cancelled, the layer it got to, the printer's HMS alerts and error code, the nozzle and
bed temperatures, the AMS trays it fed from, and whether the plate check found parts left behind.
When a print fails or parts are left, the camera picture is kept with it. Files ClearLoop makes are
kept too (the last 100, in `files/`), so the exact G-code that ran can be checked afterwards.
See it under *Monitor › Print history*, or run `clearloop history` (`--json` for everything).

[synthux](../synthux/README.md) reads this history (`synthux physical learn`)
and learns, per printer and filament, which flows, first layers and files fail on your machine.

### Print checks too (optional)

Install [synthux](../synthux/README.md) alongside ClearLoop
(the repository's setup scripts do; by hand `pip install -e ../synthux`) and *Check a file* / `clearloop check` also check
**the print in each loop**: parts off the plate, first layer and layer heights, nozzle and bed
temperatures for the filament, flow the hot end can't melt, stringing across open space,
cooling and warping. These checks read only the print itself, never the cooldown, bend or sweep
ClearLoop adds, and they are listed separately. Without synthux nothing changes.

## Command line

```bash
clearloop make plate.gcode.3mf --loops 5 --wait-for-monitor
clearloop make plate.gcode.3mf --profile p2s-sweep-only --floor 2.5
clearloop check FL_S2_plate.gcode.3mf
clearloop import-profile plain.gcode.3mf FL_S1_plain.gcode.3mf --name "A1 mini Stage 1"
clearloop calibrate sweep-floor tiny.gcode.3mf
clearloop profiles
clearloop cost plate.gcode.3mf --loops 20 --cooldown-min 15   # uses the prices saved in the app
clearloop spools                                                # your spools and what's left
clearloop history                                               # how recent prints ended
```

Settings, imported profiles, spools, usage, empty-plate pictures and the
event log live in `~/.clearloop`. Change the location with `--data` (or `CLEARLOOP_DATA`, which
synthux and ProofLoop read too).

## Before your first unattended run

- Watch the first loop of every new setup with a hand near the power switch.
- Run the sweep-floor test once per printer, and again after changing the pusher.
- Pin your printer firmware. Re-run the tests after an update.

## Development

From this folder (`packages/clearloop`):

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
