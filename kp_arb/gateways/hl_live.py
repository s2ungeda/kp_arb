"""HL 라이브 게이트웨이 — hyperliquid-python-sdk 어댑터 (DESIGN.md §5.2).

[라이브 실측 v6.10]
- 빌더 dex = ``xyz``(trade.xyz). **심볼: 삼성=``xyz:SMSN`` / 하이닉스=``xyz:SKHX`` /
  현대차=``xyz:HYUNDAI``** (szDecimals 3, maxLev 10, 펀딩 배수 0.5).
- HIP-3는 dex별 마진 분리 — 조회·주문 모두 ``dex="xyz"`` 스코프.
- 에이전트 키로 서명(SDK가 EIP-712 처리), 계정은 메인 주소(``account_address``).
- 주문 왕복(접수 oid→취소) 실계정 검증 완료.

SDK는 동기(requests) — asyncio에서는 ``asyncio.to_thread``로 감싼다.
비밀: ``HL_AGENT_KEY``(에이전트 프라이빗 키)·``HL_ACCOUNT_ADDRESS``(메인 주소) — keyring/env.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from ..config import ConfigError, SecretProvider, default_secrets
from ..domain.enums import Instrument, OrderType, Side, Underlying, Venue
from ..domain.models import OrderIntent, Position
from .base import HLGateway
from .hl import HLError

HL_DEX = "xyz"

# 실측 확정 심볼 (perpDexs/metaAndAssetCtxs, 2026-07-02)
HL_SYMBOLS: dict[Underlying, str] = {
    Underlying.SAMSUNG: "xyz:SMSN",
    Underlying.SK_HYNIX: "xyz:SKHX",
    Underlying.HYUNDAI: "xyz:HYUNDAI",
}


class HLSdkGateway(HLGateway):
    """hyperliquid-python-sdk의 Exchange/Info를 HLGateway 계약에 맞춘 어댑터."""

    def __init__(
        self,
        exchange: Any,
        info: Any,
        *,
        account_address: str,
        symbols: Mapping[Underlying, str] | None = None,
    ) -> None:
        self._ex = exchange
        self._info = info
        self._address = account_address
        self._symbols: dict[Underlying, str] = dict(symbols or HL_SYMBOLS)
        self._by_symbol = {v: k for k, v in self._symbols.items()}
        self._order_coin: dict[str, str] = {}  # oid -> coin (취소에 필요)
        self.connected = False

    @classmethod
    def from_secrets(cls, secrets: SecretProvider | None = None) -> HLSdkGateway:
        """keyring/env의 에이전트 키·메인 주소로 SDK 클라이언트 조립."""
        from eth_account import Account as EthAccount
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        provider = secrets if secrets is not None else default_secrets()
        agent_key = provider.get("HL_AGENT_KEY")
        address = provider.get("HL_ACCOUNT_ADDRESS")
        if not agent_key or not address:
            raise ConfigError("missing HL_AGENT_KEY / HL_ACCOUNT_ADDRESS")
        wallet = EthAccount.from_key(agent_key)
        exchange = Exchange(
            wallet, constants.MAINNET_API_URL,
            account_address=address, perp_dexs=[HL_DEX],
        )
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        return cls(exchange, info, account_address=address)

    async def connect(self) -> None:
        # 연결 검증: xyz dex 계정 상태 1회 조회.
        await self.get_margin()
        self.connected = True

    # --- 주문 ---

    async def place_order(self, intent: OrderIntent) -> str:
        if intent.venue is not Venue.HYPERLIQUID:
            raise ValueError("HLSdkGateway only handles Hyperliquid orders")
        coin = self._symbol(intent.underlying)
        is_buy = intent.side is Side.BUY
        if intent.order_type is OrderType.LIMIT:
            if intent.price is None:
                raise HLError("limit order requires price")
            order_type: dict[str, Any] = {"limit": {"tif": "Gtc"}}
            price = float(intent.price)
        else:
            # HL은 순수 시장가가 없음 — IOC 지정가(마크 대비 슬리피지 허용)로 대응.
            price = await self._market_px(coin, is_buy)
            order_type = {"limit": {"tif": "Ioc"}}
        resp = await asyncio.to_thread(
            self._ex.order, coin, is_buy, float(intent.qty), price, order_type
        )
        oid = self._parse_oid(resp)
        self._order_coin[oid] = coin
        return oid

    async def cancel_order(self, order_id: str) -> None:
        coin = self._order_coin.get(order_id)
        if coin is None:
            raise HLError(f"unknown order_id {order_id} (coin required for cancel)")
        resp = await asyncio.to_thread(self._ex.cancel, coin, int(order_id))
        self._check_ok(resp)

    # --- 조회 ---

    async def get_positions(self) -> Sequence[Position]:
        state = await self._post_info(
            {"type": "clearinghouseState", "user": self._address, "dex": HL_DEX}
        )
        positions: list[Position] = []
        for asset in state.get("assetPositions", []):
            pos = asset.get("position", {})
            underlying = self._by_symbol.get(str(pos.get("coin", "")))
            if underlying is None:
                continue
            szi = float(pos.get("szi", 0))
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

    async def get_open_orders(self) -> Sequence[Any]:
        """미체결 스냅샷(frontendOpenOrders, dex 스코프) → TrackedOrder."""
        from ..order_book import OrderStatus, TrackedOrder

        rows = await self._post_info(
            {"type": "frontendOpenOrders", "user": self._address, "dex": HL_DEX}
        )
        orders: list[TrackedOrder] = []
        for row in rows if isinstance(rows, list) else []:
            underlying = self._by_symbol.get(str(row.get("coin", "")))
            if underlying is None:
                continue
            intent = OrderIntent(
                venue=Venue.HYPERLIQUID,
                underlying=underlying,
                instrument=Instrument.HL_PERP,
                side=Side.BUY if str(row.get("side")) == "B" else Side.SELL,
                qty=float(row["origSz"]),
                order_type=OrderType.LIMIT,
                price=float(row["limitPx"]),
            )
            filled = float(row["origSz"]) - float(row["sz"])  # sz = 잔여
            oid = str(row["oid"])
            self._order_coin[oid] = str(row["coin"])  # 취소 가능하도록 coin 기억
            orders.append(
                TrackedOrder(
                    order_id=oid,
                    intent=intent,
                    status=OrderStatus.PARTIAL if filled > 0 else OrderStatus.ACCEPTED,
                    filled_qty=filled,
                )
            )
        return orders

    async def get_margin(self) -> float:
        """xyz dex 계정 가치(USDC). HIP-3는 dex별 마진 분리."""
        state = await self._post_info(
            {"type": "clearinghouseState", "user": self._address, "dex": HL_DEX}
        )
        return float(state.get("marginSummary", {}).get("accountValue", 0.0))

    async def get_funding(self, underlying: Underlying) -> float:
        coin = self._symbol(underlying)
        meta, ctxs = await self._post_info({"type": "metaAndAssetCtxs", "dex": HL_DEX})
        for asset, ctx in zip(meta["universe"], ctxs, strict=True):
            if asset["name"] == coin:
                return float(ctx.get("funding", 0.0))
        raise HLError(f"{coin} not in {HL_DEX} universe")

    async def get_mark(self, underlying: Underlying) -> float:
        coin = self._symbol(underlying)
        meta, ctxs = await self._post_info({"type": "metaAndAssetCtxs", "dex": HL_DEX})
        for asset, ctx in zip(meta["universe"], ctxs, strict=True):
            if asset["name"] == coin:
                return float(ctx["markPx"])
        raise HLError(f"{coin} not in {HL_DEX} universe")

    # --- 내부 ---

    async def _post_info(self, body: dict[str, Any]) -> Any:
        return await asyncio.to_thread(self._info.post, "/info", body)

    async def _market_px(self, coin: str, is_buy: bool, *, slippage: float = 0.01) -> float:
        underlying = self._by_symbol[coin]
        mark = await self.get_mark(underlying)
        raw = mark * (1 + slippage) if is_buy else mark * (1 - slippage)
        return float(f"{raw:.5g}")  # HL 유효숫자 5자리 제한

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
