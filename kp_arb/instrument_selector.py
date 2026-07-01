"""InstrumentSelector (DESIGN.md §5.4). 순수 로직.

(underlying, 방향, 세션 맵) → 최적 국내 instrument + 대상 계좌.
기준: 가용성(tradeable) → 순비용(낮을수록) → 유동성(높을수록).
- 숏(SELL)은 공매도 금지 → 롱 전용 instrument(주식 spot·단일종목 ETF) 선택 불가.
  숏은 선물 매도로만(인버스 ETF는 별도 instrument 미모델링).
- 계좌는 라우팅 불변식 ``routing.account_for`` 재사용.
- 실제 비용·유동성 값은 config([OPEN])로 주입. 미주입 시 기본 우선순위로 tiebreak.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .domain.enums import Account, Instrument, Side, Underlying
from .domain.models import InstrumentStatus
from .routing import account_for

# 숏 불가(공매도 금지) — 롱 전용 국내 instrument.
_LONG_ONLY: frozenset[Instrument] = frozenset({Instrument.KR_STOCK, Instrument.KR_ETF})

# 순비용·유동성 동률 시 tiebreak용 기본 우선순위(실제 선호는 config 주입).
_DEFAULT_PRIORITY: tuple[Instrument, ...] = (
    Instrument.KR_STOCK_FUTURE,
    Instrument.KR_STOCK,
    Instrument.KR_ETF,
    Instrument.KR_NIGHT_FUTURE,
)


@dataclass(frozen=True)
class Selection:
    instrument: Instrument
    account: Account


class InstrumentSelector:
    def __init__(
        self,
        *,
        costs: Mapping[Instrument, float] | None = None,
        liquidity: Mapping[Instrument, float] | None = None,
        priority: tuple[Instrument, ...] | None = None,
    ) -> None:
        self._costs = dict(costs or {})
        self._liquidity = dict(liquidity or {})
        self._priority = tuple(priority or _DEFAULT_PRIORITY)

    def select(
        self,
        underlying: Underlying,
        side: Side,
        session: Mapping[Instrument, InstrumentStatus],
    ) -> Selection | None:
        """거래 가능하고 방향에 맞는 국내 instrument 중 최적 하나 선택. 없으면 None."""
        candidates = [
            instrument
            for instrument, status in session.items()
            if status.tradeable
            and instrument is not Instrument.HL_PERP
            and self._direction_ok(instrument, side)
        ]
        if not candidates:
            return None
        best = min(candidates, key=self._rank)
        return Selection(best, account_for(best))

    def _direction_ok(self, instrument: Instrument, side: Side) -> bool:
        # 숏은 롱 전용 instrument 불가(공매도 금지).
        return not (side is Side.SELL and instrument in _LONG_ONLY)

    def _rank(self, instrument: Instrument) -> tuple[float, float, int]:
        cost = self._costs.get(instrument, 0.0)
        liquidity = self._liquidity.get(instrument, 0.0)
        try:
            priority = self._priority.index(instrument)
        except ValueError:
            priority = len(self._priority)
        # 비용↓ → 유동성↑ → 기본 우선순위. min()으로 최적 선택.
        return (cost, -liquidity, priority)
