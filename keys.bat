@echo off
rem key registration window (Windows Credential Manager)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kp_arb.key_setup
