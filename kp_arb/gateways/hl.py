"""Hyperliquid HIP-3 perp 게이트웨이 (DESIGN.md §5.2).

블록 3-1 범위: connect + 에이전트 서명(HLAuth 보유). 시세/주문/펀딩은 블록 3-2에서 채운다.
"""
from __future__ import annotations

from collections.abc import Sequence

from ..domain.enums import Underlying
from ..domain.models import OrderIntent, Position
from .base import HLGateway
from .hl_auth import HLAuth, HLAuthError, HLSigner


class HLApiGateway(HLGateway):
    """HL 게이트웨이. 에이전트 서명(HLAuth)을 보유하고 연결한다."""

    def __init__(self, signer: HLSigner) -> None:
        self._auth = HLAuth(signer)
        self.connected = False

    @property
    def auth(self) -> HLAuth:
        return self._auth

    async def connect(self) -> None:
        if not self._auth.agent_address:
            raise HLAuthError("agent address required to connect")
        self.connected = True

    async def place_order(self, intent: OrderIntent) -> str:
        raise NotImplementedError("HL 주문은 블록 3-2")

    async def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError("HL 취소는 블록 3-2")

    async def get_positions(self) -> Sequence[Position]:
        raise NotImplementedError("HL 포지션은 블록 3-2")

    async def get_funding(self, underlying: Underlying) -> float:
        raise NotImplementedError("HL 펀딩은 블록 3-2")
