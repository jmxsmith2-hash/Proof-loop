# Loop files: FarmLoop auto-eject, done right

The **Loop files** tab writes the G-code that drives FarmLoop hardware on Bambu Lab printers, made
for the P2S. It replaces the FarmLoop app, not the kit: you keep your FarmLoop Stage 1 parts or
Stage 2 FarmBoard, and ProofLoop writes the files with the documented problems fixed.

| FarmLoop app | ProofLoop |
|---|---|
| One push height for the whole plate (tallest part − 10 mm), so a 5 mm part next to a 170 mm part is never touched | The same rule **per part**. Every part is pushed at its own height, tallest first. |
| App 2.72 put a 2-hour "natural cooldown" **before** the print with the heaters on | Natural cooldown runs **after** the print with heaters off. Old FarmLoop files can be checked for this bug. |
| Nothing checks the plate before the next loop starts | Optional **fail-safe plate check**: the printer waits after each loop until the camera sees an empty plate |
| Subscription for Stage 2, cloud app | Free, open source, runs on your computer, no account |

Every value ProofLoop uses for a printer or kit has a public source, or is measured on your machine
with a guided test. Nothing is guessed. See [sources.md](sources.md).

## Make a loop file

1. **Slice and drop.** In Bambu Studio, slice your plate and use *File › Export › Export plate
   sliced file*. Drop the `.gcode.3mf` on *Make a loop file* (or click **Fine-tune in Loop files**
   on a plate you checked under *Check*). It recognises the printer, shows every part with the
   height the pusher will reach it at, and tells you about anything it can't do safely. Every
   setting has a slider. **Push depth** sets how far below each part's top the pusher hits
   (FarmLoop always uses 10 mm), and each part has its own slider if one needs a different height.
   Nothing ever goes below your calibrated sweep floor, and a height that would miss a part is
   refused.
2. **Download** the `FL_S2_…` file and send it the way you always do (Bambu Studio *Print plate*,
   Bambu Handy, or Bambuddy). Keep the file name: FarmBoard kits look for the prefix.
3. **Once per printer, calibrate.** *Calibrate* turns a tiny test plate into a test print that
   measures your pusher's lowest safe height. Optional tests show how your bed cools and confirm
   the FarmBoard door and bender respond.

**Full print check** sends the plate to *Check* for layer previews, flow, temperatures and the
slicer settings that fix each problem.

Also on *Make a loop file*:

- **Watch the sweep before you print.** *Play the sweep* shows the exact pusher moves from the
  file, in the top and side views, at 5–40×.
- **Plan a run.** Enter "I need 40 parts" and ProofLoop works out the loops. Once the printer
  monitor has timed your cooldowns, it also tells you when the run will finish.
