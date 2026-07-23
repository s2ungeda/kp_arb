@echo off
rem setup on a new PC: create venv (python 3.11+) and install dependencies
rem usage: run this once after copying/cloning the project folder
cd /d "%~dp0"

set PYCMD=
py -3.12 -c "pass" >nul 2>&1 && set PYCMD=py -3.12
if not defined PYCMD py -3.11 -c "pass" >nul 2>&1 && set PYCMD=py -3.11
if not defined PYCMD python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1 && set PYCMD=python
if not defined PYCMD (
    echo [ERROR] Python 3.11+ not found. Install from https://www.python.org/downloads/
    echo         and check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

echo using: %PYCMD%
%PYCMD% -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 (
    echo [ERROR] install failed. check network/proxy and retry.
    pause
    exit /b 1
)

echo.
echo setup complete. next steps:
echo   1) register API keys: run keys.bat (or copy .env from the original PC)
echo   2) run main.bat
pause
