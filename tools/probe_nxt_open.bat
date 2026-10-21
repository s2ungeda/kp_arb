@echo off
cd /d "%~dp0.."
echo NXT open probe 07:59:30-08:00:40 (no orders). Output: logs\probe\nxt_open_YYYYMMDD.jsonl
set PYTHONPATH=%CD%
set PYTHONIOENCODING=utf-8
.venv32\Scripts\python.exe tools\probe_nxt_open.py 07:59:30 08:00:40 auto
pause
