# ClearLoop

**Open-source, LAN-first, closed-loop continuous printing for Bambu Lab print farms.**

ClearLoop makes your printers clear their own plates and start the next job. Unlike
open-loop ejection systems, it **checks that the plate is actually empty before
every start**, so one stuck part can't turn into a crash or a ruined plate.

It's a free alternative to subscription ejection software such as FarmLoop. It
includes every improvement from our analysis of FarmLoop's documented complaints
(see [docs/comparison.md](docs/comparison.md)):

- **Software:** Python. Processes sliced files, talks to printers over LAN-only
  MQTT/FTPS/camera, runs the queue, and serves a REST API and dashboard.
  GPL-3.0-or-later.
- **Firmware:** ESP32 [ClearBoard](firmware/clearboard). Drives a plate bender
  and door opener, with current sensing. GPL-3.0-or-later.
- **Hardware:** an electronics reference design for the board
  ([hardware/clearboard](hardware/clearboard)). CERN-OHL-S-2.0.
- **No account, no cloud, no subscription, no paywalled features.**

> **Status: alpha.** The software is fully unit-tested against simulated
> printers. It has **not yet been validated on physical printers**. A few Bambu
> firmware behaviours it relies on are listed under
> [What to verify on your machine](#what-to-verify-on-your-machine). Run
> supervised loops first.

---

## What it does

```
slice ─▶ queue ─▶ verify plate empty ─▶ upload + start ─▶ print
                         ▲                                  │
                         │                                  ▼
                   recovery sweep ◀── not empty ── camera check ◀── cool ▶ bend ▶ sweep
                         │
                  still not empty ─▶ pause queue + notify you
```

| Problem with open-loop ejection | What ClearLoop does |
|---|---|
| Stage 2 "silently does nothing" if the bed stops short of the switch | Primary trigger is a marker the printer reports over MQTT. The switch is only a backup. A print that finishes with no trigger raises a `trigger_missed` error and goes to recovery. |
| Nothing checks the plate is empty before the next job | A camera comparison against reference images of the empty plate runs **before every start**. If parts remain: one recovery sweep, then block and notify. |
| Renaming a file breaks automation (filename-prefix arming) | Arming comes from a `JOB_START` marker inside the G-code. A signed manifest travels inside the file. File names are irrelevant. |
| Short parts left behind on mixed-height plates (one push height) | Rebuilds every part's footprint and height from the G-code. Each part gets its own lanes and push height. Front parts are cleared first. |
| ASA/ABS warp when the door opens on hot parts | Reads filament types from the 3MF. ABS/ASA/PC/PA and heated-chamber jobs automatically get a door-shut, fans-off cooldown first. |
| Loud actuators | 20 kHz PWM (no whine), a soft-start "quiet" profile, and quiet hours (gentle profile or no new jobs overnight). |
| Plate wear and no health data | Actuator current sensing tells a finished bend from a stall. Cycles and retries are counted per plate. Warnings fire on rated life and on a rising rate of loops that needed help. |
| Bed temperature written 5 °C off | Bed temperature is written exactly, with a test that enforces it. |
| "Bed leveling failed" with the bender installed | `JOB_START` comes before homing. The ejector retracts and confirms before every job. |
| Hardware needs a subscription to run | Everything is free and runs on your LAN, including the processing API. |
| No monitoring, notifications, stats or orders | Fleet dashboard, HMS/runout handling, ntfy/Discord/webhook alerts, per-printer stats, AMS-aware job matching, Shopify and generic order intake, and a cost calculator. |

It works with **any ejection hardware**: the ClearBoard, a bender kit in
mechanical mode, SwapMod/PrintFlow3D plate changers, tilt-and-scraper or
frictionless beds, or a DIY pusher on a Home Assistant relay. See
[docs/hardware-compat.md](docs/hardware-compat.md).

## Quick start

### 1. Just make files loop-ready (no server, works with Bambu Cloud)

```bash
pip install -e .
clearloop inspect plate.gcode.3mf --printer p1s           # parts, heights, sweep plan, cost, warnings
clearloop process plate.gcode.3mf --printer p1s --copies 5 # sweep-only loop, 5 plates in one file
clearloop process plate.gcode.3mf --printer x1c --eject board --door -o out.gcode.3mf
```

Send the output with Bambu Studio, Handy, or any farm tool. This mode is
open-loop: nothing checks the plate between copies unless the controller below
is running.

### 2. Run the closed-loop controller

1. Put each printer in **LAN-only mode with Developer Mode** enabled
   ([docs/bambu-setup.md](docs/bambu-setup.md)).
2. Install and configure:

   ```bash
   pip install -e ".[server]"
   cp examples/config.example.yaml config.yaml   # add printers: IP, serial, access code, ejector
   clearloop probe-marker --config config.yaml p1s-01   # prove the trigger channel works on this firmware
   clearloop reference   --config config.yaml p1s-01    # with the plate EMPTY: capture a reference image
   clearloop serve --config config.yaml                  # dashboard at http://localhost:8484
   ```

   Or run it with Docker: `docker compose up -d`, with `config/config.yaml` and
   `farm.data_dir: /data`.
3. Add plates to the queue from the dashboard, with `clearloop enqueue`, or via
   orders. ClearLoop picks a compatible idle printer that has the right
   filament loaded and verifies the plate. Then it starts the job, follows the
   loop, and verifies the plate again.

## How the loop works

The processor injects these blocks around the sliced G-code:

1. **`JOB_START` marker**, before homing. The ejector retracts and the door
   closes, so a protruding bender can't break bed levelling.
2. **The print itself**, with the bed temperature untouched or written exactly.
3. **Cooldown.** Heaters go off. Warp-prone materials on enclosed printers get a
   door-shut, fans-off cooldown. Then a `DOOR_OPEN` marker, fans on, and a wait
   until the bed reaches the filament's release temperature.
4. **Eject.** The plate moves to the bend position and an `EJECT` marker is
   sent. The printer dwells while the ejector bends.
5. **Geometry-aware sweep.** For each part, front-most first: travel above the
   tallest part still standing, drop behind the part, push at a fraction of its
   own height.
6. **`SWEEP_DONE` marker**, then park.

The controller watches the markers and printer state over MQTT and runs one
state machine per printer: `IDLE → STARTING → PRINTING → EJECTING → VERIFYING →
(RECOVERING) → IDLE`, or `BLOCKED` on anything unexpected. Every failure path
emits an event and a notification, and stops new jobs on that printer until the
plate is confirmed clear. The full design is in
[docs/architecture.md](docs/architecture.md). The marker format is an open
protocol ([docs/trigger-protocol.md](docs/trigger-protocol.md)) that any queue
or firmware can implement.

## Features

- **G-code/3MF processor.** Bambu Studio, OrcaSlicer and PrusaSlicer output, as
  `.gcode.3mf` or `.gcode`. Multi-plate files and multi-copy loops. MD5
  checksums are updated. Re-processing a processed file gives the same result.
- **Height map and sweep planner.** Built from extrusion moves, with the purge
  line skipped. Per-part lanes and push heights, travel that clears every part
  still standing, an optional full-plate wipe, and warnings for parts too low to
  sweep.
- **Material profiles.** Release temperatures and natural cooldown per polymer,
  using the strictest values across a multi-material plate. TPU and PVA are
  refused with an explanation (an override exists).
- **Signed manifests.** HMAC-SHA256 with your farm secret, stored inside the
  G-code and in `Metadata/clearloop.json`. `clearloop verify-manifest` catches
  edited or stale files.
- **LAN printer link.** MQTT over TLS (status, start, pause, resume, stop,
  `gcode_line`), implicit FTPS upload, and chamber camera (A1/P1 JPEG over TLS,
  X1/H2/P2 RTSPS through ffmpeg, or any HTTP snapshot URL).
- **Bed verification.** Brightness-normalised image differencing inside a region
  of interest, with blob detection and several reference images (door
  open/closed, light on/off). The difference image is saved for every check.
- **Controller.** Checks the plate before and after every job. Recovery sweep
  with configurable retries. Watchdogs for missed triggers, start timeouts,
  ejector stalls or timeouts, stale reports and disconnects. Handles HMS faults,
  printer self-pause (runout) and failed prints. Checks the plate between copies
  of multi-copy files. Warns when printer firmware changes.
- **Queue.** Priorities, pinning to a printer, model constraints, and matching
  on loaded AMS filament.
- **Plate wear.** Cycles and retries per plate, rated-life warnings, and
  release-degradation warnings.
- **Notifications.** ntfy (phone push), Discord, or any JSON webhook (Home
  Assistant, n8n and so on).
- **API and dashboard.** Fleet view, queue upload, events, stats (hands-off
  rate, first-try rate, missed triggers), snapshots, diff images, and plate and
  reference management. Role-based API tokens (viewer, operator, admin).
- **Processing API.** `POST /api/process` returns a loop-ready file, with cycle
  time, cost per part and warnings in response headers. Free and local.
- **Orders.** HMAC-verified Shopify `orders/create` webhook, and a generic
  `POST /api/orders` for Etsy, WooCommerce or eBay via n8n or Zapier. SKUs map
  to files and parts per plate.
- **Cost calculator.** Filament, energy, machine time and plate wear per part.
- **ClearBoard firmware.** Two-phase bend, stall and end-of-travel detection
  from motor current, door obstacle detection, quiet profile, and three trigger
  paths (HTTP, printer MQTT, switch) with a double-trigger lockout. Also an
  e-stop, a missed-trigger flag, persistent counters, a setup access point and a
  test page.

## What to verify on your machine

These Bambu behaviours come from community documentation, not from tests on
real printers. Each one has a check or a fallback:

| Assumption | Check / fallback |
|---|---|
| `M73 L<n>` is echoed back as `print.layer_num` over MQTT | `clearloop probe-marker`. If it fails, use `eject: gcode` or `custom` mode (no external trigger needed) and rely on camera verification. |
| `M190 R<t>` waits for the bed to cool | Set `cooldown_mode: time` if your firmware ignores it. |
| `project_file` with `file:///sdcard/<name>` starts an uploaded 3MF | `start_url_template` is configurable per printer. |
| `gcode_file` runs a plain `.gcode` (used for recovery sweeps) | Watch the first recovery. If it doesn't run, set `max_retries: 0` so leftovers go straight to a human. |
| Bed size and bend Z for P2S, X2D and H2 profiles (marked `verify`) | Override in `profile_overrides`. |

Pin printer firmware on farm machines. ClearLoop warns when it changes.

## Repository layout

```
src/clearloop/        Python package (processor, controller, printer link, vision, API)
  gcode/              height map, sweep planner, routines, processor
  printer/            Bambu MQTT/FTPS/camera, state model
  ejector/            hardware-agnostic ejector drivers
  static/             dashboard
firmware/clearboard/  ESP32 PlatformIO project
hardware/clearboard/  electronics reference design (CERN-OHL-S-2.0)
docs/                 architecture, trigger protocol, Bambu setup, comparison, hardware compatibility
examples/             annotated config
tests/                pytest suite (simulated printers, synthetic plates and camera images)
```

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check src tests
pio run -d firmware/clearboard      # firmware build
```

Contributions are welcome, especially validation reports from real printers,
new printer profiles, and ejector drivers.

## License

- Software and firmware: [GPL-3.0-or-later](LICENSE).
- Hardware design files: [CERN-OHL-S-2.0](hardware/LICENSE.md).

"Bambu Lab" and printer model names are trademarks of their owners. ClearLoop
is not affiliated with Bambu Lab or 3D Farmers. Comparisons refer to publicly
documented behaviour.
