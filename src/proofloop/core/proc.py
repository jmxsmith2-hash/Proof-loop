"""Starting other programs (ffmpeg, the slicer, git) without a console window popping up on Windows."""

from __future__ import annotations

import subprocess

# ProofLoop runs in the background with no console of its own, so on Windows every console program it
# starts would open a window: the camera alone starts ffmpeg every second or two. Pass as creationflags.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
