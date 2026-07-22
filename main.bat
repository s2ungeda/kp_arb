@echo off
rem main admin window (runs with .venv python)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kp_arb.main_window
