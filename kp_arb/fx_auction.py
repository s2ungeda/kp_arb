"""원달러선물 동시호가 대응주문 — 순수 로직 (DESIGN-fx-auction.md §4).

주식선물 신규주문 → 원달러선물 헤지 주문의 **방향·가격·수량**과 **시간창 판정**만 계산한다.
I/O·주문 실행은 코어(감시 태스크)와 게이트웨이 몫. 여기는 부작용 없는 순수 함수.
"""
from __future__ import annotations

import math
from collections.abc import Iterable

from .domain.enums import Side

FX_TICK = 0.1          # 원달러선물 호가단위(원)
FX_CONTRACT_USD = 10_000  # 원달러선물 1계약 = US$10,000
STOCK_FUT_MULT = 10    # 주식선물 1계약 = 10주


def _hms_to_sec(s: str) -> int | None:
    """'HH:MM' 또는 'HH:MM:SS' → 자정 이후 초. 형식 이상은 None."""
    parts = s.strip().split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    h, m = nums[0], nums[1]
    sec = nums[2] if len(parts) == 3 else 0
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60):
        return None
    return h * 3600 + m * 60 + sec


def in_auction_window(now_hms: str, windows: Iterable[tuple[str, str]]) -> bool:
    """now(HH:MM:SS)가 주어진 시간창 [시작,종료] 중 하나에 드는가(경계 포함).

    windows: [(장전시작, 장전종료), (마감시작, 마감종료)] 같은 (시작,종료) 목록.
    형식이 이상한 값(빈칸 등)은 그 창만 건너뛴다.
    """
    now = _hms_to_sec(now_hms)
    if now is None:
        return False
    for start_s, end_s in windows:
        start, end = _hms_to_sec(start_s), _hms_to_sec(end_s)
        if start is None or end is None:
            continue
        if start <= now <= end:
            return True
    return False


def hedge_side(stock_side: Side) -> Side:
    """주식선물 방향 → 원달러선물 대응 방향(반대). 매수→매도 / 매도→매수."""
    return Side.SELL if stock_side is Side.BUY else Side.BUY


def hedge_price(fx_price: float, tick_count: int, fx_side: Side,
                tick_size: float = FX_TICK) -> float:
    """대응 주문가 — 매수는 현재가 +주문틱, 매도는 −주문틱. 주문틱 = 틱개수 × 틱크기.

    0.1 그리드로 스냅(부동소수 잡음 제거)."""
    offset = tick_count * tick_size
    px = fx_price + offset if fx_side is Side.BUY else fx_price - offset
    steps = round(px / tick_size)
    return round(steps * tick_size, 4)


def hedge_qty(contracts: int, stock_price: float, fx_price: float,
              hedge_ratio: float, *, multiplier: int = STOCK_FUT_MULT,
              fx_contract_usd: float = FX_CONTRACT_USD) -> int:
    """대응 계약수 = 반내림( 계약수 × 승수 × 주식선물주문가 ÷ 현재가 ÷ 계약USD × 헤지비율 ).

    hedge_ratio는 비율(예 50% → 0.5). 헤지비율을 반내림 안에 넣어 정수 계약. 0 이하는 0.
    """
    if fx_price <= 0 or contracts <= 0 or hedge_ratio <= 0:
        return 0
    raw = contracts * multiplier * stock_price / fx_price / fx_contract_usd * hedge_ratio
    return max(0, math.floor(raw))


def compute_hedge(stock_side: Side, contracts: int, stock_price: float,
                  fx_price: float, tick_count: int, hedge_ratio: float,
                  ) -> tuple[Side, float, int]:
    """주식선물 신규주문 → 원달러선물 대응주문 (방향, 주문가, 계약수). 수량 0이면 발주 안 함.

    hedge_ratio는 비율(0.5 = 50%). fx_price는 화면 입력 현재가(고정).
    """
    fx_side = hedge_side(stock_side)
    px = hedge_price(fx_price, tick_count, fx_side)
    qty = hedge_qty(contracts, stock_price, fx_price, hedge_ratio)
    return fx_side, px, qty
