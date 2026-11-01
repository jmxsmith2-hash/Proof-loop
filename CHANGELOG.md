# Changelog

What changed in each version of ProofLoop. `proofloop update` shows the parts that are new to you,
and the window shows them under *More › What's new*.

## 0.5.0 · 2026-10-31

### New
- **Slice here.** Drop an STL, OBJ or STEP (or a Bambu Studio project) on *Make a loop file* and
  slice it without opening the slicer: pick the printer, and the nozzle, nozzle flow and the
  filament in its AMS come from it, with your own saved presets listed first. Ask for copies, or
  **Fill the plate** with as many as fit; parts are kept out of the strip the pusher comes down in.
  A project keeps its own settings and only takes the printer's nozzle flow, so a plate sliced for
  the wrong nozzle is fixed by dropping its project. Needs Bambu Studio (or OrcaSlicer) installed.
- **Run dashboard** on the Overview: each printer's parts per hour over the last 24 hours and 7
  days, counted from its finished loops ("not enough data" until there are some), and its pace
  while looping; each open order's parts made and still to make and when it will be done against
  its due date; and each printer's next queued job and whether it's hands-off.
- **Spool swap timing.** Before you send, on queued jobs and while a run prints, ProofLoop adds up
  what every loop takes from each AMS tray and says it's enough, or which loop the tray runs out on
  and after which loop to swap the spool, or which matching tray the AMS can switch to. It uses the
  linked spool's count, or the AMS's estimate for Bambu spools, and says when the amount isn't known.
- **Proof photos.** ProofLoop saves a picture of each loop's finished plate before the sweep, so
  every order in *Orders* has a gallery of what was made, and each printer's card shows its latest
  few. On by default; turn it off or choose how many to keep in *Monitor › Camera*.
- **Hands-off queue** (per printer, off unless you turn it on on its card under *Monitor*). When
  the printer is free and the camera says its plate is empty after the last print, its next queued
  job is sent by itself; ProofLoop takes the picture itself when it needs a fresh one. With *ask
  before each send* on you get a notice two minutes before, with **Hold** in the queue. Parts left
  on the plate, a stopped job or a failed send stop it and tell you; nothing is retried by itself.
- **Orders from a model.** Drop an STL, OBJ, STEP or project on a new order: it's sliced with your
  usual presets for the order's size (plates shared evenly, so 150 parts at 100 a plate is two
  loops of 75, and a small order is just its parts on one plate), planned over your printers and
  queued as before. Change the quantity and it's sliced again. With your rates set, the price
  field shows the quote.
- **Watch folder** (off unless you turn it on, under *Make a loop file*). Save a model into
  `Downloads\ProofLoop` (or a folder you choose) and it's sliced with the presets you last used in
  the Slice card, made to fit the printer you pick: its nozzle, nozzle flow and AMS. It waits in a
  list to open, and you're told when it's ready. Files in the folder are only read, never moved.
- **A price on every slice.** Each plate in *Make a loop file* shows what a part costs you with
  your rates, the break-even price and the price for your target margin, worked back through the
  tax and fees you set. *Take an order at this price* opens a new order with the plate and price
  filled in. Until your rates are set it shows the grams and hours the plate uses instead.
- **Spaghetti watch.** The failure watch also notices a print that has turned to spaghetti or come
  loose: it looks with the camera every 30 seconds (20 seconds to 5 minutes, or by layers, under
  *Monitor › Camera*) and alerts, or pauses if you chose that, once the print has looked wrong for
  at least a minute. It's a heuristic, and can be set to look only halfway and at 80%.
- **Live camera.** Each printer's card shows the camera as live video while it's open: one
  connection that stays up, about 15 frames a second from a P2S (a picture every second or two
  before), with a LIVE badge, the frame rate, and a bigger view on a click. It pauses when you hide
  it or switch away and closes the camera soon after; while it's open, plate checks and the failure
  watch use its frames instead of connecting again.
- **Timelapse** (off unless you turn it on): a picture every few layers while a print runs, made
  into a small animation when it ends; each printer's last few are on its card to view or save.
