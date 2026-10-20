# ClearBoard firmware

ESP32 firmware for the [ClearBoard](../../hardware/clearboard) ejector
controller. It is also a reference implementation of the
[ClearLoop trigger protocol](../../docs/trigger-protocol.md).

## Build and flash

```bash
pip install platformio
pio run -d firmware/clearboard -t upload        # ESP32-S3 DevKitC-1 (default env)
pio run -d firmware/clearboard -e esp32-dev -t upload   # classic ESP32
pio device monitor -b 115200
```

## First setup

1. On first boot (or when Wi-Fi fails) the board opens an access point called
   `ClearBoard-xxxx`, password `clearboard`. Its address is `192.168.4.1`.
2. Configure it:
   ```bash
   curl -X POST http://192.168.4.1/api/config -H 'Content-Type: application/json' -d '{"ssid":"farm","password":"secret","token":"long-random-token"}'
   curl -X POST http://192.168.4.1/api/restart -H 'Authorization: Bearer long-random-token'
   ```
3. Point ClearLoop at it: `ejector: {kind: clearboard, url: http://<board-ip>, token: long-random-token}`.

## Trigger paths

| Path | Enable | Use |
|---|---|---|
| HTTP `POST /api/cycle` | Always on | Primary, when the ClearLoop controller runs |
| Printer MQTT markers | `{"standalone": true, "printer_ip": "...", "printer_serial": "...", "access_code": "..."}` | No controller: the board follows `JOB_START` / `DOOR_OPEN` / `EJECT` itself and flags missed triggers |
| Limit switch on GPIO16 | Wire it | Backup if a marker is lost |

After a bend, further triggers are ignored for `lockout_ms` (60 s by default),
so the plate is never bent twice in one loop.

## API

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/status` | — | Firmware, network, armed/missed-trigger/e-stop flags, live current, counters, last result |
| POST | `/api/cycle` | `{"profile": "standard"\|"quiet", "force": false}` | `{"ok", "stalled", "message", "duration_s", "peak_current_a", ...}` (HTTP 500 on failure) |
| POST | `/api/retract` | — | Retracts the bender, closes the door, arms for a new print |
| POST | `/api/door` | `{"open": true}` | Opens or closes the door, with obstacle back-off |
| POST | `/api/config` | Any of: `ssid`, `password`, `token`, `printer_ip`, `printer_serial`, `access_code`, `standalone`, `door_fitted`, `full_travel_ms`, `stall_amps`, `stall_ms`, `end_amps`, `amps_per_volt`, `door_travel_ms`, `door_stall_amps`, `marker_base`, `lockout_ms`, `fan_seconds` | `{"ok", "restart_required"}` |
| POST | `/api/estop/reset` | — | Clears the e-stop latch once the button is released |
| POST | `/api/restart` | — | Reboots |

When a token is set, every POST needs `Authorization: Bearer <token>`.

## LED codes

| Pattern | Meaning |
|---|---|
| Solid | Ready |
| Slow blink (1 s) | No Wi-Fi / setup access point |
| Fast blink | Stand-alone mode, printer MQTT not connected |
| Triple blink | Needs attention: e-stop, stall, failed cycle or missed trigger (see `/api/status`) |

## Bend profiles

| Profile | Duty | Soft start | Sequence |
|---|---|---|---|
| `standard` | 100 % | 150 ms | 55 % bend → relax 25 % → full flex → hold 0.8 s → retract |
| `quiet` | 60 % | 900 ms | Same sequence, slower and gentler. Used during quiet hours. |

PWM runs at 20 kHz, above the range of hearing. Most of the remaining noise is
mechanical, from the actuator gearbox, and the quiet profile reduces it.
