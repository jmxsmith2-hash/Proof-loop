# Architecture

```
 .gcode.3mf ──▶ threemf.py ──▶ gcode/objects.py ──▶ gcode/sweep.py ──▶ gcode/routines.py ──▶ FL_S2_….gcode.3mf
  (plain slice)  facts from     parts: footprint +    per-part push     cycle from the
                 the file       height from labels    heights on the    hardware profile
                                                      kit's own lanes
                                     ▲
                 profiles.py ────────┘   built-in (profile_data/*.json) or imported (importer.py)

 printer (LAN MQTT + camera) ──▶ printer/bambu.py ──▶ monitor.py ──▶ resume, or keep waiting + notify
```

| Module | Job |
|---|---|
| `threemf.py` | Load and save `.gcode.3mf`. Everything comes from the file itself: `printer_model_id`, printable area and height, filaments, object names and heights. Writes back only the plate G-code and its checksum. |
| `printers.py` | `printer_model_id` → printer name (from Bambu Studio's profiles). |
| `gcode/objects.py` | Footprint and height of each labelled object, cross-checked against the slicer's header. |
| `gcode/sweep.py` | Push heights per lane, the travel height, warnings and problems, and sweep time capped by the printer's own speed limits. |
| `gcode/routines.py` | Builds the end-of-print cycle and the start step from the profile. |
| `gcode/processor.py` | Analyse (preview data, steps, checks) and process (loops, output name). Refuses FarmLoop output; re-processing its own output gives the same result. |
| `profiles.py` | Hardware profile model and store. |
| `importer.py` | Learns a profile by diffing a plain slice against FarmLoop's version of it. |
| `lint.py` | Safety checks for any loop file. |
| `print_checks.py` | Optional: synthux's checks of the print in each loop, when synthux is installed. |
| `filament.py` | Spool inventory, usage log, weigh-ins, which spool feeds a slot, how many loops your spools cover. |
| `tracking.py` | Remembers the files ClearLoop makes; charges finished and part-way loops to spools; holds a loop the spools can't cover; links RFID spools to AMS trays. |
| `gcode/usage.py` | Filament used up to each layer, from the plate's own G-code. |
| `printer/ams.py` | AMS trays from MQTT reports (partial updates merged). |
| `spoolman.py` | Optional Spoolman import and usage reporting. |
| `routes_more.py` | API for the Filament and Overview tabs, and backup/restore. |
| `costing.py` | Cost per part, loop and run, selling price, fees and capacity, from your own rates and the file's grams and times. Unset rates are reported, never filled in. |
| `calibration.py` | Guided test prints (sweep floor, cooldown, door, bend). |
| `printer/state.py`, `printer/bambu.py` | MQTT status, pause/resume, camera frames. |
| `vision.py` | Empty-plate comparison against reference pictures. |
| `monitor.py` | Recognises loop-end pauses, checks the plate, resumes only when it's empty. |
| `service.py`, `server.py`, `static/index.html` | The app. |
| `config.py`, `store.py`, `notify.py` | Settings JSON, event log, phone notifications. |

## The monitor's rule

A pause is treated as a loop-end pause only when all of these hold:

1. The printer reports a loop file (`FL_S1_`/`FL_S2_` in the job name or file
   path, and not a `CALIBRATION` file).
2. The last layer is done (`layer_num >= total_layer_num`).
3. The state is `PAUSE`.

Then the monitor checks the plate. It sends `resume` only if the camera shows
an empty plate against at least one reference picture. In every other case
(no picture, camera error, parts found, any other kind of pause) it does
nothing except tell you. Because the file itself makes the printer wait, doing
nothing is always the safe outcome.
