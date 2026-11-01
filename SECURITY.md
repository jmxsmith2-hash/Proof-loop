# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull request. On
[github.com/jmxsmith2-hash/Proof-loop](https://github.com/jmxsmith2-hash/Proof-loop), open the
**Security** tab and choose **Report a vulnerability**. That opens a private security advisory
that only you and the maintainers can see.

Say what you found, how to reproduce it, and which version you used (`proofloop --version`).

## Supported versions

Only the newest release is supported. Update with `Update.bat` or `proofloop update`. The newest
release is the top section of [CHANGELOG.md](CHANGELOG.md).

## Threat model

ProofLoop runs on your own computer, for you. Your designs, print history, settings and the
loop files it makes stay in `~/.proofloop`. It doesn't need an account or a server of ours.

- **The window** is served on `127.0.0.1:8600` only. It refuses any host name other than
  `127.0.0.1` and `localhost`, which stops DNS rebinding. Routes that only the window may use need
  the `X-ProofLoop` header and the same origin.
- **Phone access** is off until you turn it on. When it's on, it listens on your local network
  (port 8601) and serves only *Loop files* and *Printers*, never *Check* or the window's settings.
  A phone pairs with a QR code or a six-digit code, and there are two kinds: **watch only** and
  **operator**, each with its own secret and code. A watch-only phone can read what a phone sees,
  and every other request from it (anything but GET) is refused, as are the few reads that change
  something or show a secret (scanning the network; opening an invoice, which numbers it; the
  invoice sender's details). The check is one guard in front of every route, so
  a route added later is covered. An operator phone can change what the page offers, as a phone
  always could, but can never send a file to a printer, download or restore a backup (it holds the printer access
  codes and every password) or change a window-only
  setting. Phones paired before this existed are operators. Five wrong codes in ten minutes, for
  both codes together, lock out typing codes. The cookie holds the secret of its kind, is HttpOnly
  and SameSite=Lax, and a change asked for by a page from another address is refused. Secrets and
  codes are compared in constant time. *Forget phones* unpairs every phone of both kinds.
- **Sending to printers** is off unless you turn it on, and each send needs you to confirm the
  printer.
- **The MCP server** (`proofloop mcp`) can never start a print. A run it plans goes into the queue,
  and you send each job from the window.
- **Printer access codes** are kept in `~/.proofloop/settings.json` on your computer. Anyone who can
  read that file can read them.
- **The always-on server** (`proofloop printers serve`, or Docker) is a separate listener, on port
  8484 by default. Run it only on a network you trust, and set `app_token` in `settings.json`.

ProofLoop doesn't protect against software already running as your user on your computer.
