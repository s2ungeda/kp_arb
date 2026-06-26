"""전략 플러그인 계약 (DESIGN.md §6). 구체 전략은 추후 결정."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ..domain.models import MarketState, OrderIntent


class Strategy(ABC):
    @abstractmethod
    def evaluate(self, state: MarketState) -> Sequence[OrderIntent]: ...
