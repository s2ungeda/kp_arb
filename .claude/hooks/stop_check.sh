#!/usr/bin/env bash
# Stop(Claude가 답을 마칠 때): 마지막 green 뒤 kp_arb/·tests/의 .py나 pyproject.toml이 바뀌었을
# 때만 check.sh 전체(ruff+mypy+pytest, 약 28초). 바뀐 게 없으면 즉시 통과. 빨강이면 exit 2로
# 꼬리 30줄을 Claude에게 보내 고치게 한다(2026-11-12). 스탬프는 check.sh가 성공 끝에 찍는다.
cd "$(dirname "$0")/../.."
PY=".venv32/Scripts/python.exe"; [ -x "$PY" ] || PY=".venv/Scripts/python.exe"
active=$("$PY" -c 'import json,sys; print(json.load(sys.stdin).get("stop_hook_active", False))')
[ "$active" = "True" ] && exit 0            # 이미 훅 때문에 이어진 턴이면 다시 막지 않는다
stamp=".claude/hooks/.last_green"
if [ -f "$stamp" ] && [ -z "$(find kp_arb tests -name '*.py' -newer "$stamp" -print -quit)" ] \
   && [ ! pyproject.toml -nt "$stamp" ]; then
  exit 0                                   # green 뒤 바뀐 코드 없음
fi
out=$(./.claude/hooks/check.sh 2>&1) && exit 0
printf '%s\n' "check.sh 빨강 — 고치고 다시 돌릴 것" "$(printf '%s\n' "$out" | tail -30)" >&2
exit 2
