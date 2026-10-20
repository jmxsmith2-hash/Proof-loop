# Architecture

```
            ┌────────────── clearloop serve ───────────────┐
 browser ──▶│ FastAPI (server.py) ── dashboard, REST, orders│
 Shopify ──▶│        │                                      │
            │      Farm (farm.py) ── config, notifier, store│
            │        │                                      │
            │  LoopController × N (loop.py)  ◀── tick 1 Hz  │
            │     │        │          │                     │
            │  Printer  Ejector    BedVerifier              │
            │  link     driver     (vision.py)              │
            └─────┼────────┼────────────────────────────────┘
                  │        │
    MQTT/TLS 8883 │        │ HTTP / MQTT / G-code only
    FTPS 990      │        ▼
    camera        │    ClearBoard, kit, plate changer, relay...
                  ▼
           Bambu printer (LAN-only + Developer Mode)
```

## Modules

| Module | Responsibility |
|---|---|
| `threemf.py` | Load and save `.gcode.3mf` and `.gcode`. Read filament types, grams, print time, chamber temperature and the printer model the file was sliced for. Updates the MD5 checksum on save. |
| `gcode/heightmap.py` | Turns extrusion moves into a 2 mm grid of maximum Z, then connected components become objects. Skips the machine start G-code. |
| `gcode/sweep.py` | Plans the sweep: per-object lanes and push height, front-first order, travel height, optional full-plate wipe, time estimate. |
| `gcode/routines.py` | Generates the delimited G-code blocks: start, loop, loop end, recovery. Overrides the bed temperature exactly. |
| `gcode/processor.py` | Analyse → plan → build the copies → manifest → container. |
| `protocol.py` | Marker encoding and decoding, and edge detection. |
| `manifest.py` | Embeds, extracts, signs and verifies manifests. |
| `printer/state.py` | Merges partial MQTT reports. Holds HMS, AMS filament types and firmware version. |
| `printer/bambu.py` | MQTT client, FTPS upload, print commands, camera frames. |
| `ejector/` | `prepare` / `eject` / `door` / `status` drivers: `gcode`, `custom`, `clearboard`/`http`/`webhook`, `mqtt`. |
| `vision.py` | Reference-image differencing and blob detection. |
| `loop.py` | Per-printer state machine. All the safety logic lives here. |
| `store.py` | SQLite: jobs, loops, events, plates, key/value. |
| `farm.py` | Builds the controllers from config. Prepares jobs and recovery sweeps. Runs the tick thread. |
| `server.py` | REST API, dashboard, processing API, order webhooks, token roles. |

## Controller state machine

| Phase | Entered when | Leaves when |
|---|---|---|
| `OFFLINE` | Start-up | First MQTT connection → `IDLE` |
| `IDLE` | Loop finished and verified, or an operator cleared the plate | A queued job matches. If the plate is not known to be clear, it is verified first (`VERIFYING`); otherwise → `STARTING`. |
| `STARTING` | Job claimed. Ejector `prepare()`, process, upload, start. | Printer reports `PREPARE`/`RUNNING` → `PRINTING`. Times out → `BLOCKED` and the job is re-queued. |
| `PRINTING` | Printing | `EJECT` marker → `EJECTING`. `FINISH` → `VERIFYING`, raising `trigger_missed` if the board never fired. |
| `EJECTING` | Bend cycle requested from the ejector | `FINISH` → `VERIFYING`. Ejector failure → printer paused, `BLOCKED`. |
| `VERIFYING` | After the settle time, a camera check | Clear → `IDLE` (job done, plate cycle counted). Not clear → `RECOVERING` while retries remain, otherwise `BLOCKED`. |
| `RECOVERING` | Recovery sweep uploaded and started | `FINISH` → `VERIFYING` |
| `BLOCKED` | Any fault | Operator presses "Plate cleared" (resumes a paused multi-copy print if there is one) or "Check plate" |

Connectivity is tracked separately from the phase, so a Wi-Fi drop doesn't lose
the loop's place. While disconnected the loop holds, and an event is raised.

**Invariants**

- A job is only started when `bed_clear` is true. Only a successful camera
  check or an explicit operator action sets it.
- Every transition to `BLOCKED` writes an `error` event, sends notifications
  and closes the loop record as `blocked`.
- Async steps (start, eject, verify, recovery) run off the MQTT thread under one
  re-entrant lock per printer. `_busy` stops the ticker from overlapping them.
- Open-loop mode (verification disabled) is opt-in. It still refuses to assume
  the plate is empty when `SWEEP_DONE` never arrived.

## Data

`data_dir/` contains `clearloop.db` (SQLite in WAL mode), `library/` (uploaded
files), `references/<printer>/ref-NN.jpg` and `diffs/<printer>-last-diff.png`.
