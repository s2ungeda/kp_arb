"""테스트/리플레이용 Hyperliquid 목 게이트웨이. 라이브 API 호출 없음."""
from __future__ import annotations

import itertools
from collections.abc import Sequence

from ..domain.enums import Underlying, Venue
from ..domain.models import OrderIntent, Position
from .base import HLGateway


class MockHLGateway(HLGateway):
    def __init__(self) -> None:
        self.connected = False
        self.placed: list[OrderIntent] = []
        self._ids = itertools.count(1)
        self._positions: list[Position] = []
        self._funding: dict[Underlying, float] = {}

    async def connect(self) -> None:
        self.connected = True

    async def place_order(self, intent: OrderIntent) -> str:
        if intent.venue is not Venue.HYPERLIQUID:
            raise ValueError("MockHLGateway only handles Hyperliquid orders")
        self.placed.append(intent)
        return f"HL-{next(self._ids)}"

    async def cancel_order(self, order_id: str) -> None:
        return None

    async def get_positions(self) -> Sequence[Position]:
        return list(self._positions)

    async def get_funding(self, underlying: Underlying) -> float:
        return self._funding.get(underlying, 0.0)

    # --- 테스트 픽스처 주입 헬퍼 ---
    def seed_position(self, position: Position) -> None:
        self._positions.append(position)

    def seed_funding(self, underlying: Underlying, rate: float) -> None:
        self._funding[underlying] = rate
