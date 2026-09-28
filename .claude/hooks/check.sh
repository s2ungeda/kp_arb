#!/usr/bin/env bash
# 검증 루프: 편집 후 / 작업 종료 전에 돌린다. 모두 통과해야 "완료".
set -euo pipefail
cd "$(dirname "$0")/../.."

# 프로젝트 venv python 우선(시스템 PATH python이 3.8이라 직접 실행 시 실패).
# 2026-09-23: xingAPI(32비트 COM)를 코어 안에 두기로 해 **32비트 venv(.venv32)**가 기준 —
# 배포판도 32비트로 빌드하므로 검증도 같은 비트로 돈다(DESIGN-ls-xing.md §1). 없으면 옛 .venv.
if [ -x ".venv32/Scripts/python.exe" ]; then
  PY=".venv32/Scripts/python.exe"    # Windows 32비트 venv(기준)
elif [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"      # Windows 64비트 venv(옛 기준)
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"              # POSIX venv
else
  PY="python"                        # 폴백: PATH
fi

echo "▶ ruff";   "$PY" -m ruff check kp_arb tests
echo "▶ mypy";   "$PY" -m mypy kp_arb
echo "▶ pytest"; "$PY" -m pytest -q
echo "✅ all green"
