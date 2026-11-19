@echo off
cd /d "%~dp0.."
echo HL block priority-fee probe (read-only, no orders). p / 10000 = bp.
set /p D=Date YYYY-MM-DD, or a range A..B for weekdays like 2026-11-10..2026-11-18: 
set /p T=Time HH:MM:SS this PC timezone: 
set /p S=Seconds, default 2: 
if "%S%"=="" set S=2
set /p F=Coins: ours, all, or a list like xyz:SMSN,xyz:SKHX, default ours: 
if "%F%"=="" set F=ours
set OPT=
if /i "%F%"=="ours" set OPT=--ours
if /i not "%F%"=="ours" if /i not "%F%"=="all" set OPT=--coin %F%
set PYTHONPATH=%CD%
set PYTHONIOENCODING=utf-8
.venv32\Scripts\python.exe tools\hl_block_fees.py %D% %T% %S% %OPT%
pause
