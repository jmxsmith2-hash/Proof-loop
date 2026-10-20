# Ejection hardware compatibility

ClearLoop needs four things from ejection hardware: get out of the way before
a print, clear the plate, open or close the door, and report status. Set
`ejector.kind` per printer:

| Hardware | `ejector.kind` | How it works | Notes |
|---|---|---|---|
| **ClearBoard** (this repo) | `clearboard` | Controller calls `POST /api/cycle`, `/api/retract` and `/api/door` and gets back duration, peak current and stall status | Full health data. The switch and stand-alone MQTT are backups. |
| Tilt base + toolhead scraper (A1/A1 mini), frictionless or self-releasing beds, DIY toolhead pushers | `gcode` | No electronics. The geometry-aware sweep is the eject. | Large flat parts may not release without bending. You get a warning. |
| Switch-triggered bender kits in mechanical mode (e.g. FarmLoop Stage 2 in manual-trigger mode) | `custom` | Paste the kit's end G-code (bed move onto its switch plus a dwell) into `ejector.gcode` | Camera verification catches the case where the switch isn't reached. |
| Plate changers (SwapMod, INNOCUBE SwapMod A1, PrintFlow3D) | `custom` + `magazine_size` | Paste the changer's swap G-code. ClearLoop counts plates and blocks with "reload plates" when the magazine is empty. | The dashboard has a "Reload magazine" button. |
| Any device with an HTTP endpoint (Home Assistant script, Tasmota or Shelly relay, ESPHome) | `webhook` | Set `eject_url`, plus optionally `prepare_url`, `door_url` and `status_url`. Any 2xx response counts as success, or return the JSON fields below. | |
| Any MQTT-controlled device | `mqtt` | Publishes `{"id", "action": "cycle"\|"retract"\|"door", ...}` to `command_topic` and waits for `{"id", "ok", ...}` on `ack_topic` | |

## Response format for HTTP and MQTT ejectors

```json
{"ok": true, "message": "bend complete", "duration_s": 11.8, "peak_current_a": 1.9, "stalled": false}
```

All fields are optional. A missing `ok` counts as true for an HTTP 2xx
response. `stalled: true` always counts as a failure.

## Choosing `bend_window_s`

The printer waits `bend_window_s` seconds at the bend position before it sweeps.
Set it a few seconds longer than your slowest bend, for example the quiet
profile on a cold morning. If a bend takes longer than the window, ClearLoop
raises an `eject_slow` warning. If a bend fails, the printer is paused before
the sweep starts.
