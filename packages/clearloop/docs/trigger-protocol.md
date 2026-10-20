# ClearLoop trigger protocol v1

This protocol lets processed G-code tell external hardware and software which
phase of a print loop it has reached. It works without a limit switch, special
file names, or custom printer firmware. It is published so that any farm queue,
ejector firmware or plate changer can implement it.

## Transport

Each loop phase is written as a progress marker that the printer reports over
its local MQTT broker:

```gcode
; CLEARLOOP_MARKER EJECT
M400            ; wait until every queued move has finished
M73 L9003       ; marker = base + code
G4 S2           ; hold so at least one status report carries it
```

The printer copies the `M73 L` value into `print.layer_num` in its
`device/<serial>/report` messages. A listener subscribes to that topic (TLS,
port 8883, user `bblp`, password = LAN access code) and decodes:

```
code = layer_num - base        (base defaults to 9000; configurable)
```

A marker counts as an event only when `layer_num` **changes** to a marker value.
Real layer numbers in between separate repeated markers in multi-copy files.

> Verify with `clearloop probe-marker` that your printer and firmware version
> echo `M73 L` values. Re-run it after every firmware update.

## Codes

| Code | Name | Sent | Listener should |
|---|---|---|---|
| 1 | `JOB_START` | First lines of each copy, **before** homing and bed levelling | Retract the bender fully, close the door, and arm for this print. A `retract_wait_s` dwell follows. |
| 2 | `DOOR_OPEN` | After any warp-safe natural cooldown | Open the door. Optionally start an external fan. |
| 3 | `EJECT` | With the plate at the bend position (`bend_z`) | Run the bend cycle. The printer dwells `bend_window_s` seconds. If the bend fails, **pause the printer** before the sweep starts. |
| 4 | `SWEEP_DONE` | After the toolhead sweep | Nothing required. Controllers use it as evidence that the sweep ran. |
| 5 | `LOOP_END` | Between copies inside a multi-copy file | Optional: pause the printer, verify the plate, then resume. |

## Rules for listeners

1. **Never fail silently.** If the print reaches `gcode_state = FINISH` after
   `JOB_START` but no `EJECT` arrived, report a missed trigger.
2. **Bend at most once per loop.** If you also have a switch trigger, lock out
   further triggers for a while after a cycle (ClearBoard defaults to 60 s).
3. **Do not arm from file names.** `JOB_START` is the only arming signal.
4. **A failed bend pauses the printer.** The sweep must not run against a plate
   that is still bent.

## Manifest

Processed files also carry a JSON manifest. It is a base64 line
`; CLEARLOOP_MANIFEST <b64>` at the top of the G-code, plus
`Metadata/clearloop.json` in 3MF files. Fields include `protocol`,
`marker_base`, `printer`, `eject_mode`, `copies`, `material`, `release_temp`,
`natural_cooldown_min`, `objects` (footprints and heights), `sweep_passes`,
`cycle_seconds_est`, `warnings`, `gcode_sha256` (the G-code hash, excluding the
manifest line) and `sig`. `sig` is an HMAC-SHA256 of the canonical JSON without
`sig`, keyed with the farm secret, or null.

## Block delimiters

Every injected block is wrapped in:

```
; >>> CLEARLOOP <NAME>
...
; <<< CLEARLOOP <NAME>
```

`NAME` is one of `START`, `LOOP`, `LOOPEND`, `COPY` or `RECOVERY`. Tools can
strip these blocks to recover the original slicer output, which is how
re-processing stays idempotent.