- **Batch files.** Put different plates in one file, for example 3× plate A then 2× plate B. Each
  plate keeps its own sweep and its own per-part push heights, the checker checks every plate
  against its own sweep, and the monitor knows which plate each loop prints, so the plate check,
  filament counting and loop count stay right even when the plates have different layer counts.
  Plates that don't share the same filament slots, build plate and nozzle are refused, because the
  printer maps filament once per file. The printer's screen shows the first plate's picture and
  time for the whole file (as with FarmLoop's multi-loop files), and you're told when plates share
  object ids, so you know not to use the printer's "skip object" on that file.
- **See exactly what was added.** *What ProofLoop adds to your file* shows the inserted G-code
  blocks, with copy buttons. Your slicer's G-code is untouched.
- **Filament and cost.** Each slot's grams per loop and for the whole run, which spool feeds it,
  how many loops your spools cover (counting a second matching spool in another AMS tray), and the
  cost per part. If the spools don't cover the run, one click sets the loops to what they do cover.

## What goes in the file

1. **Start** (Stage 2): FarmLoop's FarmBoard reset, in the same place the FarmLoop app puts it.
2. **Your print**, untouched.
3. **After the print**, inside the printer's executable block:
   1. Steppers back on (the stock end code switches them off) and heaters off.
   2. Optional natural cooldown: door shut, fans off.
   3. FarmBoard T1: door opens, fan on.
   4. Wait until the bed reaches the release temperature (`M190 S`, as FarmLoop does; never `M190 R`).
   5. FarmBoard T2: bend, with FarmLoop's timing.
   6. The sweep: FarmLoop's lanes and feeds, each part pushed at its own height.
   7. Park, safety check, then T3 closes the door.
   8. Optional pause for the plate check.
4. **Loops** repeat the whole plate, the same layout the FarmLoop app uses.

Each block ProofLoop adds is wrapped in `; >>> PROOFLOOP <name>` / `; <<< PROOFLOOP <name>`, so a
loop file can be read back, checked and made again.

## Safety pause before the first sweep

On a new setup the first loop pauses after the parts have cooled, just before the pusher moves
(`M400 U1`), so you can check the push heights against your parts and press Resume. Later loops
don't stop. It's automatic: on until a loop has been swept cleanly on one of your printers, then
off. *Safety pause before the first sweep* on *Make a loop file* turns it on or off for a plate
(`proofloop loop PLATE --first-pause on|off|auto`). A signal just before the pause (`M104 S6`, like
FarmBoard's own S1 to S5) tells the monitor this pause is waiting for you: it never resumes it, and
doesn't mistake it for the end of a loop.

## The plate check (optional, fail-safe)

Turn on **Wait for the plate check** when you make a file, and add your printer under *Printers ›
Monitor*. The printer needs LAN-only mode with Developer Mode on ([setup guide](bambu-setup.md)).

- After each sweep the printer pauses (`M400 U1`, Bambu's own pause code).
- The monitor takes a camera picture and compares it with your pictures of the empty plate. If
  it's empty, the monitor resumes the next loop.
- If parts are left, or ProofLoop isn't running, the printer **keeps waiting** with the heaters
  off. Clear the plate and press Resume. A crash or a missed message can never start a print on a
  dirty plate.

ProofLoop never uploads or starts prints. It only resumes a loop file that is waiting for its plate
check. More in [printers.md](printers.md).

## Check any loop file

*Check a file* (or `proofloop loop check FILE`) reads any loop file, the FarmLoop app's or
ProofLoop's, and reports known problems:

- a long wait before the print with the heaters on;
- `M190 R`, which faults on a P2S;
- parts that no sweep pass reaches;
- G-code the printer will ignore;
- moves after the steppers were switched off;
- a bad checksum.

It also checks **the print in each loop**: parts off the plate, first layer and layer heights,
nozzle and bed temperatures for the filament, flow the hot end can't melt, stringing across open
space, cooling and warping. These checks read only the print itself, never the cooldown, bend or
sweep after it, and they are listed separately. `proofloop check FILE` on a loop file runs the same
loop-file checks inside the full G-code report.

## Costs

*Costs* works out the cost per part, per loop and per run: filament for each slot (grams from the
slicer, priced from your own spools), electricity, printer wear and maintenance, build plate and
kit wear, labour, overhead, failed loops and packaging. Enter a selling price and it shows profit,
margin, break-even price, the price for your target margin after tax, marketplace, payment and
shipping fees, and what your printers can make in a day or a month. Loop times use the cooldown the
monitor measured on your printer, or your own estimate. ProofLoop never invents a price: anything
you haven't entered is listed as missing rather than guessed. *Make a loop file* shows the per-part
cost too, and batches are costed plate by plate.

## Hardware profiles

| Setup | Profile | Where its moves come from |
|---|---|---|
| **P2S + FarmLoop Stage 2** (FarmBoard door, bender, pusher) | built in | The FarmLoop app's own P2S output (app 2.72, FarmBoard firmware 2.94), copied verbatim. Turn off "Bender fitted" if you removed the bender. |
| **P2S + FarmLoop pusher, no FarmBoard** | built in | The same P2S sweep path. Keep the door open. |
| **Any printer, Stage 1 or Stage 2** (A1, A1 mini, P1S, X1C, H2…) | import | Your own FarmLoop file: slice a plate, process it in the FarmLoop app (the free Lite plan makes Stage 1 files), and give ProofLoop both files under *Hardware*. It keeps your kit's moves exactly as FarmLoop wrote them and replaces only the cooldown and sweep heights. |

The P2S Stage 1 kit's own G-code hasn't been published. If you have that kit, import one of your
`FL_S1_` files to use its exact moves.

## Command line

```bash
proofloop loop plate.gcode.3mf --loops 5 --wait-for-monitor
proofloop loop plate.gcode.3mf --profile p2s-sweep-only --floor 2.5
proofloop loop check FL_S2_plate.gcode.3mf
proofloop loop import-profile plain.gcode.3mf FL_S1_plain.gcode.3mf --name "A1 mini Stage 1"
proofloop loop calibrate sweep-floor tiny.gcode.3mf
proofloop loop profiles
proofloop loop cost plate.gcode.3mf --loops 20 --cooldown-min 15   # uses the prices saved in the window
```

`proofloop loop PLATE` is short for `proofloop loop make PLATE`.

## Before your first unattended run

- Watch the first loop of every new setup with a hand near the power switch.
- Run the sweep-floor test once per printer, and again after changing the pusher.
- Pin your printer firmware. Re-run the tests after an update.
