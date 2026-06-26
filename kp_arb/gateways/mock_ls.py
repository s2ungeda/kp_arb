"""테스트/리플레이용 LS 목 게이트웨이. 라이브 API 호출 없음.

녹화한 응답(잔고·포지션)을 seed_*로 주입하고, place_order는 계좌 라우팅을 검증한 뒤
주문을 기록만 한다. 하네스에서 게이트웨이를 라이브 없이 검증하기 위한 것.
"""
from __future__ import annotations

import itertools
from collections.abc import Sequence

from ..domain.enums import Account, Venue
from ..domain.models import OrderIntent, Position
from ..routing import account_for
from .base import LSGateway


class MockLSGateway(LSGateway):
    def __init__(self) -> None:
        self.connected = False
        self.placed: list[OrderIntent] = []
        self._ids = itertools.count(1)
        self._positions: dict[Account, list[Position]] = {
            Account.KR_STOCK: [],
            Account.KR_DERIV: [],
        }
        self._balances: dict[Account, float] = {Account.KR_STOCK: 0.0, Account.KR_DERIV: 0.0}

    async def connect(self) -> None:
        self.connected = True

    async def place_order(self, intent: OrderIntent) -> str:
        if intent.venue is not Venue.LS:
            raise ValueError("MockLSGateway only handles LS orders")
        expected = account_for(intent.instrument)
        if intent.account != expected:
            raise ValueError(f"routing mismatch: {intent.account} != {expected}")
        self.placed.append(intent)
        return f"LS-{next(self._ids)}"

    async def cancel_order(self, order_id: str) -> None:
        return None

    async def get_positions(self, account: Account) -> Sequence[Position]:
        return list(self._positions[account])

    async def get_balance(self, account: Account) -> float:
        return self._balances[account]

    # --- 테스트 픽스처 주입 헬퍼 ---
    def seed_position(self, position: Position) -> None:
        if position.account is None:
            raise ValueError("position needs an account")
        self._positions[position.account].append(position)

    def seed_balance(self, account: Account, amount: float) -> None:
        self._balances[account] = amount
