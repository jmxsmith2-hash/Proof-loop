# Where every value comes from

ClearLoop doesn't invent printer or kit behaviour. Each fact it relies on is
listed here with its source. Values that can only be known on your own machine
are measured with the Calibrate tab. ClearLoop's own design choices (safety
margins and the like) are listed separately, so they're never confused with
facts.

## Sources

| Ref | Source |
|---|---|
| **BS** | Bambu Studio printer profiles, `resources/profiles/BBL/` in [bambulab/BambuStudio](https://github.com/bambulab/BambuStudio) |
| **EV** | Public FarmLoop P2S files in [Liaminamerica2/p2s-autofarm-playbook](https://github.com/Liaminamerica2/p2s-autofarm-playbook/tree/7b9a988963bf152c3839344d441e82bdcd96b924/evidence), commit `7b9a988`: a plain Bambu Studio 02.07.01.62 slice and the FarmLoop app 2.72 output of the same plate for FarmBoard firmware 2.94. Processed archive SHA-256 `e75259043c768a2f4a3a3804839996c645a31194f73ebb2dc0f124c368ea9f68` (matches the value published in the playbook's incident report). |
| **PB** | The same playbook's write-ups: `gcode-snippets/GCODE-HOWTO.md`, `FL_S2_INCIDENT.md`, `hardware-setup/README.md`. They record live runs on a P2S + FarmBoard. |
| **BD** | [maziggy/bambuddy](https://github.com/maziggy/bambuddy) source (AGPL-3.0): `backend/app/services/bambu_mqtt.py`, `camera.py`, `utils/threemf_tools.py`, `utils/printer_models.py` |
| **FL** | 3D Farmers' FarmLoop documentation and blog (help.3d-farmers.com, 3d-farmers.com/blogs), as quoted in the research this project started from and in PB |

## Printer facts

| Fact | Used for | Ref |
|---|---|---|
| `printer_model_id` values (`N7` = P2S, `C12` = P1S, `N2S` = A1, ...) | Recognising which printer a file was sliced for | BS `machine/<printer>.json` → `model_id` |
| P2S printable area 256 × 256 mm, height 256 mm | Read from each file's own `CONFIG_BLOCK` (`printable_area`, `printable_height`); never hard-coded | BS `fdm_bbl_3dp_001_common.json`, `Bambu Lab P2S 0.4 nozzle.json` |
| Bambu's pause command is `M400 U1` | Pause between loops for the plate check; pauses in calibration tests | BS `fdm_bbl_3dp_001_common.json` → `machine_pause_gcode` |
| The P2S stock end G-code finishes with `M18` (steppers off) | `M17` before any move in the cycle (added only when the file's own end G-code ends with M18/M84) | BS P2S `machine_end_gcode` template; EV plain slice |
| P2S fans: `P1` part, `P2` "remote part cooling" (aux), `P3` chamber (exhaust), `P10` left aux | Exhaust-fan cooldown option (P2S only); natural cooldown reuses the file's own fan-off lines | BS P2S end template; PB |
| G-code after `; EXECUTABLE_BLOCK_END` is not executed | The cycle is inserted just before that marker | BD `threemf_tools.py` (verified on a P1S); PB runs the same placement on a P2S |
| `M190 R` on a P2S raised HMS 0300-0100-0003-0008 and drove the bed target up | ClearLoop never writes `M190 R`; the checker flags it | PB GCODE-HOWTO §11 |
| `M190 S<t>` holds while the bed cools (seen on a P2S); whether it holds all the way to `<t>` is unconfirmed | Cooldown wait, repeated the same number of times as FarmLoop's output; the Cooldown calibration test shows your printer's behaviour | PB §11; EV (60–62 repeats) |
| MQTT over TLS on 8883, user `bblp`, LAN access code; topics `device/<serial>/report` and `/request`; `pushall`, `get_version`, `pause`, `resume` payloads | Monitor | BD `bambu_mqtt.py` |
| HMS alert level is the high 16 bits of `code` (1 error, 2 warning, 3 notification); `code < 0x4000` is a status value | Monitor events | BD `bambu_mqtt.py` (follows Bambu Studio `DevHMSItem::parse_hms_info`) |
| HMS 0500-0500-0001-0007 = "MQTT command verification failed" (Developer Mode off) | Monitor tells you to turn on Developer Mode | BD `bambu_mqtt.py` |
| A1, A1 mini, P1P, P1S camera: TLS on port 6000, 80-byte auth packet, 16-byte header + JPEG | Plate check | BD `camera.py` |
| X1, X2D, H2, P2S camera: RTSPS on port 322; Debian's GnuTLS ffmpeg drops P2S streams, so a local OpenSSL proxy is used | Plate check | BD `camera.py` |
| `Metadata/plate_N.gcode.md5` is upper-case MD5 hex; FarmLoop changes only the plate G-code and its checksum | File writing | EV (compared every archive member) |

## FarmLoop hardware facts

| Fact | Used for | Ref |
|---|---|---|
| FarmBoard (digital mode) reacts to `nozzle_target_temper` = 1–5, set with `M104 S1`…`S5`: reset, T1 door + fan, T2 bend, safety check, T3 door close | Copied verbatim into loop files; the monitor reads the same field | EV; PB GCODE-HOWTO §1 |
| P2S switch gestures: press at `Z255`, release at `Z243`, park at `Z220`, with the tap patterns and waits in the profile | Copied verbatim (mechanical-mode fallback, exactly as FarmLoop emits them) | EV; PB §2 |
| Start step position: right after the start template's date line, before heating | Same position | EV |
| The door closes on T3 even when T2 and the safety check are skipped | "Bender fitted" switch | PB §9, live run 2026-09-20 |
| P2S sweep path: lanes X125 ×2, then X220…X30 every 30 mm at F3000, the same again at F12000; pushes from Y250 to Y0; park `G1 X65 Y245`, `G1 Y265` | Kept exactly; only push heights change | EV |
| FarmLoop push height = "maximum part height minus 10 mm" | Applied per part instead of per plate | FL (Detachment Tuning text, quoted in PB §9); EV: 170 mm part → Z160 |
| One push height misses short parts on mixed plates | The reason for per-part heights; the checker detects it | FL blog; EV: a 5 mm part next to a 170 mm part, swept only at Z160 |
| Multi-loop files repeat the whole plate (header, config, executable block) per loop | Same layout | EV two-loop file |
| FarmLoop app 2.72 placed a 120-minute natural-cooldown wait before the print, heaters on | Never copied; the importer drops it; the checker flags it | PB FL_S2_INCIDENT.md; EV |
| Default cooldown target 30 °C; natural cooldown 1–120 min for warp-prone ASA/ABS/PC; rigid materials only | Defaults and warnings | FL |
| FarmBoard kits key off the `FL_S2_` file-name prefix; don't rename processed files | Output names keep the prefix | FL; PB |
| Digital mode needs LAN-only mode + Developer Mode; the access code changes when you toggle them | Setup guidance | PB hardware-setup |

## Not published, so not guessed

* **The P2S Stage 1 kit's own G-code** (and any Stage 1 bend step) isn't public.
  The built-in "sweep only" profile uses the published P2S sweep path. To use
  your Stage 1 kit's exact moves, import one of your `FL_S1_` files.
* **Other printers' FarmLoop moves.** Import a FarmLoop file for your printer.
* **Your pusher's lowest safe height.** Measure it with the sweep-floor test.

## ClearLoop design choices (not hardware facts)

| Setting | Default | Why |
|---|---|---|
| Push depth | 10 mm, adjustable 0–50 mm; per-part heights can be set too | FarmLoop's rule is the default; you can change it per plate or per part. Every height is clamped to the sweep floor, and one that doesn't reach below a part's top blocks the file. |
| Sweep floor | Z2 until calibrated | FarmLoop's own rule reaches Z2 on a 12 mm part, the shortest part the P2S playbook build treats as safe. The app asks you to calibrate it. |
| Travel height | tallest part + 5 mm, capped at the printable height | Sideways moves always clear every part |
| Rear clearance | 10 mm | Parts closer than this to the sweep's start line are refused: the pusher drops there |
| Tier merging | 1 mm | Push heights closer than this are swept once, at the lower height |
| Empty lanes | Swept at the plate's tallest push height | Matches FarmLoop's single-height behaviour, so coverage is never lower |
| Checker lane width | ±15 mm | Half the vendor's 30 mm lane spacing |
| Plate check | 3 s settle, 3 camera attempts, thresholds in Monitor settings | Tunable per printer |
