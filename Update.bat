@echo off
rem Double-click to get the newest ProofLoop from GitHub (it stops and starts ProofLoop for you).
cd /d "%~dp0"
".venv\Scripts\proofloop.exe" update
pause
