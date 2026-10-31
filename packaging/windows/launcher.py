"""What the two .exe files run: ProofLoop's own command line, with what a frozen app needs.

proofloop.exe       the command line (a console program): proofloop.exe check part.stl
ProofLoopApp.exe    opens the window like the desktop shortcut does (no console window)
"""

import multiprocessing
import os
import sys


def run() -> None:
    multiprocessing.freeze_support()
    for name in ("stdout", "stderr"):  # the windowed program has neither; print() and uvicorn need one
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
    if sys.argv[1:3] == ["-m", "proofloop"]:  # ProofLoop starts its background copy as "python -m proofloop service"
        del sys.argv[1:3]
    from proofloop.cli import main

    raise SystemExit(main())
