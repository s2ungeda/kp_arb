@echo off
rem setup on a new PC: create 32-bit venv (.venv32, python 3.12 x86) and install dependencies
rem 32-bit is required: xingAPI COM lives inside the core (DESIGN-ls-xing.md)
rem usage: run this once after copying/cloning the project folder
cd /d "%~dp0"

set PYCMD=
py -3.12-32 -c "pass" >nul 2>&1 && set PYCMD=py -3.12-32
if not defined PYCMD py -3.11-32 -c "pass" >nul 2>&1 && set PYCMD=py -3.11-32
if not defined PYCMD (
    echo [ERROR] 32-bit Python 3.11+ not found.
    echo         winget install --id Python.Python.3.12 --architecture x86 --scope user
    echo         or install the "Windows installer (32-bit)" from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo using: %PYCMD%
%PYCMD% -m venv .venv32
".venv32\Scripts\python.exe" -m pip install --upgrade pip
".venv32\Scripts\python.exe" -m pip install -e .
if errorlevel 1 (
    echo [ERROR] install failed. check network/proxy and retry.
    pause
    exit /b 1
)

echo.
echo setup complete. next steps:
echo   1) xingAPI: install package (C:\meme), run reg.bat as administrator, download Res via DevCenter
echo   2) register keys: run keys.bat (or copy .env from the original PC)
echo   3) run main.bat
pause
