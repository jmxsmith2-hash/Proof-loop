@echo off
rem Double-click to open ClearLoop (loop files and the printer monitor) in your browser.
rem setup.bat installs it when it finds ClearLoop next to this folder or can fetch it with git.
cd /d "%~dp0"
if not exist ".venv\Scripts\clearloop.exe" (
  echo ClearLoop is not installed here: put the ClearLoop folder next to this one and run setup.bat again.
  pause
  exit /b 1
)
".venv\Scripts\clearloop.exe" app
if errorlevel 1 pause
