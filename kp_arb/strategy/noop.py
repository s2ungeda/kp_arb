"""플레이스홀더 전략 — 항상 빈 주문. 실제 전략이 이 인터페이스를 구현한다."""
from __future__ import annotations

from collections.abc import Sequence

from ..domain.models import MarketState, OrderIntent
from .base import Strategy


class NoopStrategy(Strategy):
    def evaluate(self, state: MarketState) -> Sequence[OrderIntent]:
        return []
