# Printers: watched, counted, started only when you say

The **Printers** tab watches your Bambu Lab printers on your local network: it reads status, checks
the plate with the camera after each loop, resumes a loop file that is waiting for that check, and
records how every print ended. It starts a print only when you send one and confirm it (see
[Sending to printers](#sending-to-printers)); nothing starts by itself. Add printers under
*Printers › Monitor*; the printer needs LAN-only mode with Developer Mode on
([setup guide](bambu-setup.md)).

## Overview

One screen with every printer's run (loop 7 of 10, done around 09:24 based on its measured loop
times), parts made today, this week and this month, stopped loops, filament used, spools running
low and anything waiting for you.

## While you were away

The Overview opens with a summary of the last 12 hours (or 24, or 7 days): parts and loops made per
printer, prints that failed with the layer and the printer's HMS alerts, plates where parts were left,
what is waiting for you now, filament used and spools running low, and where the queue stands. Tick
*Send it to me every day at* and choose a time: it then goes to all your notification targets once a
day, covering the time since the last one. `proofloop summary` prints it (`--json` for programs).

## Orders

*Printers › Orders* keeps what people asked for: a customer, how many parts of a sliced plate, a due
date and, if you like, a price per part. Taking an order plans its run over the printers that can
print the plate and puts it in the queue (nothing starts by itself). From then on each order shows
how many parts are made, when it will be ready (each printer's remaining loops at its own measured
loop time) and whether that's before the due date, with what it earns and costs (from your prices
under *Costs*). It's marked made when its last job finishes, and you're told. Late orders show up in
the summary under what needs you. `proofloop printers orders` lists them (`--json` for programs),
and an assistant can take and read orders too ([with Claude](assistants.md)).

**Paid, and who owes you.** Mark an order paid or unpaid; the *Customers* table lists each customer's
orders, parts, what the orders you've made earned and how much of that is still unpaid.

**Invoices.** Fill in your business name, address, email, tax ID and an invoice prefix once (*Orders ›
Invoices*), then press **Invoice** on an order: a page opens to print or save as PDF from your
browser. The first time gives the order its number (the prefix and a count that goes up by one, such
as `INV-0001`); the number and date are kept with the order and never change, even if you change the
prefix later. It shows the order's parts, price per part and total, the tax included in your price if
you've set one under *Costs*, the order's notes and, once marked, that it's paid. Anything you haven't
filled in is left as a blank to write in, never made up.

**Spreadsheets.** Download orders, print history, and production and costs as CSV that Excel and
Sheets open directly (UTF-8, dates as `2026-10-30`, plain numbers with the currency in its own
column). Names and notes that start with `=`, `+`, `-` or `@` get a `'` in front so they can't run as
a formula. *Orders* and *Print history* have a download link; *Costs › What you've made* takes a date
range and one row per order, day or month: loops, parts, filament grams of finished loops, print hours,
and cost, price and margin for the parts that belong to an order (from that order's price and cost per
part; left blank where an order has none). From the command line:
`proofloop printers export orders|history|costs [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--by order|day|month]
[--out file.csv | --json]`.

## Queue

*Plan a production run* (under *Loop files › Make a loop file*) can spread the loops you need over
the printers that can print the plate: the model it was sliced for, the filament loaded (each slot
matched to an AMS tray of the same type and colour), and connected. Each loop goes to the printer
that would finish it first, counting when the printer is free (the time left on its current print)
and its own loop time (the plate's print and fixed steps, plus the cooldown measured on that
printer), so the printers finish at about the same time. A run of more than 50 loops on one printer
becomes several files. Printers left out say why.

