# Check: designs and G-code before you print

The **Check** tab takes a CAD file or a sliced file. Drop it on the matching card; each check runs
in the background and links to its report. On the command line, `proofloop check FILE` picks the
right check for the file, and `proofloop check COMMAND` gives every option.

- [CAD designs](#cad-designs)
- [From CAD to a print-ready verdict](#from-cad-to-a-print-ready-verdict)
- [G-code](#g-code-the-sliced-file)
- [Learning from your printers, and from you](#learning-from-your-printers-and-from-you)
- [Moulded and machined products](#moulded-and-machined-products)
- [Product predictions](#product-predictions-prediction-not-a-test)

A checked Bambu plate (`.gcode.3mf`) gets a **Loop file** row: **Make it now** makes the loop file
with your saved loop-file settings (profile, sweep floor, push depth, cooldown, first-sweep safety
pause), checks it and offers it for download; **Fine-tune in Loop files** opens the plate in *Make
a loop file* to change them for that plate. Jobs are kept until you remove them. Checks that take
over 30 seconds send a notification to your targets (set up under *Printers › Monitor*).

## CAD designs

A typical part checks in about 6 seconds, and a 670,000-triangle export in about 13. Point it at a
CAD export and it checks the geometry before you spend filament, then learns from your real prints
so the next check is about *your* printer.

```bash
proofloop check part.stl                                 # STL, 3MF, OBJ, PLY, GLB (STEP: pip install ".[step]")
proofloop check design part.3mf --material "PETG HF"     # 3MF from Fusion, Tinkercad or Bambu Studio
proofloop check design part.stl --printer bambu_h2d --handheld
proofloop check part_v2.stl --design examples/cad/phone_stand.yaml   # same design name = next version
proofloop check design part.stl --auto-eject             # for FarmLoop loops: pusher, copies per plate
proofloop check printed "Phone stand" --measure measured.csv --defect stringing --notes "clip too tight"
proofloop check history "Phone stand"                    # versions and what changed
proofloop check new                                      # answer a few questions to describe a design
```

Each check writes `~/.proofloop/reports/designs/<name>_v<N>/index.html` with renders from several
angles, every finding marked on the model, and a "fixed / still there / new" comparison against the
previous version.

**Printability:** a broken or non-watertight mesh, wrong units (a 0.06 mm part exported in metres),
fit in the build volume, overhangs and bridges (only on outside faces), the best print orientation,
bed contact, thin walls and thin pins below what the nozzle can lay down, slender towers, sharp
edges you will touch, clearances between parts that will fuse, parts joined only along an edge,
and tipping over (centre of mass, plus a declared load such as a phone leaning on it). It also
estimates mass, filament length, time and cost. Support material is measured from each overhang
down to the bed or the part below; it counts towards filament, time and cost but not towards the
weight of the finished part.

**Holes:** vertical holes small enough to close up for your nozzle are flagged. Small fit holes,
under 8 mm (screws, pins, heat-set inserts), get the size to model them at. That uses the offset
your own measured prints taught it, or a typical FDM allowance until you record a print. On resin
printers, sealed hollow pockets that would trap liquid resin are flagged, with where to add drain
holes. Horizontal holes (along X or Y) are flagged too: their top is printed in mid-air, so small
ones come out oval and large ones sag; the fix is a teardrop or diamond top.

**Fine detail:** cross-sections are cut the way a slicer cuts them and drawn at a sixth of the
nozzle width. Raised lines or text strokes narrower than the nozzle (dropped or blurred), engraved
lines it can't leave open (they fill in), and raised or sunk areas shallower than a layer (they
vanish) are reported with the sizes to use instead. Narrow gaps between separate parts are left to
the clearance check.

**Flexing tabs:** thin tabs fixed at one end (clips, snap-fits, latches) get how far their free end
can bend before the material is strained past its limit (`snap_strain_pct` per material in
`printers.yaml`: about 2% for PLA, 4% for PETG, 5% for ABS). Tabs standing upright are flagged,
because bending them pulls the layers apart.

**Auto-eject:** with `--auto-eject` (or `use: {auto_eject: true}`, or `prepare --loops N`), each
part standing on the bed is checked against how a loop file's pusher clears it: pushed at its
height minus the push depth (10 mm), never below the pusher's calibrated floor, from the back of
the plate to the front. It flags parts too low for the pusher or barely above its floor, parts that
tip rather than slide, large flat contact patches that may not release at the release temperature
(more so without the bender), and very small parts. It also says how many copies fit on one plate
inside the pusher's lanes. Floor, push depth, release temperature and bender come from your
loop-file settings.

**Human fit**, from the built-in anthropometrics and the design YAML:

* grip size and weight in the hand (`--handheld`);
* wearable inner sizes against wrist, finger, head or other population ranges;
* connector openings too small for the plug's moulded body (USB-C, USB-A, micro-USB, Lightning,
  5.5 mm barrel, HDMI);
* buttons smaller than a fingertip.

Personas differ in hand size, grip strength, body size, motor control and eyesight, and react only
to measured facts that apply to them (a persona with a weak grip, for example, reacts to the
measured grip diameter and weight). A product-knowledge checklist adds category-specific questions
for everyday goods, electronics, wearables and 3D-printed parts.

**After printing**, record what happened. Use a CSV of `feature, designed_mm, measured_mm, kind
(outer|hole|gap), axis` (see `examples/cad/v1_print_measurements.csv`), plus defects. ProofLoop
learns offsets per printer and material: holes printing 0.2 mm small, gaps closing up, the overhang
angle your printer really manages. The next check applies them and tells you which ones it used.

`examples/cad/` has a phone stand with planted flaws (v1) and its fixed v2, a tool handle with a
too-small USB-C opening, and a plain block as a control (`python examples/cad/make_examples.py`
regenerates them). The default printer is the Bambu Lab P2S; H2D, H2S, X1, Prusa MK4, Ender 3 and
generic FDM and resin profiles are included, and slicer material names ("PLA Basic", "PETG HF") are
understood. Add your own printer by putting a `printers.yaml` in `~/.proofloop/config` (start from
`src/proofloop/data/printers.yaml`). From Blender, export STL or 3MF; from Fusion, *File > 3D Print*
or *Export > 3MF*.

## From CAD to a print-ready verdict

With Bambu Studio or OrcaSlicer installed, one command checks the design, slices it with the
printer's own presets, checks the G-code and compares the two:

```bash
proofloop check prepare part.stl                         # P2S + PLA by default
proofloop check prepare part.stl --material PETG --support --open
proofloop check prepare part.stl --loops 5               # also a loop file, checked too
proofloop check prepare part.stl --fix                   # apply the setting fixes and slice again
```

* The slicer is found where it is normally installed (or `--slicer PATH`, or `PROOFLOOP_SLICER`).
  Its system presets are used: for a P2S with a 0.4 mm nozzle and PLA, "Bambu Lab P2S 0.4 nozzle",
  "0.20mm Standard @BBL P2S" and "Bambu PLA Basic @BBL P2S". Presets inherit from one another, so
  ProofLoop flattens them (parents and the printer's start and end G-code templates) before
  slicing. `--process-preset` and `--filament-preset` pick others, and `--support / --no-support`
  overrides support. If the presets are somewhere unusual, set `PROOFLOOP_SLICER_PROFILES` to the
  folder that holds `BBL/`.
* **What the slicer did with the design** is checked: an overhang the design check says needs
  support but which got none, and a thin wall or detail the slicer left out because no line fits.
* The verdict is **Ready to print** or **Fix N things first**, with the slicer's own grams and
  time, and links to the full design check and G-code check.
* `--fix` applies the slicer settings that fix what was found (support on, max volumetric speed,
  fan, first-layer height, temperatures, Arachne walls ...), slices again and checks again, up to
  twice, and shows what changed and how many problems were left.
* In the window, tick "Slice it with Bambu Studio too" on the CAD card.

The command line follows Bambu Studio's own option definitions (`--slice`, `--export-3mf`,
`--load-settings`, `--load-filaments`); the tests use a stand-in with the same command line, and CI
runs the real OrcaSlicer and Bambu Studio on Windows.

## G-code: the sliced file

After slicing, check the file itself before it goes to the printer:

```bash
proofloop check part.gcode                               # Bambu Studio, OrcaSlicer, PrusaSlicer, Cura
proofloop check gcode part.gcode.3mf --plate 2           # Bambu Studio "Export plate sliced file"
proofloop check gcode part.gcode --printer bambu_h2d --material "PETG HF" --open
```

The printer and filament are read from the file when you don't give them. Every move, temperature
and fan command is measured against the printer profile and the material:

* **plate**: extrusion past the plate edges, above the build height, or into the bed;
* **printer**: sliced for another printer or nozzle;
* **layers**: first-layer height and speed, layers too thick for the nozzle;
* **temperatures**: nozzle and bed against the material's range and the printer's limits,
  extruding cold, not waiting for the nozzle to heat;
* **flow**: mm³/s the moves ask for against the hot end (the most common cause of weak, thin lines
  on fast printers);
* **extrusion**: line widths that point to the wrong filament diameter or flow;
* **retraction**: too long for direct drive, and travels across open space without retracting
  (travels inside infill are left alone, as slicers intend);
* **cooling**: part fan off for PLA, too much fan for ABS/ASA, layers too quick to cool;
* **adhesion**: warping materials on open printers, tall narrow parts without a brim;
* **end**: heaters left on; **consistency**: the moves against the slicer's own header.

Findings a slicer setting can fix come with the exact setting and value, named the way Bambu Studio
and OrcaSlicer show it ("Filament › Max volumetric speed = 21"), and the report writes them as user
presets that inherit from the presets the file was sliced with, ready for *File › Import › Import
Configs*.

The report shows the first layer on the plate (anything off the plate in red), layer previews, a
side view with thick layers marked, and a flow map. A 50 MB file checks in about 5 seconds.
`examples/gcode/` has a clean bracket and one with seven planted flaws
(`python examples/gcode/make_examples.py` regenerates them).

### Loop files

Auto-eject loop files (`FL_S1_` / `FL_S2_`, made under *Loop files* or by the FarmLoop app) repeat
the plate once per loop, with a cooldown and sweep after each print. The G-code check recognises
them:

* each loop's print is checked like a normal plate (layers counted per loop, the first layer of
  every loop measured from the bed), and the cooldown, bend and sweep moves after it are not
  mistaken for stringing or bad travel;
* filament and time are compared with each loop's own slicer header;
* the report has a **Loop file** section: how many loops and plates, grams per loop, and how often
  the printer waits for the plate check;
* the [loop-file checks](loop-files.md#check-any-loop-file) run on the ejection cycle too: a long
  heated wait before the print (the FarmLoop app 2.72 cooldown bug), `M190 R`, parts no sweep pass
  reaches, code the printer ignores, moves after the steppers are off, a bad checksum. A plain
  `.gcode` loop file is still checked for the heated wait.

`examples/gcode/` has a 3-loop file of a clean two-part plate made by ProofLoop and a
FarmLoop-app-style file with both documented problems.

## Learning from your printers, and from you

The printer monitor records how every print on your printers ended ([print
history](printers.md#print-history)). Each G-code check reads it (read-only), or run
`proofloop check learn`: it re-checks the G-code that ran (ProofLoop keeps the loop files it makes;
add `--files` folders for your other sliced files) and gives each failure its likely cause from
what was printing when it stopped: the first layers, an AMS alert, the camera's spaghetti detection,
flow near the hot end's limit, or very quick layers.

What it learns per printer and filament is used in every later G-code check:

* flow that has failed on your printer is flagged, and flow it has finished at least twice is no
  longer called a problem;
* repeated first-layer failures are pointed out, with what to try;
* a file that failed before says at which layer, with the printer's HMS codes;
* a loop file whose plate check found parts left says so.

The report's *What your printer taught* section shows the counts. Causes are likely ones from the
file, not a diagnosis.

**Wrong about something?** Under each finished job you can mark a finding *Not a problem* or *It
was right*. A check marked a false alarm at least twice for a printer and material, more often than
it was confirmed, counts one level lower there from then on (four times: only a note), and says so.
The loop-file safety checks are never lowered. Your verdicts are kept in
`~/.proofloop/feedback.json`.

## Moulded and machined products

The same CAD check works for parts that will be injection moulded or CNC machined:

```bash
proofloop check design housing.stl --process injection --material PP     # mould opens along the best axis
proofloop check design housing.stl --process injection --pull z
proofloop check design bracket.step --process cnc --material aluminium_6061
```

(or set `process:` and `pull:` in the design YAML, or pick "How it is made" on the CAD card).

**Injection moulding** (two-part mould, straight pull):

* undercuts: space with part material above and below it along the pull, which neither mould half
  can form. It says which side a slider could come from, or that nothing can (a sealed space);
* draft on side walls (1° by default, 3° for textured surfaces);
* wall thickness from a distance map: too thin to fill (short shot), thick sections that sink or
  void, and walls uneven enough to warp;
* sharp inside corners;
* shot weight, clamp force from the projected area, cooling and cycle time, and the shrinkage to
  allow for.

**CNC machining** (3-axis, re-clamped as needed):

* material no straight tool can reach from any side;
* the fewest set-ups that reach the rest;
* inside corners sharper than the tool's radius, and slots narrower than the smallest tool (gaps
  between separate bodies don't count);
* pockets deeper than 4x their width, thin walls, deep or tiny holes;
* stock size, how much is cut away, and a rough machining time.

Grip, weight, sharp edges, clearances between parts and tipping are checked as for printed parts,
and personas react the same way. Material and process figures live in `processes.yaml` (ABS, PP,
PC, PA66, POM, HDPE and TPE for moulding; aluminium, steel, stainless, brass and plastics for
machining). They are typical design-guide values; a moulder's or machinist's quote wins.

## Product predictions (prediction, not a test)

```bash
proofloop check review examples/physical/trailflask.yaml
```

Input (`examples/physical/trailflask.yaml`):

* spec values;
* photos;
* CAD exports (STL bounding box) or measured dimensions (CSV);
* **real reviews of similar products** (CSV/text);
* specs of those similar products;
* optional prototype sensor CSVs.

The reviews become a complaint taxonomy (size, weight, durability, ergonomics, leaks, cleaning,
value, performance, instructions) with quotes. Personas weigh each pattern against spec comparisons
with the similar products, adjusted by their traits, and give a forced-choice "most likely return
reason". Every claim must cite a spec value, a review pattern or a photo.

Every output is labelled **"Prediction, not a test"**. ProofLoop never claims to assess feel,
weight in the hand, texture, taste or smell. It compares a spec value (e.g. 540 g against a 495 g
median); it does not say "feels heavy". Sensor data is analysed for spikes, stuck sensors and usage
sessions. Predictions are only as good as the reviews and specs you provide, and never a substitute
for handling the product.
