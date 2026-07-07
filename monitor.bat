@echo off
rem market monitor window (runs with .venv python)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kp_arb.monitor
