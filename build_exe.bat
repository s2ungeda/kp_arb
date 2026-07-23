@echo off
rem build standalone distribution (no python needed on target PC)
rem output: dist\kp-arb\  -> copy that folder to the target PC
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm kp_arb.spec
if errorlevel 1 (
    echo [ERROR] build failed
    pause
    exit /b 1
)
copy /Y config.yaml "dist\kp-arb\" >nul
echo.
echo build complete: dist\kp-arb\
echo copy that folder (plus your .env) to the target PC and run kp-arb.exe
pause
