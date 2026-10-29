"""``proofloop update``: get the newest ProofLoop from GitHub and install it.

ProofLoop is a git checkout, so updating is ``git pull`` plus re-installing the three packages
(new dependencies may have come with the update), then restarting ProofLoop if it was running.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .autostart import repo_root

PACKAGES = ("packages/synthux[dev]", "packages/clearloop[dev]", "packages/proofloop[dev]")


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=check,
                          encoding="utf-8", errors="replace")


def behind(root: Path) -> tuple[int, str]:
    """(commits behind the remote, the branch), after fetching. Raises CalledProcessError."""
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(root, "fetch", "--quiet", "origin", branch)
    n = int(_git(root, "rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip() or 0)
    return n, branch


def update(check_only: bool = False, say=print) -> int:
    root = repo_root()
    if root is None or not (root / ".git").exists():
        say("This ProofLoop wasn't installed with git, so it can't update itself. Download it again from "
            "https://github.com/jmxsmith2-hash/Proof-loop, copy packages/synthux/config/settings.yaml and "
            "packages/synthux/data from this copy into it, and run setup.bat. ClearLoop's settings live in your "
            "user folder and are kept.")
        return 2
    try:
        n, branch = behind(root)
    except FileNotFoundError:
        say("git isn't installed. Install it (https://git-scm.com/download/win) and run this again.")
        return 2
    except subprocess.CalledProcessError as e:
        last = ((e.stderr or e.stdout or "").strip().splitlines() or [""])[-1]
        say(f"Couldn't reach GitHub: {last}\nThe repository is private: sign in when Git asks (Git Credential "
            "Manager remembers it).")
        return 2
    if not n:
        say(f"ProofLoop is up to date ({branch}).")
        return 0
    say(f"{n} update{'s' if n != 1 else ''} available on {branch}.")
    if check_only:
        return 0
    if _git(root, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip():
        say("You've changed ProofLoop's own files, so the update would overwrite them. Keep a copy, then run "
            f'git -C "{root}" stash and try again.')
        return 2
    r = _git(root, "pull", "--ff-only", "--quiet", "origin", branch, check=False)
    if r.returncode:
        say("git pull failed:\n" + (r.stderr or r.stdout))
        return 2
    say("Installing…")
    args = [sys.executable, "-m", "pip", "install", "--quiet"]
    for p in PACKAGES:
        args += ["-e", str(root / p)]
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        say("Installing the update failed:\n" + (r.stderr or r.stdout)[-2000:])
        return 2
    say("Updated. " + _git(root, "log", "-1", "--format=%h %s").stdout.strip())
    return 0