**Keep it as a queue** stores the plan with a copy of the plate. The *Queue* card on the Overview
lists each job and where it is: waiting, sent, printing (and which loop), done, or stopped. A job
you sent or downloaded is followed on its printer from what the printer reports, so you don't mark
anything yourself: it's done when the file finishes, and stopped (with why) when it fails, is
cancelled, or a sent file doesn't start within 10 minutes. A stopped job can be sent or downloaded
again. When a printer with a job waiting comes free (and isn't still on a job you sent it),
ProofLoop tells you once, in the event log and through your notification targets; it tells you
too when a queued job finishes or stops. Nothing starts by itself: you **Send and start** a job
(with sending turned on, and confirmed) or **Download** it and send it yourself. A downloaded job
is followed if you print it on the printer it was planned for.

## Sending to printers

Off unless you turn it on (*Monitor › Sending to printers*). Then a loop file can go straight to a
printer and start, from *Make a loop file* or the queue, after you confirm the printer. Before
anything is sent, ProofLoop checks that the printer is connected and idle (not printing, preparing
or paused), that it is the model the plate was sliced for, that every filament slot has a matching
spool loaded, and that the loop-file checks find nothing dangerous. The file is copied over the
printer's own FTPS (port 990, the LAN access code) and started over MQTT, the way Bambu Studio
sends a print on your network; it is recorded first, so filament counting and the print journal
know it. The plate must be empty: the printer starts as soon as the file arrives. Watch the first
send to each printer. A paired phone can never send, or change this setting.

## Monitor

- **Find printers automatically.** *Find printers* listens for the printers' own network
  announcements and fills in the IP, serial and model. You still type the access code.
- **Test connection.** Logs in to the printer the way a send does (the access code, the serial
  number, Developer Mode, its file storage) without printing or copying anything, and says what to
  fix. The setup check runs it for every printer (`proofloop doctor --printer-test`).
- **Live loop status.** For each printer: the loop number, the current phase (printing, cooling,
  bending, door), average cooldown and loop times measured on your printer, and a plate-bend counter
  you reset when you fit a new plate.
- **The plate check.** After each sweep of a loop file made with *Wait for the plate check*, the
  monitor compares a camera picture with your pictures of the empty plate and resumes the next loop
  only when it's empty. Anything else (parts left, no picture, a camera error, any other kind of
  pause) leaves the printer waiting with the heaters off and tells you. Take the empty-plate
  pictures here.
- **Failure watch.** Part-way through each print (each loop of a loop file), ProofLoop looks at the
  plate with the camera and watches for two things. It needs pictures of the empty plate, never alerts
  without a camera picture, and every alert comes with that picture. *Pause the printer and tell me*
  pauses the printer too; nothing else it does ever pauses, resumes or starts a print.
  - *The plate still looks empty* halfway and again at 80%: the part has most likely come loose or the
    nozzle is clogged, and the printer is printing into the air.
  - *The print has gone wrong* (spaghetti, or a part knocked loose and dragged): every 10% of the
    layers (*Monitor › Camera* sets this from 5% to 25%, or off) the picture is compared with the
    empty-plate pictures and with the last look that was fine. A part that is growing changes the
    picture in one compact piece next to where it was. Spaghetti shows as a large area of new change
    away from there, spread over much of the plate, ragged and full of edges. It is an alert only after
    two looks in a row find that, the message says "the print looks like it has come loose or turned to
    spaghetti at layer 70 of 100", and it is told once for each print. A look the camera can't give,
    a blank or too dark picture, or a change over the whole plate (a lamp, a bumped camera) counts as
    neither good nor bad.

  This is a heuristic on pictures, with no model and no cloud, and it was tuned on made-up pictures,
  not on footage of real failures. Expect it to miss some failures and to be wrong now and then:
  - It misses a failure that grows slowly (each look is only compared with the last good one), one
    that starts before the first look, a single part knocked off in one piece (it looks like the
    toolhead moving), and spaghetti the camera can't see (behind the part, or a dark filament on a dark
    plate).
  - It can raise a false alarm when the camera is bumped or the light changes a lot during a print, or
    on a print that really does fill much of the plate in a short time. If that happens too often,
    choose *Only halfway and at 80%*, or *Don't look*.
- **Camera.** *Camera* on a printer's card shows its view: a new picture every second or two while
  it is open. It stops asking the printer's camera when you hide it, switch to another tab or the
  window is hidden, and several people (or phones) looking share one request. *Snapshot* takes one
  picture. A phone can look too, watch-only phones included.
