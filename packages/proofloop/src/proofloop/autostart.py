"""Start ProofLoop in the background when you log in, so the printers are watched after a restart.

Windows: a shortcut in your Startup folder. macOS: a LaunchAgent (started again if it crashes).
Linux: an XDG autostart entry. All of these are for your user only and need no admin rights.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

LABEL = "com.proofloop.service"


def repo_root() -> Path | None:
    """The ProofLoop folder (repository root) when ProofLoop runs from it."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / "packages" / "proofloop").is_dir() else None


def background_python() -> Path:
    """pythonw.exe on Windows (no console window), else this Python."""
    exe = Path(sys.executable)
    if sys.platform == "win32":
        w = exe.with_name("pythonw.exe")
        if w.is_file():
            return w
    return exe


def command() -> list[str]:
    return [str(background_python()), "-m", "proofloop", "service"]


def _entry() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / \
            "ProofLoop.lnk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "proofloop.desktop"


def enabled() -> bool:
    try:
        return _entry().is_file()
    except KeyError:  # no APPDATA
        return False


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def enable() -> Path:
    path = _entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = command()
    workdir = str(repo_root() or Path.home())
    if sys.platform == "win32":
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(str(path)) + "); "
            "$s.TargetPath = " + _ps_quote(cmd[0]) + "; "
            "$s.Arguments = " + _ps_quote(" ".join(cmd[1:])) + "; "
            "$s.WorkingDirectory = " + _ps_quote(workdir) + "; "
            "$s.WindowStyle = 7; "
            "$s.Description = 'ProofLoop in the background: watches your printers'; $s.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elif sys.platform == "darwin":
        from xml.sax.saxutils import escape

        args = "".join(f"<string>{escape(c)}</string>" for c in cmd)
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0"><dict><key>Label</key><string>{LABEL}</string>'
            f"<key>ProgramArguments</key><array>{args}</array>"
            f"<key>WorkingDirectory</key><string>{escape(workdir)}</string>"
            "<key>RunAtLoad</key><true/>"
            "<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>"
            "</dict></plist>\n",
            encoding="utf-8",
        )
    else:
        exec_line = " ".join('"' + c.replace('"', '\\"') + '"' for c in cmd)
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=ProofLoop\n"
            "Comment=ProofLoop in the background: watches your printers\n"
            f"Exec={exec_line}\nPath={workdir}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
    return path


def disable() -> None:
    try:
        _entry().unlink(missing_ok=True)
    except KeyError:
        pass
