#!/usr/bin/env bash
# 검증 루프: 편집 후 / 작업 종료 전에 돌린다. 모두 통과해야 "완료".
set -euo pipefail
cd "$(dirname "$0")/../.."

# 프로젝트 venv python 우선(시스템 PATH python이 3.8이라 직접 실행 시 실패).
if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"      # Windows venv
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"              # POSIX venv
else
  PY="python"                        # 폴백: PATH
fi

echo "▶ ruff";   "$PY" -m ruff check kp_arb tests
echo "▶ mypy";   "$PY" -m mypy kp_arb
echo "▶ pytest"; "$PY" -m pytest -q
echo "✅ all green"
