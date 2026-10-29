# Printers: watched, counted, never started

The **Printers** tab watches your Bambu Lab printers on your local network. It never uploads or
starts a print: it reads status, checks the plate with the camera after each loop, resumes a loop
file that is waiting for that check, and records how every print ended. Add printers under
*Printers › Monitor*; the printer needs LAN-only mode with Developer Mode on
([setup guide](bambu-setup.md)).

## Overview

One screen with every printer's run (loop 7 of 10, done around 09:24 based on its measured loop
times), parts made today, this week and this month, stopped loops, filament used, spools running
low and anything waiting for you.

## Monitor

- **Find printers automatically.** *Find printers* listens for the printers' own network
  announcements and fills in the IP, serial and model. You still type the access code.
- **Live loop status.** For each printer: the loop number, the current phase (printing, cooling,
  bending, door), average cooldown and loop times measured on your printer, and a plate-bend counter
  you reset when you fit a new plate.
- **The plate check.** After each sweep of a loop file made with *Wait for the plate check*, the
  monitor compares a camera picture with your pictures of the empty plate and resumes the next loop
  only when it's empty. Anything else (parts left, no picture, a camera error, any other kind of
  pause) leaves the printer waiting with the heaters off and tells you. Take the empty-plate
  pictures here.
- **Quiet hours.** Plates are still checked overnight, but the next loop waits until morning, with
  the printer paused and its heaters off.
- **Notifications.** ntfy, Discord or any JSON webhook, each with its lowest level. When parts are
  left behind, the camera picture arrives with the ntfy or Discord notification. These same
  targets get a notice when a long check under *Check* or a website test has finished, and when
  ProofLoop stops or restarts.

### Print history

The monitor records how every print on your printers ended, not only loop files: finished, failed
or cancelled, the layer it got to, the printer's HMS alerts and error code, the nozzle and bed
temperatures, the AMS trays it fed from, and whether the plate check found parts left behind. When
a print fails or parts are left, the camera picture is kept with it. Loop files ProofLoop makes are
kept too (the last 100, in `~/.proofloop/files/`), so the exact G-code that ran can be checked
afterwards. See it under *Monitor › Print history*, or run `proofloop printers history` (`--json`
for everything). *Check* learns from it which flows, first layers and files fail on each printer
([more](check.md#learning-from-your-printers-and-from-you)).

## Filament

*Filament* keeps track of every spool you own:

- **Your spools**: material, colour, brand, price paid, filament when new, the empty-spool weight,
  what's left, where it's kept, when it was opened and dried, and notes. Each has a fill bar, its
  value, and how many days it lasts at the rate you've been using it. Search, filter and sort them;
  buy another of the same spool in one click, or print a spool label.
- **What's in each AMS**, live from the printer: type, colour, how full the AMS thinks each spool
  is, the tray being fed, humidity and temperature. Bambu spools link themselves from their RFID
  tag. Link any other spool by picking it for its tray, or add a tray's spool to your inventory in
  one click.
- **Every loop is counted.** When the monitor sees a loop of a file this ProofLoop made finish, its
  filament (the slicer's grams per slot) is taken off the spool that fed it. A loop that's
  cancelled or fails part-way is charged what its finished layers used, worked out from the file's
  own G-code. If ProofLoop can't tell which spool fed a loop (two trays with the same colour,
  neither linked), the grams wait in a list for you to assign. Nothing is charged to a guess, and
  files ProofLoop didn't make are never charged.
- **Weigh-ins.** Enter the empty-spool weight once, then type the scale reading. You can also set
  what's left directly, or log filament used outside ProofLoop. Every entry can be undone.
- **No run comes up short.** With the plate check on, a loop-end pause **stays waiting** when the
  spool can't cover the next loop, and you get an alert.
- **Low-stock alerts and a shopping list.** Set a warning weight per spool, or one for all spools.
  The shopping list shows what's low, plus anything your last 14 days of use would empty within two
  weeks.
- **Your slicer's preset, read from the file**: preset name, vendor, density, diameter, nozzle
  range and Bambu Studio's AMS drying settings fill in a new spool in one click.
- **Usage history**: a chart of grams per day by material, and a log of every loop, weigh-in and
  correction. Export spools and usage as CSV, or list them with `proofloop printers spools`.
- **Spoolman.** Import your spools from a Spoolman server, and optionally report each loop's usage
  and every weigh-in back to it.
- **Costs use your real spools.** Filament cost per part under *Loop files › Costs* comes from what
  you paid for the spool that feeds the plate.

## Backup and restore

One file with your settings, imported profiles, spools, usage, production log and measured
timings (*Printers › Filament › Backup*).

## On your phone, and on an always-on machine

- **Phone:** *More › Phone* in the window pairs a phone on your Wi-Fi to *Loop files* and
  *Printers* (see the [README](../README.md#on-your-phone)).
- **Always on:** `proofloop printers serve --host 0.0.0.0` (or `docker compose up -d`) serves
  *Loop files* and *Printers* alone on port 8484, for a NAS or mini PC that watches the printers
  around the clock. Set `app_token` in `settings.json` to require an access token. Run only one
  ProofLoop per farm, so the printers are watched once.
