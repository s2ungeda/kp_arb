"""ETF 이론가(iNAV) 계산 — 순수 로직 (문서: ETF 이론가.md).

v1 범위는 **정규장 공식**:
    이론가 = 전일NAV × (1 + 배율 × 기초 KRX 등락률)
기초 등락률 = 기초 KRX 현재가 ÷ 기초 전일종가 − 1.

계산이 불가능하면 대체 순서(문서 §1): 거래소 공식 iNAV → 전일NAV.
주의: 기초가는 반드시 KRX 기준(통합/NXT 섞으면 공식 iNAV와 어긋남 — 문서 §4-1).
애프터장·동시호가 공식은 전략(Phase 7)에서 확장 예정.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EtfTheoryInputs:
    """시작 시 1회 조회로 채우는 고정 입력 (t1901 + 기초 t1102)."""

    prev_nav: float                     # 전일NAV (t1901 jnilnav)
    leverage: float                     # 배율 (t1901 leverage — 인버스는 음수로 보정해 넣을 것)
    base_prev_close: float              # 기초 전일종가 (t1102 jnilclose, KRX)
    exchange_inav: float | None = None  # 거래소 공식 iNAV (t1901 nav — 대체용)


def theory_price(
    inputs: EtfTheoryInputs | None, base_price_krx: float | None
) -> float | None:
    """정규장 이론가. 기초가가 없으면 대체 순서(공식 iNAV → 전일NAV)."""
    if inputs is None:
        return None
    if base_price_krx is None or base_price_krx <= 0 or inputs.base_prev_close <= 0:
        # 기초 시세가 아직 없음 — 대체 순서.
        if inputs.exchange_inav is not None and inputs.exchange_inav > 0:
            return inputs.exchange_inav
        return inputs.prev_nav if inputs.prev_nav > 0 else None
    base_return = base_price_krx / inputs.base_prev_close - 1.0
    return inputs.prev_nav * (1.0 + inputs.leverage * base_return)


def disparity_pct(etf_price: float | None, theory: float | None) -> float | None:
    """괴리율(%) = (ETF 현재가 − 이론가) ÷ 이론가 × 100. (전략 입력용)"""
    if etf_price is None or theory is None or theory <= 0:
        return None
    return (etf_price - theory) / theory * 100.0
