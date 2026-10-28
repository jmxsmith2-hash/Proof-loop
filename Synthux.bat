@echo off
rem Double-click to open the synthux app window (run setup.bat once first).
rem Keep this console open while you use the app; closing it stops the app.
cd /d "%~dp0"
if not exist ".venv\Scripts\synthux.exe" (
  echo synthux is not set up yet: double-click setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\synthux.exe" app
if errorlevel 1 pause
