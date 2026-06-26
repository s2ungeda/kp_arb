"""FX 노출 계산 (DESIGN.md §9). 외부 #2로 보고할 USD 순노출. 순수 로직."""
from __future__ import annotations

from collections.abc import Iterable

from .domain.enums import Instrument, Underlying
from .domain.models import Position


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
