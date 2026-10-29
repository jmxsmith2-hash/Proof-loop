@echo off
rem Double-click to open ProofLoop. It starts in the background if it isn't running, and keeps
rem watching your printers when you close its window (quit it from its icon next to the clock).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo ProofLoop is not set up yet: double-click setup.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m proofloop
