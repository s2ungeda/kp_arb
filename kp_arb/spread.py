"""스프레드(프리미엄) 계산 (DESIGN.md §6.1). 순수 로직.

contract_ratio: HL perp(USD)을 국내 비교단위(KRW)로 환산하는 종목별 계약 사양.
실제 값은 perp 사양 실측으로 캘리브레이션해야 한다([OPEN] DESIGN.md §13).
"""
from __future__ import annotations


def fair_kr_price(hl_mark_usd: float, usdkrw: float, contract_ratio: float) -> float:
    if hl_mark_usd <= 0 or usdkrw <= 0 or contract_ratio <= 0:
        raise ValueError("hl_mark_usd, usdkrw, contract_ratio must all be > 0")
    return hl_mark_usd * usdkrw * contract_ratio


def compute_spread(
    kr_price_krw: float, hl_mark_usd: float, usdkrw: float, contract_ratio: float
) -> float:
    """프리미엄 P = 국내가 / Fair - 1. P > 0 이면 국내 고평가."""
    if kr_price_krw <= 0:
        raise ValueError("kr_price_krw must be > 0")
    fair = fair_kr_price(hl_mark_usd, usdkrw, contract_ratio)
    return kr_price_krw / fair - 1.0
