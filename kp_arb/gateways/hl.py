"""Hyperliquid HIP-3 perp 게이트웨이 (DESIGN.md §5.2).

블록 3-1: connect + 에이전트 서명(HLAuth).
블록 3-2: 마크 구독 / 펀딩 / 포지션 / 주문·취소.
- 심볼은 config 주입(정확한 HIP-3 심볼·펀딩 주기는 [OPEN §13 #4], 하드코딩 금지).
- HL 주문은 KR 계좌를 갖지 않는다(OrderIntent 불변식이 보장).
- REST(/info·/exchange)는 주입형 ``HLTransport``, 마크 WS는 ``MarkConnection`` 뒤로 격리.
  exchange 요청은 HLAuth로 서명. 정확한 액션/응답 스키마는 라이브 시 hyperliquid-sdk로 확인.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel

from ..domain.enums import Instrument, OrderType, Side, Underlying, Venue
from ..domain.models import OrderIntent, Position
from .base import HLGateway
from .hl_auth import HLAuth, HLAuthError, HLSigner


class HLError(RuntimeError):
    """HL REST/주문 응답 오류."""


class Mark(BaseModel):
    """perp 마크 가격 이벤트."""

    underlying: Underlying
    price: float
    ts: float = 0.0


class HLTransport(Protocol):
    """HL REST(/info·/exchange) 전송 계약. 라이브는 aiohttp, 테스트는 mock."""

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]: ...


class MarkConnection(Protocol):
    """마크 WS 세션. 구독 전송 + 프레임 비동기 수신."""

    async def send(self, message: str) -> None: ...
    def __aiter__(self) -> AsyncIterator[str]: ...


class HLApiGateway(HLGateway):
    """HL 게이트웨이. 에이전트 서명(HLAuth) + REST + 마크 WS."""

    INFO_PATH = "/info"
    EXCHANGE_PATH = "/exchange"

    def __init__(
        self,
        signer: HLSigner,
        transport: HLTransport | None = None,
        *,
        symbols: Mapping[Underlying, str] | None = None,
        nonce_fn: Callable[[], int] | None = None,
    ) -> None:
        self._auth = HLAuth(signer)
        self._tx = transport
        self._symbols: dict[Underlying, str] = dict(symbols or {})
        self._by_symbol: dict[str, Underlying] = {v: k for k, v in self._symbols.items()}
        self._nonce_fn: Callable[[], int] = nonce_fn or (lambda: int(time.time() * 1000))
        self.connected = False
        self._mark_coins: list[str] = []
        self.on_mark: list[Callable[[Mark], None]] = []

    @property
    def auth(self) -> HLAuth:
        return self._auth

    async def connect(self) -> None:
        if not self._auth.agent_address:
            raise HLAuthError("agent address required to connect")
        self.connected = True

    # --- 주문 ---

    async def place_order(self, intent: OrderIntent) -> str:
        if intent.venue is not Venue.HYPERLIQUID:
            raise ValueError("HLApiGateway only handles Hyperliquid orders")
        coin = self._symbol(intent.underlying)
        order = {
            "coin": coin,
            "is_buy": intent.side is Side.BUY,
            "sz": intent.qty,
            "limit_px": intent.price,
            "order_type": (
                {"limit": {"tif": "Gtc"}}
                if intent.order_type is OrderType.LIMIT
                else {"market": {}}
            ),
            "reduce_only": False,
        }
        resp = await self._exchange({"type": "order", "orders": [order]})
        return self._parse_oid(resp)

    async def cancel_order(self, order_id: str) -> None:
        resp = await self._exchange({"type": "cancel", "cancels": [{"oid": int(order_id)}]})
        self._check_ok(resp)

    # --- 조회 ---

    async def get_positions(self) -> Sequence[Position]:
        resp = await self._info({"type": "clearinghouseState"})
        positions: list[Position] = []
        for asset in resp.get("assetPositions", []):
            pos = asset.get("position", {})
            underlying = self._by_symbol.get(str(pos.get("coin", "")))
            if underlying is None:
                continue
            szi = float(pos["szi"])
            if szi == 0:
                continue
            positions.append(
                Position(
                    venue=Venue.HYPERLIQUID,
                    instrument=Instrument.HL_PERP,
                    underlying=underlying,
                    side=Side.BUY if szi > 0 else Side.SELL,
                    qty=abs(szi),
                    avg_price=float(pos["entryPx"]),
                    account=None,  # HL은 KR 계좌 없음
                )
            )
        return positions

    async def get_funding(self, underlying: Underlying) -> float:
        coin = self._symbol(underlying)
        resp = await self._info({"type": "funding", "coin": coin})
        return float(resp["funding"])

    # --- 마크 WS ---

    def subscribe_mark(self, underlying: Underlying) -> None:
        coin = self._symbol(underlying)
        if coin not in self._mark_coins:
            self._mark_coins.append(coin)

    async def stream_marks(self, conn: MarkConnection) -> None:
        """구독 전송 후 프레임을 받아 on_mark로 디스패치(정상 종료까지)."""
        for coin in self._mark_coins:
            await conn.send(
                json.dumps({"method": "subscribe", "subscription": {"type": "mark", "coin": coin}})
            )
        async for raw in conn:
            mark = self._parse_mark(raw)
            if mark is not None:
                for handler in self.on_mark:
                    handler(mark)

    # --- 내부 ---

    async def _exchange(self, action: dict[str, Any]) -> dict[str, Any]:
        signed = self._auth.signed_request(action, self._nonce_fn())
        return await self._require_tx().post(self.EXCHANGE_PATH, signed)

    async def _info(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._require_tx().post(self.INFO_PATH, body)

    def _require_tx(self) -> HLTransport:
        if self._tx is None:
            raise HLError("transport not configured")
        return self._tx

    def _symbol(self, underlying: Underlying) -> str:
        try:
            return self._symbols[underlying]
        except KeyError as exc:
            raise HLError(f"no HL symbol configured for {underlying}") from exc

    def _check_ok(self, resp: dict[str, Any]) -> None:
        if resp.get("status") != "ok":
            raise HLError(f"HL rejected: {resp.get('response')}")

    def _parse_oid(self, resp: dict[str, Any]) -> str:
        self._check_ok(resp)
        try:
            status = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise HLError("cannot parse HL order response") from exc
        for key in ("resting", "filled"):
            if key in status:
                return str(status[key]["oid"])
        raise HLError(f"HL order not accepted: {status}")

    def _parse_mark(self, raw: str) -> Mark | None:
        data = json.loads(raw).get("data", {})
        underlying = self._by_symbol.get(str(data.get("coin", "")))
        if underlying is None:
            return None
        return Mark(underlying=underlying, price=float(data["mark"]), ts=float(data.get("ts", 0.0)))
