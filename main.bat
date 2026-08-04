@echo off
rem main admin window - no console, detached
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m kp_arb.main_window
