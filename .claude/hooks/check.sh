#!/usr/bin/env bash
# 검증 루프: 편집 후 / 작업 종료 전에 돌린다. 모두 통과해야 "완료".
set -euo pipefail
cd "$(dirname "$0")/../.."
echo "▶ ruff"
ruff check kp_arb tests
echo "▶ mypy"
mypy kp_arb
echo "▶ pytest"
pytest -q
echo "✅ all green"
