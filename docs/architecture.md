# Architecture

ProofLoop is one Python package, `proofloop`, in `src/proofloop`, served by one web server. Its
parts match the window's tabs:

```
                  ┌──────────────────────────── proofloop.app ─────────────────────────────┐
                  │ shell.py: the window's server (127.0.0.1:8600)   service.py: background │
                  │ tray.py, autostart.py, phone.py (:8601), doctor.py, update.py, window.py│
                  └──────────┬──────────────────────────────────┬──────────────────────────┘
             checks.py: Check (/check)           loop_server.py + loop_routes.py:
                             │                   Loop files + Printers (/loop, API at /)
             ┌───────────────┴───────────┐              ┌─────┴──────────────┐
      proofloop.check            proofloop.personas   proofloop.loopfiles   proofloop.monitor
      CAD, G-code, slicing,      (who holds the part, make + check loop     printers, plate check,
      learning, predictions      product predictions) files                 history, filament, costs
             └──────────────────────── proofloop.core ──────────┴────────────────────┘
          paths (~/.proofloop), config (YAML), settings (settings.json), db, llm, notify, theme
```

- **Plates move between tabs** through the window: `/proofloop/to-loop` (a checked plate opened in
  *Make a loop file*) and `/proofloop/to-check` (a plate from *Make a loop file* given the full
  print check). Only the window may call them, or upload to Check (`X-ProofLoop` header and its own
  origin), and host names other than `127.0.0.1` and `localhost` are refused, which stops
  DNS-rebinding attacks. Every tab is on the same origin, so the window frames them all.
- **Loop files and Printers are one page** (`app/static/loop.html`); the window tells it which tab
  is showing (`postMessage {proofloop: "part"}`), and a link to a section of the other tab asks the
  window to switch.
- **Check reads what the monitor records**: `check/learn.py` reads `printers.db` (read-only) and the
  loop files in `files/`; `check/my_printers.py` reads your printers from `settings.json`.
- **The G-code check and the loop-file checks call each other**: `check/gcode/checks.py` runs
  `loopfiles/lint.py` on a loop file's ejection cycle, and `loopfiles/print_checks.py` runs the
  G-code check on the print in each loop.
- **One look**: `core/theme.css` is put inline into every page and report by `core/theme.py`.
- **One shape for results**: `core/output.py` (JSON and exit codes) and `check/summary.py` (findings
  with stable ids) are what `--json` prints and what `proofloop mcp` returns. The MCP server runs each
  check as a `proofloop ... --json` command, and asks the running ProofLoop for live state.

## Folders

| Package | Job |
|---|---|
| `core/` | `paths.py` (`~/.proofloop`, `PROOFLOOP_HOME`), `config.py` (built-in YAML in `data/` with yours in `config/` over it), `settings.py` (`settings.json`: printers, notifications, loop-file defaults, costs, filament), `db.py` + `migrations/` (`results.db`), `llm.py` + `cost.py` (optional model engine for personas and predictions), `notify.py` (ntfy, Discord, webhooks), `printers.py` (Bambu model ids), `theme.py`, `commands.py` (shared by the `check` commands). |
| `check/` | `cad/` (mesh loading incl. 3MF, voxel/print checks, fine detail, auto-eject, moulding and CNC, renders, ergonomics, personas, print iteration), `gcode/` (parser for plain and Bambu `.gcode.3mf`, checks, layer previews, slicer-setting fixes), `slicer.py` + `prepare.py` (headless slicing and the print-ready verdict), `learn.py` (from the print journal), `feedback.py` (false alarms), product predictions (`inputs.py`, `reviews.py`, `evaluate.py`, `sensors.py`, `guardrails.py`), `templates/` (reports). |
| `loopfiles/` | See below. |
| `monitor/` | See below. |
| `personas/` | people who differ in hand size, grip, body size, motor control and eyesight: traits, deliberate design, generation, diversity (used by the design check and product predictions). |
| `app/` | the window's server (`shell.py`) with Check (`checks.py`) and Loop files & Printers (`loop_server.py`, `loop_routes.py`) on it; the first-run guide (`guide.py`); backup and restore (`backup.py`); assistants over MCP (`mcp.py`); the pretend farm (`pretend_farm.py`); background service, tray, start at login, phone access, setup check, updates; the pages (`static/`, `templates/`). |
| `data/` | built-in YAML (settings, models, printers, processes) and reference data (body sizes, product checklists). |

## Loop files

```
 .gcode.3mf ──▶ threemf.py ──▶ gcode/objects.py ──▶ gcode/sweep.py ──▶ gcode/routines.py ──▶ FL_S2_….gcode.3mf
  (plain slice)  facts from     parts: footprint +    per-part push     cycle from the
                 the file       height from labels    heights on the    hardware profile
                                                      kit's own lanes
                                     ▲
                 profiles.py ────────┘   built-in (profile_data/*.json) or imported (importer.py)
```