- **Maintenance.** *Printers › Maintenance* counts print hours, prints and loops per printer since
  each job was last done (the build plate, nozzle, belts, rods and more, with intervals you can
  change), shows what is due, and tells you once when something comes due.
- **Email and Telegram.** Send notices to your email or a Telegram chat as well as ntfy, Discord and
  webhooks, each with its own lowest level and a *Send test* button; the camera picture comes along.
- **Home Assistant.** Optionally share each printer's state with your own MQTT broker, and Home
  Assistant finds the printers by itself. It only publishes: nothing on the broker can control a
  printer.
- **Invoices.** Press *Invoice* on an order for a page to print or save as PDF, numbered once and
  never changed; mark orders paid, and see what each customer has earned and still owes.
- **Spreadsheets.** Download orders, print history, and production and costs as CSV for Excel or
  Sheets, or run `proofloop printers export`.
- **Fix and slice again in Check.** A print-ready result that lists slicer settings that would fix
  it now has a *Fix and slice again* button (or tick it at upload): it slices again with the settings
  changed, shows each change with its old and new value and the problems before and after, and lets
  you download the fixed plate and the presets to import.
- **Two kinds of phone.** *More › Phone* pairs a phone as **Watch only** (sees everything, changes
  nothing) or **Operator** (as before); phones paired earlier stay operators, and *Forget phones*
  unpairs both.
- **More printers for the checks.** Design and G-code checks have their own profiles for the P1S,
  P1P, A1, A1 mini, A2L, H2C and X2D, each value with its source in `docs/sources.md`. Loop files stay
  P2S-only.
- **Files with several sliced plates.** Drop a file made with *Export all sliced file* and every
  plate is used, not just the first: *Make a loop file* shows a button per plate and *Add all to the
  batch*, and *Check* checks each plate as its own job. Each plate is kept as its own file
  (`name-plate2.gcode.3mf`), so sending it starts that plate.
- **Send to a printer, smoother.** *Make a loop file* lists every printer as ready, or says why
  not (busy, wrong filament, not connected), with the AMS tray that will feed each filament. It
  picks a ready printer for you, and sending can be turned on right there.
- **Nozzle check.** *Make a loop file* shows the nozzle a plate was sliced for (size and flow), and
  before sending warns when it differs from the printer's: a plate sliced for a Standard nozzle on a
  printer with a High Flow one, for example. A different nozzle size stops the send. ProofLoop never
  changes the file's nozzle settings; slice again in Bambu Studio with the right nozzle flow.
- **Batches mix plates with different filaments.** A slot number only has to mean the same filament
  on every plate (as it does for plates of one Bambu Studio project); the batch file lists every
  filament, and sending maps each to the AMS tray that holds it. A batch can be sent to a printer
  from its own card, and *Ask before each send* can be turned off for one-click sending.
- **Quieter loops, one load line.** *Printer sounds* plays the printer's start and finish tunes on
  every loop, only when the whole run ends, or not at all. Bambu's nozzle load line (the line drawn
  on the front edge before printing) is drawn on the first loop only; later loops prime the nozzle
  with a small dab where it starts, so the first layer still starts full. Tick *Draw the nozzle load
  line on every loop* to keep it as sliced.
- **Level once per run** (off unless you turn it on). Loops after the first skip bed leveling, the
  vibration sweep and flow calibration and use loop 1's results, saving a few minutes per loop.
  Homing, plate detection and the nozzle wipe still run on every loop.
- **Every AMS tray as a spool in one click.** *Filament › In your printers* adds a spool for each
  loaded tray, linked to it. Bambu spools come with their weight and what's left from the RFID tag.

- **A Windows installer** (no Python needed) and a package ready for PyPI, built by the release.

### Changed
- `settings.json`, the queue and orders carry a format version. A file written by a newer ProofLoop
  is never overwritten, an older one is upgraded (with a one-time `.v0.bak` copy), and a backup from a
  newer ProofLoop is refused before anything is replaced. Files are written whole or not at all.
