"""국내 호가단위(틱)·틱 반올림·maker 가격 보정 — 순수 로직 (DESIGN §6.2-4).

호가가격단위 표: KRX 주식/ETF(2023-01 개편)·주식선물.
[실측 확인 예정 — 주문 거부 01427(가격범위)이 나면 이 표부터 의심할 것.]
"""
from __future__ import annotations

import math

from .domain.enums import Instrument, Side

# (미만 상한, 틱) — 마지막 밴드는 그 이상 전부
_STOCK_BANDS = ((2_000, 1), (5_000, 5), (20_000, 10),
                (50_000, 50), (200_000, 100), (500_000, 500))
_STOCK_TOP_TICK = 1_000
_FUTURES_BANDS = ((10_000, 10), (50_000, 50), (100_000, 100), (500_000, 500))
_FUTURES_TOP_TICK = 1_000


def stock_tick(price: float) -> int:
    """주식/ETF 호가단위."""
    for limit, tick in _STOCK_BANDS:
        if price < limit:
            return tick
    return _STOCK_TOP_TICK


def stock_futures_tick(price: float) -> int:
    """주식선물 호가단위."""
    for limit, tick in _FUTURES_BANDS:
        if price < limit:
            return tick
    return _FUTURES_TOP_TICK


def tick_for(instrument: Instrument, price: float) -> int:
    """국내 상품의 호가단위. HL은 대상 아님(자체 규칙)."""
    if instrument is Instrument.KR_STOCK_FUTURE:
        return stock_futures_tick(price)
    return stock_tick(price)


def floor_to_tick(price: float, tick: int) -> float:
    """틱 내림 — 매수 주문가용 (계산가보다 불리하게 깎지 않는 방향)."""
    return math.floor(price / tick + 1e-9) * tick


def ceil_to_tick(price: float, tick: int) -> float:
    """틱 올림 — 매도 주문가용."""
    return math.ceil(price / tick - 1e-9) * tick


def maker_cap(
    side: Side,
    price: float,
    best_ask: float | None,
    best_bid: float | None,
    tick: int,
) -> float:
    """maker 유지 보정 (사용자 확정 2026-07-22).

    역산 주문가가 반대편 1호가를 침범하면 taker가 돼버리므로:
    매수 주문가 ≥ 매도1호가 → 매도1호가 − 1틱 / 매도 주문가 ≤ 매수1호가 → 매수1호가 + 1틱.
    """
    if side is Side.BUY and best_ask is not None and price >= best_ask:
        return best_ask - tick
    if side is Side.SELL and best_bid is not None and price <= best_bid:
        return best_bid + tick
    return price
