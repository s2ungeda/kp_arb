@echo off
rem strategy core process (runs with .venv32 python, 32-bit)
cd /d "%~dp0"
".venv32\Scripts\python.exe" -m kp_arb.core_server
