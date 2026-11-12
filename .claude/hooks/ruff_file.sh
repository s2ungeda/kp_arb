#!/usr/bin/env bash
# PostToolUse(Edit|Write): 고친 파일이 kp_arb/·tests/의 .py면 그 파일만 ruff (2026-11-12).
# 위반이면 exit 2 — stderr가 Claude에게 간다(exit 0/1의 출력은 Claude가 못 본다).
# 전체 검증(ruff+mypy+pytest)은 Stop 훅(stop_check.sh)이 끝낼 때 한 번.
cd "$(dirname "$0")/../.."
PY=".venv32/Scripts/python.exe"; [ -x "$PY" ] || PY=".venv/Scripts/python.exe"
f=$("$PY" -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))')
f=${f//\\//}
case "$f" in */kp_arb/*.py|*/tests/*.py|kp_arb/*.py|tests/*.py) ;; *) exit 0 ;; esac
out=$("$PY" -m ruff check "$f" 2>&1) && exit 0
printf '%s\n' "ruff 위반 — 지금 고친 파일: $f" "$out" >&2
exit 2
