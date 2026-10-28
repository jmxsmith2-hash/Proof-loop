@echo off
rem Double-click to open ClearLoop (loop files and the printer monitor) in your browser.
rem Run setup.bat once first.
cd /d "%~dp0"
if not exist ".venv\Scripts\clearloop.exe" (
  echo ClearLoop is not set up yet: double-click setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\clearloop.exe" app
if errorlevel 1 pause
