# proofloop

The ProofLoop window and command. It joins [synthux](../synthux/README.md), which checks designs
and G-code, and [ClearLoop](../clearloop/README.md), which makes loop files and watches your
printers. Start with the [ProofLoop README](../../README.md).

```bash
proofloop               # one window: "Check & test" (synthux) and "Loop files & printers" (ClearLoop)
proofloop check FILE    # synthux's check for a CAD file or sliced G-code
proofloop loop PLATE    # ClearLoop's make
proofloop synthux ...   # any synthux command
proofloop clearloop ... # any ClearLoop command
```

## How it works

`proofloop app` starts both apps in one process (code in `src/proofloop/shell.py`):

- **ClearLoop's API** is mounted on the window's own address, `http://127.0.0.1:8600`, and its page is shown at `/clearloop`.
- **synthux's app** runs on a private port and allows only this window to show it in a frame (`frame-ancestors`).
- **Handing plates over:** each frame asks the window by `postMessage`, and the window checks where the message came from. The window then calls one of two endpoints:
  - `/proofloop/to-clearloop`: a plate synthux checked goes into ClearLoop's upload cache.
  - `/proofloop/to-synthux`: a plate dropped in ClearLoop becomes a synthux job.

  Both endpoints need the `X-ProofLoop` header and the window's own origin.
- **Notifications:** when a synthux check finishes, the window shows a desktop notice (if the window is in the background) or a badge on the tab.
- **Network:** everything listens on `127.0.0.1` only. Host names other than `127.0.0.1` and `localhost` are refused, which stops DNS-rebinding attacks.

If only one of the two is installed, the window shows that one alone. Double-clicking the
shortcut while ProofLoop is already running opens its window again instead of starting a second one.

## Development

```bash
pip install -e ../synthux -e ../clearloop -e ".[dev]"
pytest -q
ruff check src tests
```