- A phone can no longer download a backup: it holds your printers' access codes and passwords.
- `proofloop update` says what to run for a pip, pipx or installer copy, and *What's new* works there
  too.
- Inside: the Loop files & Printers server is one module per part (`app/routes/`), the CAD and
  G-code checks are split by kind, every module is type-checked, and errors are caught by kind and
  logged instead of swallowed.
- CI lints the page scripts, drives the window in a real browser, and pins its actions to commits.

### Removed
- **Moulding and CNC checks.** *Check* is for 3D printing only: the "How it is made" choice,
  `--process` and `--pull`, and `processes.yaml` are gone (they're in the history of 0.4.0 if you
  need them). A design checked for moulding or machining before isn't compared with its next print
  check.

### Fixed
- Saving the notification form wiped a saved access token.
- Taking an order when `orders.json` came from a newer ProofLoop left an unfollowed run in the queue.
- The print-ready check crashed when the slicer itself reported a problem, and its report when a fix
  changed only filament settings.
- Sensor logs without a time zone gave a busiest hour that depended on your computer's time zone.
- A camera snapshot for an unknown printer said "camera error" instead of "no such printer".
- A printer whose serial number was typed in lower case connected but never showed its state or
  AMS. Serials are now saved in capitals, and a printer that connects but sends nothing says to
  check the serial.
- On Windows, the camera (and the slicer and other tools) no longer open a console window each
  time ProofLoop starts them.
- Sending to a P2S failed on the printer with error 0500-4002 ("unsupported file path or name"):
  the file now goes in the printer's `cache/` folder and is started the way a P2S expects, and
  its name is made safe (no spaces or brackets, always `.gcode.3mf`).

## 0.4.0 · 2026-10-30

### New
- **Orders.** *Printers › Orders* keeps what people asked for: a customer, how many parts, a due date
  and a price. Taking an order plans its run over your printers into the queue, then shows how many
  parts are made, when it will be ready, whether that's on time, and what it earns.
- **ProofLoop with Claude.** `proofloop mcp` lets Claude check parts and G-code (and see them),
  fix a design and check it again, make loop files, take orders and plan runs, and read how your
  printers are doing. It can never start a print. See `docs/assistants.md`.
- **Try it without printers.** `proofloop try` runs three pretend printers that print about 40 times
  faster than real ones, with everything else real.
- **While you were away.** A summary on the Overview, from `proofloop summary`, and every morning
  through your notification targets if you choose a time.
- **Failure watch.** Halfway through each print and again at 80%, ProofLoop looks at the plate. If it
  still looks empty, the part has come loose or the nozzle is clogged: you're told, and the printer
  can pause.
- **Test connection.** Each printer card (and `proofloop doctor --printer-test`) logs in the way a
  send does and says what to fix, without printing anything.
- **Live updates.** The Overview, Monitor and Orders change as your printers do, instead of every few
  seconds.
- **OpenSCAD.** Check takes `.scad` files.
- **`--json` on every command**, with exit codes that mean the same everywhere: `0` passed, `1`
  something to fix first, `2` couldn't run.
- **Backups hold everything**: design checks, the queue, orders, `config/` and the empty-plate
  pictures too.
- **The queue follows each job** on its printer (printing loop 3 of 10, done, stopped) and offers a
  printer's next job only when the last one has ended.

### Changed
- A fresh look: clearer type, depth and motion in every part of the window, the reports and the
  phone; check results start with a verdict.
- A check that finds problems now exits with `1` (it was `0`).
- Every installer installs the exact versions CI tested (`constraints.txt`).

### Fixed
- Sending a file to a printer failed every time (the encrypted file copy had no host name).
- Layer counts showed "layer 72/1" for files without layer numbers.
- Turning phone access on or off re-ran the setup guide's code.

## 0.3.0 · 2026-10-29

### New
- **One app.** The design checks, loop files and the printer monitor became one ProofLoop: one
  window with *Check*, *Loop files* and *Printers*, one command, one data folder, one look.
- **Get set up**, a first-run guide that ticks itself off.
- **Sending to printers** (off unless you turn it on), and **spreading a run over your printers**
  into a queue.
