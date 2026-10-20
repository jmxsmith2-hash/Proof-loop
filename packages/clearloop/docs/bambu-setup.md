# Bambu Lab printer setup

ClearLoop talks to printers only over your local network. To start jobs and
read status from third-party software after Bambu's January 2025 authorization
change, each printer needs:

1. **LAN-only mode** on (printer screen → Settings → Network/WLAN).
2. **Developer Mode** on (in the same menu on recent firmware). The P2 series
   ships with it available. Developer Mode opens MQTT (8883), FTPS (990) and the
   camera stream to clients that know the access code.
3. Note the **IP address**, **serial number** and **LAN access code** from the
   printer screen.

Trade-offs to know about:

- In LAN-only and Developer Mode the printer can't use Bambu Cloud. Bambu Handy
  and remote cloud printing stop working for that printer. Bambu describes
  Developer Mode as unsupported.
- Give printers fixed IP addresses (DHCP reservations) so the config stays
  valid.
- A1 and P1 printers need a microSD card for LAN printing.

## If you can't use Developer Mode

Use **processor-only mode**. Run `clearloop process` with `--eject gcode` or
`--eject custom` and send files with Bambu Studio or Handy. Every routine runs
from G-code inside the job, so it works in any network mode. You lose the
closed loop (no plate verification, no automatic queue), and you should plan on
supervising more.

## Per-printer validation checklist

Run once per printer, and again after every firmware update:

1. `clearloop probe-marker --config config.yaml <name>` must print **OK**.
2. `clearloop snapshot --config config.yaml <name> -o test.jpg`. Check that the
   plate is visible, then adjust `verify.roi` so it covers only the plate.
3. With the plate **empty**, run `clearloop reference --config config.yaml <name>`.
   Repeat with the door open and with the chamber light off, so lighting changes
   are covered.
4. Put a small part on the plate and run
   `clearloop check-bed --config config.yaml <name> --diff diff.png`. It must
   report `"clear": false`.
5. Run one supervised loop with a cheap PLA part. Watch the cooldown, bend and
   sweep, and confirm the dashboard shows `loop_done`.
6. Pin the printer firmware. ClearLoop raises `firmware_changed` if it moves.
