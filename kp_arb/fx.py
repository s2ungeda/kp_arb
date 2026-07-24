"""FX 노출 계산 (DESIGN.md §9). 외부 #2로 보고할 값. 순수 로직.

- `domestic_krw_notional`: 외부 #2로 전송하는 `total_coin`(국내 롱 다리 KRW 명목).
- `usd_exposure`: HL perp USD 명목(내부 감사/기록용).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .domain.enums import Instrument, Side, Underlying
from .domain.models import Position

# total_coin 승수(instrument별). KR_ETF는 2x 레버리지 단일종목 ETF 가정 — config로 조정 가능.
_DEFAULT_MULTIPLIERS: dict[Instrument, float] = {
    Instrument.KR_STOCK: 1.0,
    Instrument.KR_STOCK_FUTURE: 10.0,  # 단일종목선물 승수
    Instrument.KR_ETF: 2.0,            # 2x 레버리지 ETF
}


def domestic_krw_notional(
    positions: Iterable[Position],
    multipliers: Mapping[Instrument, float] | None = None,
) -> float:
    """외부 #2 `total_coin`: 국내 롱 다리 KRW 명목 합.

    (주식잔고×평단×1) + (주식선물 매수계약×평단×10) + (레버ETF×평단×2).
    국내 다리는 전략상 매수(롱) 전용 → BUY만 집계. HL/미지 instrument는 제외.
    """
    mult = dict(multipliers) if multipliers is not None else dict(_DEFAULT_MULTIPLIERS)
    total = 0.0
    for p in positions:
        if p.side is not Side.BUY:
            continue
        factor = mult.get(p.instrument)
        if factor is None:
            continue
        total += p.qty * p.avg_price * factor
    return total


def hl_coin_notional(positions: Iterable[Position]) -> float:
    """외부 #2 `total_coin`: HL 보유종목 **Σ(평균단가 × 수량)** (사용자 확정 2026-07-24).

    HL perp의 USD 명목(진입 평균가 기준). 수량은 절대값(magnitude) — #2가 환헤지할
    USD 규모. 종목(SMSN/SKHX/HYUNDAI) 전체 합. HL 외 instrument는 제외.
    """
    total = 0.0
    for p in positions:
        if p.instrument is Instrument.HL_PERP:
            total += p.avg_price * p.qty
    return total


def usd_exposure(
    positions: Iterable[Position],
    hl_mark_usd: dict[Underlying, float] | None = None,
) -> float:
    """HL perp 포지션의 signed USD 명목 합. mark 미제공 시 avg_price 사용.

    국내 포지션은 원화 자산이라 USD 노출에서 제외(내재 FX는 미미, DESIGN.md §9).
    """
    marks = hl_mark_usd or {}
    total = 0.0
    for p in positions:
        if p.instrument is not Instrument.HL_PERP:
            continue
        mark = marks.get(p.underlying, p.avg_price)
        total += p.signed_qty * mark
    return total
