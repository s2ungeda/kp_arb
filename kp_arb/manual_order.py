"""수동 주문(일반 주문창) 순수 검증 로직 (DESIGN-manual-order.md §6.3).

여기엔 **I/O 없는 순수 함수만** 둔다. OrderIntent가 이미 수량>0·가격·계좌 라우팅을
검증하므로(models.py), 이 모듈의 고유 책임은 **공매도(숏) 판정** — 국내 현물 매도가
보유(정확히는 매도가능수량)를 넘지 않는지다(§3 불변식). 실제 잔고·미체결 값은 코어의
OrderBook에서 가져와 이 함수들에 넣는다.
"""
from __future__ import annotations

from .domain.enums import Instrument, Side

# 공매도 제약이 걸리는 국내 현물(spot). 선물·HL perp는 양방향(숏 허용)이라 제외.
_SPOT_INSTRUMENTS: frozenset[Instrument] = frozenset(
    {Instrument.KR_STOCK, Instrument.KR_ETF}
)


def is_spot_stock(instrument: Instrument) -> bool:
    """국내 현물(공매도 금지 대상)인가. (순수 함수)"""
    return instrument in _SPOT_INSTRUMENTS


def sellable_qty(held: float, pending_sell: float) -> float:
    """매도가능수량 = 보유수량 − 미체결 매도수량. 음수는 0으로. (순수 함수)"""
    return max(0.0, held - pending_sell)


def short_sale_error(
    instrument: Instrument, side: Side, qty: float, sellable: float
) -> str | None:
    """국내 현물 매도가 매도가능수량을 넘으면 공매도 사유 문자열, 아니면 None.

    현물이 아니거나(선물·HL) 매수면 항상 None — 숏 제약 없음. (순수 함수)
    """
    if side is not Side.SELL or not is_spot_stock(instrument):
        return None
    if qty > sellable:
        return f"공매도 금지: 매도수량 {qty:g} > 매도가능 {sellable:g}"
    return None
