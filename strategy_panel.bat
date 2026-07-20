@echo off
rem strategy panel UI (runs with .venv python)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kp_arb.strategy_panel
