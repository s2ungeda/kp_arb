@echo off
rem 호가 추적 자동 주문 창 (.venv 파이썬으로 실행)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kp_arb.peg_order
