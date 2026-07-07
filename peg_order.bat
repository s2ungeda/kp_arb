@echo off
rem peg order window (runs with .venv python)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kp_arb.peg_order
