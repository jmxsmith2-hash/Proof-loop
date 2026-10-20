# FarmLoop vs ClearLoop: every gap, and where it is addressed

This document maps the documented FarmLoop features, complaints and
recommended improvements (from the October 2026 analysis this project started
from) to ClearLoop. FarmLoop refers to the product by 3D Farmer GmbH. All claims
about it come from public sources: its help center, blog, product pages and
MakerWorld comments.

## Feature parity

| FarmLoop feature | Plan | ClearLoop | Where |
|---|---|---|---|
| G-code processing (cooldown → door/fan → bend → push) for `.gcode.3mf` from Bambu Studio, Orca and PrusaSlicer | Lite/Pro | Yes, free. Plain `.gcode` too. | `gcode/processor.py`, `gcode/routines.py` |
| Multi-copy loops | Lite limited, Pro unlimited | Unlimited `--copies`, plus plate verification between copies when the controller runs | `processor.py`, `loop.py` (`LOOP_END`) |
| Job queue | Pro | Persistent queue with priorities, pinning, model and filament matching | `store.py` |
| Detachment tuning (push height, cooldown by temperature or time, bed temperature, Z) | Pro | Per-part push heights (automatic), temperature/time cooldown, exact bed temperature, all settings per printer | `config.py`, `gcode/sweep.py` |
| Advanced sweep control (beta) | Pro/Business | Default behaviour: lanes per part, heights per part, front-first order, cycle-time estimate, ASCII preview | `gcode/sweep.py`, `clearloop inspect` |
| Trigger calibration (beta) | Pro/Business | Not needed: the primary trigger is the MQTT marker. `probe-marker` proves the channel works. The switch is only a backup. | `protocol.py`, `cli.py` |
| Natural cooldown (beta) | Pro/Business | Automatic from filament type and chamber temperature, with overrides | `gcode/routines.py`, `profiles.py` |
| Fan upgrade support | Toggle | Part, aux and chamber fans driven in G-code. ClearBoard fan output with a timer. | `routines.py`, firmware |
| Door control with obstacle detection | Stage 2 hardware | `DOOR_OPEN` marker; ClearBoard door channel with current-based obstacle detection and back-off | firmware |
| Pricing calculator | Pro | Free: filament, energy, machine time and plate wear per part | `cost.py` |
| Processing API with presets | Business | Free local `POST /api/process`. Per-printer config acts as the presets. | `server.py` |
| SimplyPrint integration | Partner | Output files work with any sender. ClearLoop is itself the queue and sender. | — |
| Board firmware: OTA page, serial log, test mode, e-stop | Stage 2 | ClearBoard: test page, JSON status, config API, e-stop latch, counters, setup access point | `firmware/clearboard` |

## Complaints

| Documented complaint | ClearLoop fix |
|---|---|
| Stage 2 silently never starts (bed stops short of the switch) | MQTT marker trigger is primary. A missing trigger raises `trigger_missed` and goes to recovery. The firmware also flags a missed trigger in stand-alone mode. |
| Fragile filename-prefix arming (`FL_S2_`); renaming or re-saving breaks it | Arming comes from the `JOB_START` marker in the G-code. The manifest lives inside the G-code too. Tests cover renamed files. |
| Short parts left on mixed-height plates | Per-part push heights. Parts below the sweep floor produce a warning, and camera verification catches anything left behind. |
| ASA/ABS corners lift when the door opens | Automatic natural cooldown for warp-prone materials and heated-chamber jobs |
| App writes the bed temperature 5 °C low | Bed temperature is written exactly. `test_bed_temperature_written_exactly` enforces it. |
| "Bed leveling failed" with the bender installed | `JOB_START` comes before homing, with a retract dwell. The controller also calls `prepare()` (retract and confirm) before every upload. |
| Door opens randomly; FarmBoard won't connect | Firmware has a status endpoint, LED codes and an event log. The controller reports an unreachable ejector as a blocking error with the exact message. |
| Multi-loop: later loops don't trigger | Edge-detected markers per copy, with tests. A failed bend pauses the printer. |
| Loud motors | 20 kHz PWM, quiet profile (60 % duty, 900 ms soft start), quiet hours |
| Plates wear; bending stresses plates | Cycles and retries per plate, rated-life warnings, release-degradation warnings. Bend force is current-limited by stall detection. |
| Subscription required for hardware you own | No subscription, no account, no cloud |
| No monitoring, verification or order integration | Dashboard, camera verification, Shopify and generic order intake |

## Recommended improvements (A1–A10)

| # | Recommendation | Status |
|---|---|---|
| A1 | Camera check that the bed is empty after the sweep; one retry, then pause and notify | Done, and the check also runs **before every start**. `vision.py`, `loop.py`. |
| A2 | MQTT print state plus a G-code marker as the trigger, the switch as backup, a watchdog | Done. `protocol.py`, `loop.py` watchdogs, firmware lockout. |
| A3 | Signed marker in metadata instead of a filename prefix; publish the protocol | Done. `manifest.py`, [trigger-protocol.md](trigger-protocol.md). |
| A4 | Actuator health from current or force; cycles per plate | Done. Firmware current sensing, `peak_current_a` and `eject_s` logged per loop, plate table. |
| A5 | Fix the bed-temperature offset and the levelling interference | Done (see above). |
| A6 | Geometry-aware default sweep | Done. The height map comes from the G-code itself, so it works for any slicer. |
| A7 | Material-aware defaults | Done. Reads 3MF slice info and project settings. |
| A8 | Quieter profiles and quiet hours | Done |
| A9 | Run locally; hardware usable without a subscription | Done. Local server and Docker. Everything is free. |
| A10 | Fleet status, HMS handling, notifications, per-printer stats | Done |

## Build list (section B)

| Item | Status |
|---|---|
| G-code/3MF processor with MD5 update and bounding-box-aware sweeps | Done |
| LAN controller: MQTT/TLS, FTPS, camera | Done (`printer/bambu.py`) |
| Queue and AutoPrint state machine: FINISH → eject → verify → next; HMS and runout; Discord/ntfy | Done |
| Bed-empty vision with per-printer references | Done |
| ESP32 ejector board on local MQTT | Done (ClearBoard, plus an HTTP mode for the controller) |
| Hardware-agnostic loop controller for bender kits, SwapMod, PrintFlow3D, 3DQue-style beds and DIY | Done ([hardware-compat.md](hardware-compat.md)) |
| Etsy/Shopify intake | Shopify webhook done. Etsy through the generic order endpoint (n8n/Zapier). |

## Not included (yet)

- **Mechanical designs** (bender brackets and mounts) for each printer family.
  That is the expensive, per-model part. ClearLoop drives existing kits and DIY
  mechanisms instead, and contributions of open designs are welcome.
- **AI spaghetti detection.** Bed verification only runs between jobs. Obico can
  run alongside for in-print detection.
- **Direct Etsy API polling.** Use the generic order endpoint.
- **Native mobile app.** The dashboard works on phones, and ntfy delivers push
  notifications.
- **Printer firmware management.** ClearLoop warns when firmware changes.
  Pinning versions is up to you.