- **Timelapse.** Off unless you turn it on (*Monitor › Camera*). During each print (each loop of a
  loop file) ProofLoop saves a small camera picture every 5 layers (you choose), and when the print
  ends it makes one animated WebP of them (a GIF if this computer can't write WebP), 640 pixels wide
  and at most 100 pictures, and deletes the pictures. A picture the camera can't give is skipped; the
  plate check is never held up. The last 5 timelapses of each printer (you choose) are kept in
  `~/.proofloop/timelapse/<printer>/` and listed on its card, with *View* and *Save*. Older ones are
  deleted, and a timelapse is not part of a backup.
- **Quiet hours.** Plates are still checked overnight, but the next loop waits until morning, with
  the printer paused and its heaters off.
- **Notifications.** ntfy, Discord, Telegram, email or any JSON webhook, as many as you like, each
  with its lowest level and a *Send test* button. When parts are left behind, the camera picture
  arrives with the notification (ntfy, Discord and Telegram attach it, email sends it as
  `plate.jpg`; a target that refuses the picture gets the text alone). These same targets get a
  notice when a long check under *Check* has finished, and when ProofLoop stops or restarts.
  *Email* needs your mail server (SMTP host, port, STARTTLS or SSL, username and password, From and
  To; Gmail and some others want an app password). *Telegram* needs a bot token from @BotFather
  and your chat id. Passwords and tokens are saved in `settings.json` and never shown again: the
  window says *saved* and an empty box keeps what is there. A paired phone sees only where each
  target goes, can't change them, and can't download a backup.
- **Home Assistant.** Off unless you turn it on (*Monitor › Home Assistant*). ProofLoop then
  publishes each printer's state to your own MQTT broker (address, port, optional user name and
  password, optional TLS, base topic `proofloop`), and Home Assistant finds the printers by itself
  through MQTT discovery. Each printer is a device with sensors for its state (idle, preparing,
  printing, paused, finished, failed, offline), progress, layer (with the total as an attribute),
  time left, nozzle and bed temperature, current job, plate check (empty, parts left, not
  checked), the loop it is on and the loops in the file, and a *Needs you* problem sensor that is
  on while the printer waits for you. `proofloop/status` says `online` while ProofLoop is connected
  and `offline` when it stops or the broker loses it, so Home Assistant marks the sensors
  unavailable. A printer's values go out when they change, at most once every 5 seconds. It only
  publishes: ProofLoop subscribes to nothing, so nothing on the broker can control a printer. A
  broker that is down, slow or refusing is only logged (the export keeps retrying, waiting longer
  each time, up to a minute) and never slows the watching. Turning it off leaves the sensors in
  Home Assistant, unavailable; delete them there if you don't want them. *Test connection* logs in
  and hangs up without publishing anything.

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

## Maintenance

*Printers › Maintenance* counts, for each printer, the print hours, prints, loops and grams of
filament (and days) since each job was last done, from the prints ProofLoop recorded. Each task shows
when it was last done, how much has been used since, and a bar that fills to its interval: **due** at
100% and **overdue** from 125%. Press **Done** to count from now again (pick an earlier day if you did
it a while ago, and add a note). A task ProofLoop hasn't seen done counts from when it first saw it,
not from before.

The built-in tasks are starting points, not manufacturer figures, and you change any of them or switch
them off: clean the build plate (every 25 prints), empty the purge chute (50 prints), clean and check
the nozzle (100 print hours), change the nozzle (500), check belt tension (300), clean and oil the rails
(200), clean the carbon rods (30 days), grease the lead screws (90 days) and check the AMS desiccant
(30 days). Only the carbon rods and lead screws follow a published interval, from Bambu Lab's wiki for
the X1 series ([sources](sources.md#maintenance)). Add your own tasks with an interval in print hours,
prints, loops, grams or days.

When a task becomes due you get one notice through your notification targets (level warning), and not
again until it's done or a week has passed; turn it off with the checkbox. The daily summary lists what
is due. Maintenance never stops or pauses a print. Your changes are in the settings and what you marked
done is in `printers.db`, so both are in the backup.

## Backup and restore

One file (*Printers › Filament › Backup*) with your settings, imported profiles, spools, usage,
production log and measured timings, the pictures of each printer's empty plate, the queue, your
`config/` options, and Check's design checks and what it has learned (including the findings you
marked *Not a problem*). Making and restoring a backup are only possible in the window, never from
a phone, because a backup holds your printers' access codes and passwords. Restoring replaces all of these. Phones pair again afterwards. Reports, the kept loop files and the pictures of
failed prints aren't included. Older backups (a `.json` file) still restore.

## On your phone, and on an always-on machine

- **Phone:** *More › Phone* in the window pairs a phone on your Wi-Fi to *Loop files* and
  *Printers* (see the [README](../README.md#on-your-phone)).
- **Always on:** `proofloop printers serve --host 0.0.0.0` (or `docker compose up -d`) serves
  *Loop files* and *Printers* alone on port 8484, for a NAS or mini PC that watches the printers
  around the clock. Set `app_token` in `settings.json` to require an access token. Run only one
  ProofLoop per farm, so the printers are watched once.