| Module | Job |
|---|---|
| `threemf.py` | Load and save `.gcode.3mf`. Everything comes from the file itself: `printer_model_id`, printable area and height, filaments, object names and heights. Writes back only the plate G-code and its checksum. |
| `gcode/objects.py` | Footprint and height of each labelled object, cross-checked against the slicer's header. |
| `gcode/sweep.py` | Push heights per lane, the travel height, warnings and problems, and sweep time capped by the printer's own speed limits. |
| `gcode/routines.py` | Builds the end-of-print cycle and the start step from the profile, each block between `; >>> PROOFLOOP` markers. |
| `gcode/processor.py` | Analyse (preview data, steps, checks) and process (loops, output name). Refuses FarmLoop output; re-processing its own output gives the same result. |
| `gcode/usage.py` | Filament used up to each layer, from the plate's own G-code. |
| `profiles.py` | Hardware profile model and store. |
| `importer.py` | Learns a profile by diffing a plain slice against FarmLoop's version of it. |
| `lint.py` | Safety checks for any loop file. |
| `print_checks.py` | The G-code check of the print in each loop. |
| `calibration.py` | Guided test prints (sweep floor, cooldown, door, bend). |
| `api.py` | Make a loop file with your saved settings (Check's *Make it now*, `check prepare --loops`). |

## Printers

```
 printer (LAN MQTT + camera) ──▶ printer/bambu.py ──▶ watch.py ──▶ resume, or keep waiting + notify
```

| Module | Job |
|---|---|
| `service.py` | The running monitor: settings, profiles, uploaded plates and watched printers (`App`). |
| `printer/state.py`, `printer/bambu.py`, `printer/ams.py` | MQTT status, pause/resume, camera frames; AMS trays from MQTT reports (partial updates merged). |
| `watch.py` | Recognises loop-end pauses, checks the plate, resumes only when it's empty. |
| `vision.py` | Empty-plate comparison against reference pictures. |
| `journal.py` | Print journal: how every print ended (layer, HMS, temperatures, plate check, picture); keeps the files ProofLoop makes. |
| `filament.py` | Spool inventory, usage log, weigh-ins, which spool feeds a slot, how many loops your spools cover. |
| `tracking.py` | Remembers the files ProofLoop makes; charges finished and part-way loops to spools; holds a loop the spools can't cover; links RFID spools to AMS trays. |
| `spoolman.py` | Optional Spoolman import and usage reporting. |
| `costing.py` | Cost per part, loop and run, selling price, fees and capacity, from your own rates and the file's grams and times. Unset rates are reported, never filled in. |
| `store.py` | `printers.db`: events, outcomes, production, timings. |
| `send.py` | Sending to printers (opt-in): what must be true first, the filament-to-tray mapping, the FTPS upload and the `project_file` command. |
| `queue.py` | Spreading a run over the printers that can print it, and the queue of jobs (`queue/`): each job followed on its printer, with a notice when it ends and when a printer comes free. |
| `summary.py` | What happened on the printers (the Overview's summary, `proofloop summary`, the daily notice). |
| `printer/pretend.py` | A pretend printer speaking the Bambu LAN protocol on localhost, for the tests and `proofloop try`. |

## The monitor's rule

A pause is treated as a loop-end pause only when all of these hold:

1. The printer reports a loop file (`FL_S1_`/`FL_S2_` in the job name or file path, and not a
   `CALIBRATION` file).
2. The last layer is done (`layer_num >= total_layer_num`).
3. The state is `PAUSE`.

Then the monitor checks the plate. It sends `resume` only if the camera shows an empty plate
against at least one reference picture. In every other case (no picture, camera error, parts found,
any other kind of pause) it does nothing except tell you. Because the file itself makes the printer
wait, doing nothing is always the safe outcome.

## The background service

`proofloop service` (what the shortcut and login start) runs one process holding everything: the
window's server (with Check, Loop files and Printers on it), the monitor, and phone access when
it's on. The window is only a view: closing it leaves the service running. The service writes
a heartbeat to `~/.proofloop/running.json` every minute; if that file is still there at the next
start, the last run ended unexpectedly, and the service says for how long the printers went
unwatched. It sends a notice when it stops (waiting for the notice to be delivered before the
process ends) and restarts the window's server if it dies on its own. Double-clicking the shortcut
while ProofLoop is running opens its window instead of starting a second copy.

**Phone access** is a second listener on the local network (port 8601) that serves the Loop files &
Printers page and API only. A phone pairs with the QR code (a link holding a long secret) or the
six-digit code, and then carries an HttpOnly, SameSite=Lax cookie. Five wrong codes in ten minutes
lock out typing codes; *Forget phones* makes new secrets.

**Start at login**, for your user only: a shortcut in the Startup folder on Windows (pythonw, so no
console), a LaunchAgent on macOS that restarts it after a crash, an XDG autostart entry on Linux.
