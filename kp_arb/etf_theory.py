"""ETF 이론가(iNAV) 계산 — 순수 로직 (문서: ETF 이론가.md).

문서 §1 공식(등락률 기반 — 기초 KRX 체결의 `drate` 필드 사용, §2):
- 장전~정규장: 이론가 = 전일NAV × (1 + 배율 × 기초 KRX 등락률)
- 애프터장:    이론가 = 당일종가NAV × (1 + 배율 × (기초 애프터 현재가 ÷ 기초 당일종가 − 1))
  (당일종가NAV = 정규장 공식의 15:30 값 — KRX 등락률이 종가 등락률로 고정되므로 그대로 계산)
- 계산 불가(등락률 미수신 등) 대체 순서: 거래소 공식 iNAV → 전일NAV.
- 괴리율 = (ETF 현재가 − 이론가) ÷ 이론가 × 100.
동시호가 '동시이론가'(기초 예상체결가 기반)는 추후(전략 단계) 확장.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EtfTheoryInputs:
    """시작 시 1회 조회(t1901)로 채우는 고정 입력."""

    prev_nav: float                     # 전일NAV (jnilnav)
    leverage: float                     # 배율 (인버스는 음수로 보정해 넣을 것)
    exchange_inav: float | None = None  # 거래소 공식 iNAV (nav — 대체용)


def _fallback(inputs: EtfTheoryInputs) -> float | None:
    """계산 불가 시 대체 순서(문서 §1): 거래소 공식 iNAV → 전일NAV."""
    if inputs.exchange_inav is not None and inputs.exchange_inav > 0:
        return inputs.exchange_inav
    return inputs.prev_nav if inputs.prev_nav > 0 else None


def theory_regular(
    inputs: EtfTheoryInputs | None, base_rate_pct: float | None
) -> float | None:
    """장전~정규장 이론가. base_rate_pct = 기초 KRX 등락률(%, S3_ drate)."""
    if inputs is None:
        return None
    if base_rate_pct is None:
        return _fallback(inputs)
    return inputs.prev_nav * (1.0 + inputs.leverage * base_rate_pct / 100.0)


def theory_after(
    inputs: EtfTheoryInputs | None,
    close_rate_pct: float | None,
    base_close: float | None,
    base_after: float | None,
) -> float | None:
    """애프터장 이론가 = 당일종가NAV × (1 + 배율 × 애프터 등락률).

    close_rate_pct = 기초 KRX 종가 등락률(15:30에 고정된 drate),
    base_close = 기초 KRX 종가(마지막 KRX 체결), base_after = 기초 애프터 현재가(통합).
    애프터 체결이 아직 없으면 당일종가NAV 그대로.
    """
    close_nav = theory_regular(inputs, close_rate_pct)
    if close_nav is None or inputs is None:
        return close_nav
    if (base_after is None or base_after <= 0
            or base_close is None or base_close <= 0):
        return close_nav
    return close_nav * (1.0 + inputs.leverage * (base_after / base_close - 1.0))


def disparity_pct(etf_price: float | None, theory: float | None) -> float | None:
    """괴리율(%) = (ETF 현재가 − 이론가) ÷ 이론가 × 100. (전략 입력용)"""
    if etf_price is None or theory is None or theory <= 0:
        return None
    return (etf_price - theory) / theory * 100.0
