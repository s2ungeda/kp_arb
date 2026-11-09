@echo off
cd /d "%~dp0.."
echo HL block priority-fee probe (read-only, no orders). p / 10000 = bp.
set /p D=Date (YYYY-MM-DD): 
set /p T=Time (HH:MM:SS, this PC timezone): 
set /p S=Seconds (default 2): 
if "%S%"=="" set S=2
set PYTHONPATH=%CD%
set PYTHONIOENCODING=utf-8
.venv32\Scripts\python.exe tools\hl_block_fees.py %D% %T% %S%
pause
