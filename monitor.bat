@echo off
rem 시세 모니터 창 (.venv 파이썬으로 실행)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kp_arb.monitor
