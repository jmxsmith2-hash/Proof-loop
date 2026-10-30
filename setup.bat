@echo off
rem Double-click to set up ProofLoop on Windows (one .venv here, a desktop shortcut, start at login).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1"
pause
