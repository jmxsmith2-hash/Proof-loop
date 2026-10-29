@echo off
rem Double-click to open ProofLoop: synthux (check designs and G-code) and ClearLoop (loop files,
rem printers) in one window. Run setup.bat once first. Keep this console open while you use it.
cd /d "%~dp0"
if not exist ".venv\Scripts\proofloop.exe" (
  echo ProofLoop is not set up yet: double-click setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\proofloop.exe" app
if errorlevel 1 pause
