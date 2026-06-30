"""HL 주문/취소 계약 테스트. 라이브 없음(mock transport + 녹화 픽스처 + mock 서명자)."""
from typing import Any

import pytest

from kp_arb.domain.enums import Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent
from kp_arb.gateways.hl import HLApiGateway, HLError
from kp_arb.gateways.hl_auth import Signature

SYMBOLS = {Underlying.SAMSUNG: "SAMSUNG-PERP", Underlying.SK_HYNIX: "HYNIX-PERP"}


class MockSigner:
    def __init__(self, address: str = "0xAGENT") -> None:
        self._address = address

    @property
    def address(self) -> str:
        return self._address

    def sign_l1_action(self, action: dict[str, Any], nonce: int) -> Signature:
        return Signature(r="0xrr", s="0xss", v=27)


class OrderTransport:
    """exchange 액션을 기록하고 녹화 응답을 돌려주는 mock."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, body))
        action_type = body["action"]["type"]
        if action_type == "order":
            return {
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 555}}]}},
            }
        return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}


class ErrTransport:
    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"status": "err", "response": "insufficient margin"}


def _gateway(transport: Any, *, symbols: dict[Underlying, str] | None = None) -> HLApiGateway:
    return HLApiGateway(
        MockSigner(),
        transport,
        symbols=symbols if symbols is not None else SYMBOLS,
        nonce_fn=lambda: 111,
    )


def _intent(underlying: Underlying = Underlying.SAMSUNG) -> OrderIntent:
    return OrderIntent(
        venue=Venue.HYPERLIQUID,
        underlying=underlying,
        instrument=Instrument.HL_PERP,
        side=Side.BUY,
        qty=1,
        order_type=OrderType.LIMIT,
        price=70_000.0,
    )


async def test_place_order_signs_and_parses_oid() -> None:
    transport = OrderTransport()
    gw = _gateway(transport)
    oid = await gw.place_order(_intent())
    assert oid == "555"
    path, body = transport.posts[-1]
    assert path == HLApiGateway.EXCHANGE_PATH
    assert body["action"]["type"] == "order"
    assert body["action"]["orders"][0]["coin"] == "SAMSUNG-PERP"
    assert body["nonce"] == 111
    assert body["signature"] == {"r": "0xrr", "s": "0xss", "v": 27}
    assert "account" not in body["action"]  # HL 주문은 KR 계좌 없음


async def test_place_order_rejects_non_hl() -> None:
    gw = _gateway(OrderTransport())
    oi = OrderIntent(
        venue=Venue.LS,
        underlying=Underlying.SAMSUNG,
        instrument=Instrument.KR_STOCK,
        side=Side.BUY,
        qty=1,
        order_type=OrderType.MARKET,
    )
    with pytest.raises(ValueError):
        await gw.place_order(oi)


async def test_place_order_unknown_symbol_raises() -> None:
    gw = _gateway(OrderTransport(), symbols={Underlying.SAMSUNG: "SAMSUNG-PERP"})
    with pytest.raises(HLError):
        await gw.place_order(_intent(Underlying.HYUNDAI))


async def test_order_error_status_raises() -> None:
    gw = _gateway(ErrTransport())
    with pytest.raises(HLError):
        await gw.place_order(_intent())


async def test_cancel_order_posts_cancel() -> None:
    transport = OrderTransport()
    gw = _gateway(transport)
    await gw.cancel_order("555")
    path, body = transport.posts[-1]
    assert body["action"]["type"] == "cancel"
    assert body["action"]["cancels"][0]["oid"] == 555


async def test_order_without_transport_raises() -> None:
    gw = HLApiGateway(MockSigner(), symbols=SYMBOLS, nonce_fn=lambda: 1)
    with pytest.raises(HLError):
        await gw.place_order(_intent())
