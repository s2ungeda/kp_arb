@echo off
rem peg order window (runs with .venv32 python, 32-bit)
cd /d "%~dp0"
".venv32\Scripts\python.exe" -m kp_arb.peg_order
