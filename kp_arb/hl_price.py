"""HL 주문가 격자 맞춤 — 순수 로직 (DESIGN-auto-m §4 후주문 지정가).

HL 규칙(공식 문서 'Tick and lot size'): 가격은 **유효숫자 5자리 이하**이고, 소수 자릿수는
**MAX_DECIMALS − szDecimals**(무기한 MAX_DECIMALS=6) 이하여야 한다. 정수 가격은 유효숫자와
무관하게 항상 허용. 어기면 주문이 통째로 거부된다 — 실측 2026-09-07 자동M 후주문
"Price must be divisible by tick size"(SKHX 1,281.06 → 6자리).

taker로 잡히게 하는 지정가이므로 **공격적인 쪽으로** 맞춘다: 매수는 올림, 매도는 내림.
"""
from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .domain.enums import Side

MAX_DECIMALS_PERP = 6
SIG_FIGS = 5


def hl_price_decimals(price: float, sz_decimals: int | None,
                      max_decimals: int = MAX_DECIMALS_PERP) -> int:
    """허용 소수 자릿수 — 유효숫자 5자리 한도와 자릿수 한도 중 작은 쪽(정수는 항상 허용)."""
    if price <= 0:
        return 0
    by_sig = SIG_FIGS - 1 - math.floor(math.log10(price))  # 197.48 → 2, 1281.06 → 1, 12.345 → 3
    by_dec = max_decimals - (sz_decimals or 0)
    return max(0, min(by_sig, by_dec))


def hl_round_price(price: float, side: Side, sz_decimals: int | None,
                   max_decimals: int = MAX_DECIMALS_PERP) -> float:
    """HL 격자에 맞춘 지정가 — 매수 올림 / 매도 내림(공격적). 0 이하면 그대로."""
    if price <= 0:
        return price
    decimals = hl_price_decimals(price, sz_decimals, max_decimals)
    quantum = Decimal(1).scaleb(-decimals)
    mode = ROUND_CEILING if side is Side.BUY else ROUND_FLOOR
    out = Decimal(repr(price)).quantize(quantum, rounding=mode)
    # 올림으로 자릿수가 한 단계 오르면(999.99→1000.0) 그 값은 정수라 항상 허용
    return float(out)
