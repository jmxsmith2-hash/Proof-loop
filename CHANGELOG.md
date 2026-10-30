# Changelog

What changed in each version of ProofLoop. `proofloop update` shows the parts that are new to you,
and the window shows them under *More › What's new*.

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
