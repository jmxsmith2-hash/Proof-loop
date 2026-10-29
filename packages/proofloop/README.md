# proofloop

The ProofLoop window, background service and command. It joins [synthux](../synthux/README.md),
which checks designs, G-code and websites, and [ClearLoop](../clearloop/README.md), which makes
loop files and watches your printers. Start with the [ProofLoop README](../../README.md).

## How it works

**`service.py`: the background service.**
- `proofloop service` runs one process holding everything:
  - the window's server on `http://127.0.0.1:8600`;
  - ClearLoop and its printer monitor;
  - synthux on a private port;
  - phone access, when it's on.
- The window is only a view: closing it leaves the service running.
- The service writes a heartbeat to `~/.proofloop/running.json` every minute. If that file is still there at the next start, the last run ended unexpectedly, and the service says for how long the printers went unwatched.
- It sends a notice when it stops (waiting for the notice to be delivered before the process ends) and when it restarts after an unexpected stop. Notices go through ClearLoop's targets.
- If the window's server dies on its own, the service restarts it.

**`tray.py`: the icon next to the clock.** It uses pystray (installed on Windows and macOS). Where there's no tray, the service runs without one.

**`autostart.py`: start at login**, for your user only:
- Windows: a shortcut in the Startup folder (pythonw, so no console).
- macOS: a LaunchAgent that restarts it after a crash.
- Linux: an XDG autostart entry.

**`shell.py`: the window.**
- Three frames: synthux's printing view and website view (`?view=print` and `?view=web`), and ClearLoop's page at `/clearloop`.
- ClearLoop's API is mounted on the window's own address.
- synthux allows only this window to show it in a frame (`frame-ancestors`).
- Plates move between the frames by `postMessage`, and the window checks where each message came from.
- Endpoints that change things all need the `X-ProofLoop` header and the window's own origin:
  - `/proofloop/to-clearloop` and `/proofloop/to-synthux` hand plates over;
  - `/proofloop/status`, `/phone`, `/autostart` and `/quit` are ProofLoop's own settings.
- Host names other than `127.0.0.1` and `localhost` are refused, which stops DNS-rebinding attacks.

**`phone.py`: phone access.**
- A listener on the local network, port 8601. It serves ClearLoop's page and API only.
- A phone pairs with the QR code (a link holding a long secret) or the six-digit code, and then carries an HttpOnly, SameSite=Lax cookie.
- Five wrong codes in ten minutes lock out typing codes.
- *Forget phones* makes new secrets.

**`doctor.py`: the setup check**, with an exact fix for everything it finds missing.

**`update.py`:** `git pull --ff-only`, then reinstalls the three packages.

If only one of synthux and ClearLoop is installed, the window shows that one alone.
Double-clicking the shortcut while ProofLoop is running opens its window instead of starting a
second copy.

## Development

```bash
pip install -e ../synthux -e ../clearloop -e ".[dev]"
pytest -q
ruff check src tests
```

The tests never touch your real `~/.proofloop` or `~/.clearloop` (see `tests/conftest.py`).
