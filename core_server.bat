@echo off
rem strategy core process (runs with .venv python)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kp_arb.core_server
