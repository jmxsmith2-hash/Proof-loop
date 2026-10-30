# ProofLoop with Claude (and other assistants)

`proofloop mcp` lets an assistant such as Claude use ProofLoop on your computer, over the
[Model Context Protocol](https://modelcontextprotocol.io). Ask in your own words:

- "Check `C:\parts\bracket.stl` for my P2S and tell me what to fix."
- "Make this part ready to print": it checks the part, changes the design (in an OpenSCAD file it
  edits the code), checks again, and repeats until nothing major is left.
- "Make a loop file for 40 of these and spread it over my printers."
- "Why did printer 2 stop last night?" It reads the print history: the layer, the HMS alerts, the
  plate check.
- "What happened overnight?", "How much PLA is left, and what should I reorder?"

**It can't start a print.** Nothing it can do sends a file to a printer, starts, pauses or resumes
one. A run it plans goes into the queue under *Printers › Overview*, and you send each job from the
ProofLoop window, after confirming the printer.

## Set it up

**Claude Code:**

```bash
claude mcp add proofloop -- proofloop mcp
```

**Claude Desktop:** run `proofloop mcp --config`. It prints the entry for
`claude_desktop_config.json` with the paths on your computer (Settings › Developer › Edit Config),
then restart Claude Desktop. On Windows, after `setup.bat`, that's:

```json
{"mcpServers": {"proofloop": {"command": "C:\\...\\Proof-loop\\.venv\\Scripts\\python.exe",
                              "args": ["-m", "proofloop", "mcp"]}}}
```

Live printer state (status, the queue, planning a run) needs ProofLoop running, which it usually is:
it starts when you log in. Everything else works without it.

## What it can do

| Tool | What it does |
|---|---|
| `check_part` | Check a CAD part (STL, 3MF, OBJ, STEP, or OpenSCAD `.scad`): findings with fixes and the numbers behind them, and pictures of the part and of each problem. The same `design` name each time compares versions. |
| `check_gcode` | Check sliced G-code, with the slicer settings that fix what it finds. |
| `prepare_print` | Check the design, slice it with Bambu Studio or OrcaSlicer, check the G-code, optionally apply the fixes and make a loop file. |
| `make_loop_file`, `check_loop_file`, `cost_plate` | Make a loop file with your saved settings (written next to the plate); check any loop file; what a plate costs and earns. |
| `plan_run` | Spread a run over the printers that can print it; with `keep`, put it in the queue. |
| `farm_status`, `summary`, `print_history`, `filament` | Every printer now; what happened (parts, failures, what needs you); how prints ended; spools and what's low. |
| `setup_check` | The setup check. |

There is also a prompt, **improve_part**, that walks an assistant through checking a part, fixing
it in its design and checking again.

## For programs

Every command takes `--json` and prints one JSON document; the exit code is `0` when the check
passed (nothing major or worse), `1` when there is something to fix first and `2` when it couldn't
run. Check results share one shape: `ready`, `blocking`, and `findings`, each with a stable `id`
(the check that found it), `severity` (1 note, 2 minor, 3 major, 4 will fail), `fix`, `location` or
`layer`, `measured` and `image`.

```bash
proofloop check part.stl --json | jq '.findings[] | select(.severity >= 3) | {id, fix}'
proofloop summary --hours 24 --json
```
