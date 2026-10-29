"""Opening the ProofLoop window: its own window in Edge or Chrome, else a browser tab."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def app_browser() -> str | None:
    """Chrome or Edge, which can show a page as its own window (--app) without tabs or an address bar."""
    candidates: list[str] = []
    if sys.platform == "win32":
        for base in filter(None, (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                                  os.environ.get("LOCALAPPDATA"))):
            candidates += [str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                           str(Path(base) / "Google/Chrome/Application/chrome.exe")]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    for exe in candidates:
        if Path(exe).is_file():
            return exe
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_window(url: str, profile_dir: Path) -> bool:
    """Open the app in its own window; falls back to a normal browser tab."""
    exe = app_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1120,880", f"--user-data-dir={profile_dir}",
                              "--no-first-run", "--no-default-browser-check"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    import webbrowser

    return webbrowser.open(url)
