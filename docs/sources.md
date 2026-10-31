# Where every value comes from

ProofLoop doesn't invent printer or kit behaviour. Each fact it relies on is
listed here with its source. Values that can only be known on your own machine
are measured with *Loop files › Calibrate*. ProofLoop's own design choices (safety
margins and the like) are listed separately, so they're never confused with
facts.

## Sources

| Ref | Source |
|---|---|
| **BS** | Bambu Studio printer profiles, `resources/profiles/BBL/` in [bambulab/BambuStudio](https://github.com/bambulab/BambuStudio) (profile values below read at commit `79df7ae`, 2026-10-13) |
| **SP** | Bambu Lab's own spec pages: [A1](https://bambulab.com/en/a1/tech-specs), [A1 mini](https://bambulab.com/en/a1-mini/tech-specs), [A2L](https://bambulab.com/en/a2l/specs), [X1 Carbon / X1](https://bambulab.com/en/x1), [X1E](https://bambulab.com/en/x1e), [P1S](https://wiki.bambulab.com/en/p1/manual/unboxing-p1s_combo), [P1P](https://public-cdn.bambulab.com/store/bambulab-P1P-tech-specs.pdf), [P2S](https://bambulab.com/en/p2s/specs), [H2D](https://bambulab.com/en/h2d/tech-specs), [H2S](https://bambulab.com/en/h2s/tech-specs), [H2C](https://bambulab.com/en/h2c/specs), [X2D](https://bambulab.com/en/x2d/specs). Read 2026-10-30. |
| **EV** | Public FarmLoop P2S files in [Liaminamerica2/p2s-autofarm-playbook](https://github.com/Liaminamerica2/p2s-autofarm-playbook/tree/7b9a988963bf152c3839344d441e82bdcd96b924/evidence), commit `7b9a988`: a plain Bambu Studio 02.07.01.62 slice and the FarmLoop app 2.72 output of the same plate for FarmBoard firmware 2.94. Processed archive SHA-256 `e75259043c768a2f4a3a3804839996c645a31194f73ebb2dc0f124c368ea9f68` (matches the value published in the playbook's incident report). |
| **PB** | The same playbook's write-ups: `gcode-snippets/GCODE-HOWTO.md`, `FL_S2_INCIDENT.md`, `hardware-setup/README.md`. They record live runs on a P2S + FarmBoard. |
| **BD** | [maziggy/bambuddy](https://github.com/maziggy/bambuddy) source (AGPL-3.0): `backend/app/services/bambu_mqtt.py`, `camera.py`, `utils/threemf_tools.py`, `utils/printer_models.py` |
| **BW** | Bambu Lab Wiki, [Basic maintenance](https://wiki.bambulab.com/en/x1/maintenance/basic-maintenance) (X1 series). Read 2026-10-31. |
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
| `M190 R` on a P2S raised HMS 0300-0100-0003-0008 and drove the bed target up | ProofLoop never writes `M190 R`; the checker flags it | PB GCODE-HOWTO §11 |
| `M190 S<t>` holds while the bed cools (seen on a P2S); whether it holds all the way to `<t>` is unconfirmed | Cooldown wait, repeated the same number of times as FarmLoop's output; the Cooldown calibration test shows your printer's behaviour | PB §11; EV (60–62 repeats) |
| MQTT over TLS on 8883, user `bblp`, LAN access code; topics `device/<serial>/report` and `/request`; `pushall`, `get_version`, `pause`, `resume` payloads | Monitor | BD `bambu_mqtt.py` |
| HMS alert level is the high 16 bits of `code` (1 error, 2 warning, 3 notification); `code < 0x4000` is a status value | Monitor events | BD `bambu_mqtt.py` (follows Bambu Studio `DevHMSItem::parse_hms_info`) |
| HMS 0500-0500-0001-0007 = "MQTT command verification failed" (Developer Mode off) | Monitor tells you to turn on Developer Mode | BD `bambu_mqtt.py` |
| Files go onto the printer over FTPS with TLS from the first byte (port 990, user `bblp`, LAN access code), the data connection reusing the control connection's TLS session; a `.gcode.3mf` there is started with `{"print": {"command": "project_file", "param": "Metadata/plate_N.gcode", "url": "file:///sdcard/<file>", "use_ams", "ams_mapping", ...}}` | Sending to printers (optional, off by default) | Bambu Studio's LAN printing, as Bambuddy and other LAN tools use it. Not yet tried by ProofLoop on every model: watch the first send |
| A1, A1 mini, P1P, P1S camera: TLS on port 6000, 80-byte auth packet, 16-byte header + JPEG | Plate check | BD `camera.py` |
| X1, X2D, H2, P2S camera: RTSPS on port 322; Debian's GnuTLS ffmpeg drops P2S streams, so a local OpenSSL proxy is used | Plate check | BD `camera.py` |
| Printers announce themselves with SSDP on UDP port 2021 (multicast 239.255.255.250, `ST: urn:bambulab-com:device:3dprinter:1`), with `USN` = serial, `DevName.bambu.com`, `DevModel.bambu.com` = model id | *Find printers* | BD `discovery.py` |
| ntfy takes a picture as a PUT body with `Filename` and `Message` headers; Discord as multipart with `payload_json` + `files[0]` | Photo alerts | BD `notification_service.py` |
| Telegram `sendMessage` takes 1-4096 characters of text; `sendPhoto` takes a new photo as a `multipart/form-data` upload (up to 10 MB) with a caption of 0-1024 characters; the bot token goes in the URL path (`/bot<token>/<method>`) | Telegram notifications: photo with caption, text if the photo is refused, text cut to fit | [Telegram Bot API](https://core.telegram.org/bots/api) (`sendMessage`, `sendPhoto`, "Sending files") |
| Email goes out over SMTP with `STARTTLS` (usually port 587), implicit TLS (465) or none (25), then login and `send_message`; an `EmailMessage` takes the picture as an attachment | Email notifications (standard library `smtplib`, `email.message`) | [Python `smtplib`](https://docs.python.org/3/library/smtplib.html), [`email.message.EmailMessage`](https://docs.python.org/3/library/email.message.html); ports from [RFC 6409](https://www.rfc-editor.org/rfc/rfc6409) and [RFC 8314](https://www.rfc-editor.org/rfc/rfc8314) |
| Discovery topic `<discovery_prefix>/<component>/[<node_id>/]<object_id>/config` (default prefix `homeassistant`; node and object ids use only `[a-zA-Z0-9_-]`); a retained config message is read when Home Assistant starts; `availability_topic` with a last-will message marks entities unavailable | Home Assistant export: one device per printer, retained discovery configs, `proofloop/status` as availability | [Home Assistant MQTT integration, MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) |
| MQTT sensors take `device_class` (`enum` with `options`, `temperature`, `duration`), `unit_of_measurement`, `state_class`, `value_template`, `json_attributes_topic` / `json_attributes_template`; `enum` can't be combined with a unit or `state_class`; a binary sensor takes `payload_on` / `payload_off` and `device_class: problem` | Home Assistant export: sensor and binary sensor definitions | [MQTT sensor](https://www.home-assistant.io/integrations/sensor.mqtt/), [MQTT binary sensor](https://www.home-assistant.io/integrations/binary_sensor.mqtt/) |
| A batch's filament mapping comes from the file's first plate | Batches require the same filament slots, plate and nozzle | Inference from the 3MF layout (one `slice_info.config` per file), so ProofLoop refuses anything else |
| `Metadata/plate_N.gcode.md5` is upper-case MD5 hex; FarmLoop changes only the plate G-code and its checksum | File writing | EV (compared every archive member) |

## Printer profiles (design and G-code checks)

`src/proofloop/data/printers.yaml` holds one profile per printer for `proofloop check design` and
`proofloop check gcode`. The same Bambu models are recognised by the printer monitor. **Loop files
are still P2S-only**: the FarmLoop kit moves are published only for the P2S (see *Not published*
below), so these profiles don't make loop files for any other printer.

| Fact | Used for | Ref |
|---|---|---|
| `names` = the slicer's `printer_model` string: `Bambu Lab X1 Carbon`, `X1`, `X1E`, `P1S`, `P1P`, `A1`, `A1 mini`, `A2L`, `P2S`, `H2D`, `H2D Pro`, `H2S`, `H2C`, `X2D` (each with the `Bambu Lab ` prefix) | Picking the profile from a G-code file's header, and from a saved printer's model id | BS `machine/Bambu Lab <model> 0.4 nozzle.json` → `printer_model` |
| Build volume: X1 Carbon / X1 / X1E / P1S / P1P 256 × 256 × **250** (the slicer's `printable_height`; the spec pages say 256); A1 256 × 256 × 256; A1 mini 180 × 180 × 180; A2L 330 × 320 × 325; P2S 256 × 256 × 256; H2D 350 × 320 × 325 and H2S 340 × 320 × 340 (the slicer's printable area; the H2D page gives 325 × 320 × 325 for one nozzle); H2C 330 × 320 × 325 (page: 325 × 320 × 320 left nozzle, 305 × 320 × 325 right); X2D 256 × 256 × 260 (main nozzle, page value; the slicer has 261 for it, and the second nozzle reaches 235.5 × 256 × 256) | Fits-the-printer check, out-of-bounds moves in G-code | BS `printable_area`, `printable_height`, `extruder_printable_*`; SP "Build Volume" |
| Max nozzle temperature: 300 °C for X1 Carbon, X1, P1S, P1P, A1, A1 mini, A2L, P2S, X2D; 350 °C for H2D, H2S, H2C. The X1E page gives 320 °C; `bambu_x1` keeps the 300 °C shared with the X1 Carbon | Nozzle-temperature warnings | SP "Max Hot End / Nozzle Temperature" |
| Max bed temperature: 110 °C X1 Carbon / X1 (page: 110 °C at 220 V, 120 °C at 110 V) and P2S; 100 °C P1S, P1P, A1; 80 °C A1 mini, A2L; 120 °C H2D, H2S, H2C, X2D | Bed-temperature warnings | SP "Max Build Plate / Heatbed Temperature" |
| Enclosed: X1 Carbon, X1, X1E, P1S, P2S, H2D, H2S, H2C, X2D. Open frame: P1P, A1, A1 mini, A2L (their spec pages list no chamber) | Warp-risk warnings for ABS/ASA on open printers | SP; P1S page ("fully enclosed" chamber vs the P1P); H2/X2D/X1E pages list active chamber heating |
| Default acceleration `accel_mm_s2` = `default_acceleration` of the printer's "0.20mm Standard" process: A1, A1 mini, A2L 6000; H2D, H2C 8000; P2S, H2S, X2D, X1 Carbon (also used for X1, X1E, P1S, whose processes inherit it) and P1P 10000. Used only when the G-code doesn't set an acceleration | Time estimates | BS `process/0.20mm Standard @BBL <model>.json` |
| Hot-end flow, published maximum (ABS at 280 °C test): A1, A1 mini 28 mm³/s; A2L 28 (PLA at 220 °C test); X1 Carbon, X1, P1S, P1P 32; P2S, H2D, H2S, H2C, X2D 40 (standard hot end; 65 on the H2D / H2S / X2D with the optional high-flow hot end) | The ceiling for `max_flow_mm3_s` (below) | SP "Max Hot End Flow" / "Max Flow for Hotend"; P1P sheet (value printed apart from its label) |
| Hot-end flow the slicer allows with Bambu PLA Basic on the stock hot end: 21 mm³/s on A1, A1 mini, X1 Carbon, X1, P1S, X2D; 23 on A2L; 25 on H2D, H2S, H2C; 21 on P2S | The floor for `max_flow_mm3_s` (below) | BS `filament/Bambu PLA Basic @BBL <model>.json` → `filament_max_volumetric_speed`, first (standard hot end) entry |

### Printer design choices

| Setting | Value | Why |
|---|---|---|
| `max_flow_mm3_s` | A1, A1 mini, A2L 24; X1 family and P1S, P1P 24 (carried over); P2S 28; H2D, H2S, H2C, X2D 32 | Bambu publishes no single melt-rate figure for every material. ProofLoop picks a figure between the slicer's PLA rate on the stock hot end and the published ABS-test maximum, so sliced G-code is flagged before it starts under-extruding. It is ProofLoop's choice, not a Bambu spec |
| `volumetric_speed_mm3_s` | 15 for A1, A1 mini, A2L; 18 X1 family, P1S, P1P, P2S; 20 for the H2 series and X2D | Realistic average (travel included) for time estimates only. An estimate, not a spec |
| `layer_height`, `nozzle` | 0.2 mm, 0.4 mm | The slicer's default 0.20 mm Standard profile on the 0.4 mm nozzle |

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
* **Loop files for any printer but the P2S.** The kit moves (switch gestures, sweep path, push
  heights) are public only for the P2S. The other printers get design and G-code checks and the
  monitor, not loop-file output.
* **A hot end's safe sustained melt rate.** Bambu publishes only a one-material maximum (ABS or PLA
  test models) and per-filament slicer rates. `max_flow_mm3_s` is ProofLoop's own figure between
  them (see *Printer design choices*), and the X1 Carbon / X1 / X1E / P1S / P1P value of 24 is
  carried over from the earlier profile.
* **The X1E's own spec table** (bed, flow): the X1E page lists only its 320 °C hot end and 60 °C
  active chamber, so it shares the X1 Carbon profile.
* **A usable printing height of 256 mm on the X1 / P1 family.** The spec pages say 256 mm, Bambu
  Studio limits slicing to 250 mm; ProofLoop uses 250 mm.
* **Which nozzle printed a dual-nozzle (H2, X2D) file.** The profile uses the larger single-nozzle volume; a part
  that only fits one nozzle's reach isn't distinguished.
* **Your pusher's lowest safe height.** Measure it with the sweep-floor test.

## Filament facts

| Fact | Used for | Ref |
|---|---|---|
| `print.ams.ams[]` units hold `tray[]`; reports are partial, so units and trays merge by `id`; `tray_type == ""` means the slot was emptied | Live AMS view | BD `bambu_mqtt.py` `_handle_ams_data` |
| `tray_color` is `RRGGBBAA`; `remain` is a percentage, `-1` (or anything outside 0–100) means unknown; `humidity_raw` is relative humidity in % (the 1–5 `humidity` index is not used) | AMS view; unknown is shown as unknown | BD `bambu_mqtt.py`, `usage_tracker.py`, `utils/ams_humidity.py` |
| Global tray ids: AMS unit n slot s = n×4 + s; AMS HT units are 128–135 with one tray; the external spool is 254 (255 is a second one on dual-nozzle printers); `tray_now` 255 = nothing loaded | Tray names (A1…D4, HT-A, Ext) and the tray being fed | BD `bambu_mqtt.py`, `spoolman_tracking.py` |
| `tray_uuid` / `tag_uid` all zeros = no RFID tag read; periodic reports often carry zeros and must not overwrite a tag already read | RFID spools link themselves | BD `spoolman.py`, `bambu_mqtt.py` |
| The P2S (like the A1 and P1S) doesn't report which tray each slicer slot was mapped to | Which spool fed a loop is taken from `tray_now` during the loop (one-filament plates), or a single AMS tray with the same type and colour; otherwise you assign it | BD `spoolman_tracking.py` (`mapping` not published on A1/P1S/P2S) |
| Bambu slices use relative extrusion (`M83`); `M73 L<n>` marks layer n, the number the printer reports as `layer_num`; `T<n>` selects filament n + 1 | Filament used by a loop that stopped part-way: the net `E` sum up to the last finished layer | EV plain slice (its P2S `change_filament_gcode` uses `T[next_filament_id]`, a 0-based index); BD `threemf_tools.py` (counts layers the same way) |
| The net `E` sum, with the file's own `filament_diameter` and `filament_density`, equals the slicer's `used_g` | Confirms the part-way method | EV: 53.887 g from the G-code vs 53.89 g in `slice_info.config` (checked by `tests/loopfiles/test_evidence.py`) |
| `CONFIG_BLOCK` holds the slicer's filament preset per slot: `filament_settings_id`, `filament_vendor`, `filament_ids` (the AMS `tray_info_idx`), `filament_density`, `filament_diameter`, `filament_cost`, nozzle range, `filament_dev_ams_drying_temperature` / `_time` | Filling in a new spool from the plate; drying hints | EV plain slice |
| Spoolman: `GET /api/v1/spool`, `PUT /api/v1/spool/{id}/use` `{"use_weight": g}`, `PATCH /api/v1/spool/{id}` `{"remaining_weight": g}`; net weight = `initial_weight` else the filament's `weight`; empty spool = `spool_weight`, else the filament's, else the vendor's | Spoolman import and reporting | BD `services/spoolman.py`, `_spoolman_helpers.py` |

What ProofLoop doesn't take from these sources: Bambuddy's table of empty-spool
weights has no cited source, and its default density and spool weight are
fallbacks. ProofLoop uses neither. Empty-spool weights are yours to weigh, and
part-way usage needs the file's own density, or it isn't estimated.

## Filament design choices

| Choice | Why |
|---|---|
| A finished loop is charged the slicer's grams, not the G-code sum | They agree (above), and the slicer's number is the one you see in Bambu Studio |
| A part-way loop is charged only its finished layers | The layer in progress when it stopped is not counted; the count is a lower bound, never an overestimate |
| A new spool starts full only if you tick "new, unopened" | Otherwise what's left is unknown until you weigh it or type it |
| The next loop is held only when the count is known to be short | Unknown amounts never block a run. Linked spools in other AMS trays with the same type and colour count too, because the AMS switches to them |
| Several matching spools at the same price per kg price a plate without picking one | Whichever feeds it, the price is the same |

## Maintenance

Per-printer maintenance (*Printers › Maintenance*). The intervals are starting points you change in
the app. Only these two come from a published source, and it is the X1 page: Bambu's P2S pages weren't
readable, so check your own printer's guide.

| Task | Default | Ref |
|---|---|---|
| Clean the X-axis carbon rods | 30 days: the page says to check them for dust and buildup once a month | BW |
| Grease the Z-axis lead screws | 90 days: the page says to check and grease them every three months | BW |

Chosen by ProofLoop, with no source (a guess you should tune to your farm): clean the build plate every
25 prints, empty the purge chute every 50 prints, clean and check the nozzle every 100 print hours,
change the nozzle every 500, check belt tension every 300, clean and oil the rails every 200, check the
AMS desiccant every 30 days. A task counts as due at 100% of its interval and overdue from 125%; print
hours are the time from the start to the end of each recorded print.

## Costs

Every **rate** under *Loop files › Costs* (spool prices, electricity, watts, printer
price and life, labour, fees, tax, shipping) is yours to enter. ProofLoop ships
no prices and no power figures; a cost whose rate is missing is reported as
"not set" and left out of the total, which is then marked as a minimum.

The **quantities** come from your file and printer:

| Quantity | Where it comes from |
|---|---|
| Grams per filament slot | `slice_info.config` → `filament used_g`, as the slicer reports it |
| Print time | `slice_info.config` → `prediction` (the slicer's estimate) |
| FarmBoard waits, sweep and door time | The G-code ProofLoop adds, timed from its own dwells and moves |
| Cooldown time | Measured by the printer monitor on your printer (T1 → T2), else the cycle time minus sweep and door, else your own estimate, else "unknown" |
| Parts per plate | The file's object labels |

The cost formulas are plain arithmetic: wear = price ÷ life; failed loops add
cost ÷ (1 − failure rate) − cost; break-even price = (cost + per-order costs
per part) ÷ (1 ÷ (1 + tax) − fee % − payment %).

## ProofLoop design choices (not hardware facts)

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
