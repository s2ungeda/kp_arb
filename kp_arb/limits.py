"""일일 HL 체결액 한도 — 순수 로직 (DESIGN-settings §1).

- 당일 HL 체결액(USDC)을 누적하고, 날짜가 바뀌면 0으로 리셋(자정 리셋).
- 발주 시 ``당일 체결액 + 이 주문 금액 > 한도`` 면 거부(하드블록). 한도 0/음수면 무제한.

외부 I/O 없음 — 오늘 날짜는 호출자(코어)가 문자열로 넘긴다.
"""
from __future__ import annotations


class DailyLimitExceeded(Exception):
    """HL 일일 체결액 한도 초과 — 발주 거부(수동·전략 공통, place_order에서 raise)."""


def would_exceed_daily_limit(
    filled_today: float, order_notional: float, limit: float
) -> bool:
    """한도(>0)일 때 ``당일 체결액 + 이 주문 금액``이 한도를 넘으면 True.

    한도가 0 이하이면 무제한(항상 False). 정확히 한도와 같으면 허용(초과 아님).
    """
    if limit <= 0:
        return False
    return filled_today + order_notional > limit


class DailyFilled:
    """당일 HL 체결액(USDC) 누적 — 날짜가 바뀌면 자동 리셋(자정 리셋).

    날짜는 ``'YYYYMMDD'`` 같은 문자열로 호출자가 준다(코어가 로컬 자정 기준으로 넘김).
    현재 날짜의 누적만 유지한다 — 과거 날짜를 물으면 0(이미 리셋된 것으로 본다).
    """

    def __init__(self) -> None:
        self._day: str = ""
        self._total: float = 0.0

    def add(self, day: str, amount: float) -> float:
        """당일에 체결액을 더한다(날이 바뀌었으면 먼저 0으로). 누적값을 반환."""
        if day != self._day:
            self._day, self._total = day, 0.0
        self._total += amount
        return self._total

    def total(self, day: str) -> float:
        """그 날짜 기준 누적 체결액 — 날이 다르면 0(리셋됨)."""
        return self._total if day == self._day else 0.0
