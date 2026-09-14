"""kp_arb 패키지. 가장 먼저 임포트되므로 프로세스 시작 기준 시각을 여기서 잡는다(시동 계측용)."""
from __future__ import annotations

import time

PROC_T0 = time.perf_counter()  # 패키지 첫 임포트 시각 ≈ 파이썬 기동 직후(인터프리터 초기화는 제외)


def since_start() -> float:
    """프로세스(패키지 첫 임포트) 시작 후 흐른 초 — 시동 계측 로그용."""
    return time.perf_counter() - PROC_T0
