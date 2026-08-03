@echo off
rem build standalone distribution (no python needed on target PC)
rem output: dist\kp-arb\  -> copy that folder to the target PC
cd /d "%~dp0"
rem stop running exes so PyInstaller can overwrite locked files in _internal
taskkill /F /IM kp-arb-core.exe >nul 2>&1
taskkill /F /IM kp-arb.exe >nul 2>&1
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm kp_arb.spec
if errorlevel 1 (
    echo [ERROR] build failed
    pause
    exit /b 1
)
copy /Y config.yaml "dist\kp-arb\" >nul
rem ship config .env template (no secrets; secrets go to keyring via keys.bat)
copy /Y .env.example "dist\kp-arb\.env" >nul
rem key registration shortcut for the target PC (double-click)
(
    echo @echo off
    echo start "" "%%~dp0kp-arb.exe" keys
) > "dist\kp-arb\keys.bat"
echo.
echo build complete: dist\kp-arb\
echo copy that folder to the target PC and run kp-arb.exe (edit .env if needed)
pause
