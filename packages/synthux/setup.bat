@echo off
rem Double-click to set up synthux on Windows (creates .venv, installs everything, downloads Chromium).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1"
pause
