"""LS TR별 초당 한도 표(공식 TR 목록 transaction_per_sec) — 우리 한도 지킴이가 이 표를 쓴다."""
from __future__ import annotations

from kp_arb.gateways.ls_rest import LS_PER_SECOND, RateLimiter, RateLimitError


def test_official_per_tr_limits_present() -> None:
    # 실측 2026-09-08: 기본 2회를 전 TR에 적용해 선물 취소(공식 10회)를 우리 쪽에서 막았음.
    assert LS_PER_SECOND["CFOAT00300"] == 10 and LS_PER_SECOND["CFOAT00100"] == 10
    assert LS_PER_SECOND["CSPAT00601"] == 10 and LS_PER_SECOND["CSPAT00801"] == 3
    assert LS_PER_SECOND["CFOBQ10500"] == 1 and LS_PER_SECOND["t1901"] == 1


def test_limiter_uses_table_per_tr() -> None:
    clock = [1000.0]
    lim = RateLimiter(now=lambda: clock[0], per_tr_per_second=LS_PER_SECOND)
    for _ in range(10):  # 선물 취소 초당 10회까지 허용
        lim.check("CFOAT00300")
    try:
        lim.check("CFOAT00300")
    except RateLimitError:
        pass
    else:  # pragma: no cover - 실패 경로
        raise AssertionError("11번째 취소는 막혀야 함")
    lim.check("t2111")  # 표에 없는 TR은 기본값(2)
    lim.check("t2111")
    try:
        lim.check("t2111")
    except RateLimitError:
        pass
    else:  # pragma: no cover
        raise AssertionError("표에 없는 TR은 기본 2회")
