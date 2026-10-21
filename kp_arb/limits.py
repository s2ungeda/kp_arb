"""HL 한도 — 순수 로직. 일일 체결액 한도(DESIGN-settings §1) + 리스크 한도(§1a, 2026-10-21).

- 당일 HL 체결액(USDC)을 누적하고, 날짜가 바뀌면 0으로 리셋(자정 리셋).
- 발주 시 ``당일 체결액 + 이 주문 금액 > 한도`` 면 거부(하드블록). 한도 0/음수면 무제한.

외부 I/O 없음 — 오늘 날짜는 호출자(코어)가 문자열로 넘긴다.
"""
from __future__ import annotations

from .domain.enums import Side


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


# --- 리스크 한도 (DESIGN-settings §1a, 사용자 2026-10-20) — HL 주문 전용, 걸리면 거부 ---

class RiskLimitExceeded(Exception):
    """HL 리스크 한도(주문가격한도·1회수량한도) 위반 — 발주 거부(수동·전략 공통, place에서
    raise)."""


def price_limit_error(side: Side, price: float | None, oracle: float | None,
                      pct: float) -> str | None:
    """주문가격한도 — 매도는 ``주문가 > 오라클×(1−x)``, 매수는 ``주문가 < 오라클×(1+x)`` 이어야
    발주.
    x(pct)는 소수(0.02 = 2%). 0 이하면 검사 없음. **오라클이 없으면 거부**(사용자 10-20 "오라클은
    받아야지" — 검사 못 하면 안 낸다). 가격 없음(시장가)은 호출자가 마크가를 넣는다."""
    if pct <= 0:
        return None
    if oracle is None or oracle <= 0:
        return "HL 리스크 한도 — 오라클 미수신(주문가격한도 검사 불가)"
    if price is None or price <= 0:
        return "HL 리스크 한도 — 주문 가격 없음(주문가격한도 검사 불가)"
    if side is Side.SELL:
        floor = oracle * (1.0 - pct)
        if price <= floor:
            return (f"HL 리스크 한도 — 매도 주문가 {price:g} ≤ 오라클 {oracle:g}×(1−{pct * 100:g}%)"
                    f"={floor:g}")
        return None
    cap = oracle * (1.0 + pct)
    if price >= cap:
        return (f"HL 리스크 한도 — 매수 주문가 {price:g} ≥ 오라클 {oracle:g}×(1+{pct * 100:g}%)"
                f"={cap:g}")
    return None


def qty_limit_error(qty: float, max_qty: float) -> str | None:
    """1회수량한도 — 한 주문의 수량(HL 수량)이 한도 **이상이면** 거부(엑셀 H8 "이상", 사용자
    2026-10-21 확인 — 한도 값 자체도 거부). 0 이하면 없음."""
    if max_qty <= 0:
        return None
    if abs(qty) >= max_qty:
        return f"HL 리스크 한도 — 1회수량 {abs(qty):g} ≥ 한도 {max_qty:g}"
    return None
