"""HLSdkGateway 계약 테스트 — SDK 스텁 주입(실 네트워크·실 키 없음)."""
from __future__ import annotations

from typing import Any

import pytest

from kp_arb.domain.enums import Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent
from kp_arb.gateways.hl import HLError
from kp_arb.gateways.hl_live import HL_SYMBOLS, HLSdkGateway

ADDR = "0x" + "a" * 40

META_CTXS = [
    {"universe": [
        {"name": "xyz:SMSN", "szDecimals": 3},
        {"name": "xyz:SKHX", "szDecimals": 3},
        {"name": "xyz:HYUNDAI", "szDecimals": 3},
    ]},
    [
        {"markPx": "184.1", "funding": "0.0001841299"},
        {"markPx": "1434.4", "funding": "0.0004326268"},
        {"markPx": "312.59", "funding": "0.0003316256"},
    ],
]


class StubExchange:
    def __init__(self) -> None:
        self.orders: list[tuple[Any, ...]] = []
        self.cancels: list[tuple[str, int]] = []

    def order(self, coin: str, is_buy: bool, sz: float, px: float,
              order_type: dict[str, Any], reduce_only: bool = False) -> dict[str, Any]:
        self.orders.append((coin, is_buy, sz, px, order_type))
        self.last_reduce_only = reduce_only
        statuses = [{"resting": {"oid": 485478010353}}]
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}

    def cancel(self, coin: str, oid: int) -> dict[str, Any]:
        self.cancels.append((coin, oid))
        return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}

    def update_leverage(self, leverage: int, name: str, is_cross: bool) -> dict[str, Any]:
        self.leverage_calls: list[tuple[int, str, bool]] = getattr(self, "leverage_calls", [])
        self.leverage_calls.append((leverage, name, is_cross))
        if leverage > 20:  # 상한 초과 거부 흉내
            return {"status": "err", "response": "Invalid leverage"}
        return {"status": "ok", "response": {"type": "default"}}


class StubInfo:
    """실측 shape 픽스처를 돌려주는 /info 스텁."""

    def __init__(self, positions: list[dict[str, Any]] | None = None,
                 account_value: str = "19.6") -> None:
        self._positions = positions or []
        self._account_value = account_value
        self.posts: list[dict[str, Any]] = []

    def post(self, path: str, body: dict[str, Any]) -> Any:
        self.posts.append(body)
        if body["type"] == "clearinghouseState":
            assert body["dex"] == "xyz"  # dex 스코프 필수
            return {"marginSummary": {"accountValue": self._account_value},
                    "assetPositions": self._positions}
        if body["type"] == "metaAndAssetCtxs":
            assert body["dex"] == "xyz"
            return META_CTXS
        raise AssertionError(f"unexpected info type {body['type']}")


def _gw(info: StubInfo | None = None) -> tuple[HLSdkGateway, StubExchange, StubInfo]:
    ex, inf = StubExchange(), info or StubInfo()
    return HLSdkGateway(ex, inf, account_address=ADDR), ex, inf


def _intent(side: Side = Side.SELL, *, order_type: OrderType = OrderType.LIMIT,
            price: float | None = 180.0) -> OrderIntent:
    return OrderIntent(venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
                       instrument=Instrument.HL_PERP, side=side, qty=0.1,
                       order_type=order_type, price=price)


async def test_limit_order_uses_dex_symbol_and_parses_oid() -> None:
    gw, ex, _ = _gw()
    oid = await gw.place_order(_intent())
    assert oid == "485478010353"
    coin, is_buy, sz, px, otype = ex.orders[0]
    assert coin == "xyz:SMSN"  # 실측 심볼(SAMSUNG 아님)
    assert is_buy is False and sz == 0.1 and px == 180.0
    assert otype == {"limit": {"tif": "Gtc"}}


async def test_market_order_becomes_ioc_with_slippage() -> None:
    gw, ex, _ = _gw()
    await gw.place_order(_intent(Side.BUY, order_type=OrderType.MARKET, price=None))
    _, is_buy, _, px, otype = ex.orders[0]
    assert otype == {"limit": {"tif": "Ioc"}}
    assert is_buy is True and px == pytest.approx(184.1 * 1.01, rel=1e-3)


async def test_cancel_requires_tracked_coin() -> None:
    gw, ex, _ = _gw()
    oid = await gw.place_order(_intent())
    await gw.cancel_order(oid)
    assert ex.cancels == [("xyz:SMSN", 485478010353)]
    with pytest.raises(HLError):
        await gw.cancel_order("999")  # 미지 주문 — coin을 모름


async def test_positions_parsed_from_xyz_dex() -> None:
    info = StubInfo(positions=[
        {"position": {"coin": "xyz:SMSN", "szi": "-0.1", "entryPx": "184.0"}},
        {"position": {"coin": "xyz:NVDA", "szi": "5", "entryPx": "1.0"}},   # 대상 외
        {"position": {"coin": "xyz:SKHX", "szi": "0", "entryPx": "0"}},     # 0 → skip
    ])
    gw, _, _ = _gw(info)
    positions = await gw.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.underlying is Underlying.SAMSUNG and p.side is Side.SELL
    assert p.qty == 0.1 and p.account is None


async def test_margin_and_funding_and_mark() -> None:
    gw, _, _ = _gw()
    assert await gw.get_margin() == 19.6
    assert await gw.get_funding(Underlying.SK_HYNIX) == pytest.approx(0.0004326268)
    assert await gw.get_mark(Underlying.HYUNDAI) == pytest.approx(312.59)


async def test_update_leverage_calls_sdk() -> None:
    gw, ex, _ = _gw()
    await gw.update_leverage(Underlying.SAMSUNG, 10, is_cross=True)
    assert ex.leverage_calls == [(10, "xyz:SMSN", True)]  # (배수, 심볼, 교차)


async def test_update_leverage_raises_on_reject() -> None:
    from kp_arb.gateways.hl import HLError

    gw, _, _ = _gw()
    with pytest.raises(HLError):
        await gw.update_leverage(Underlying.SAMSUNG, 50, is_cross=False)  # 상한 초과


async def test_position_details_parsed() -> None:
    # clearinghouseState 상세(마진·누적펀딩·청산가·레버리지) — 잔고표(B2)·레버리지(D)
    info = StubInfo(positions=[
        {"position": {"coin": "xyz:SMSN", "szi": "-0.1", "entryPx": "184.0",
                      "marginUsed": "12.3", "liquidationPx": "250.5",
                      "positionValue": "18.4", "unrealizedPnl": "-0.6",
                      "cumFunding": {"sinceOpen": "-0.05"}, "maxLeverage": "20",
                      "leverage": {"type": "cross", "value": "5"}}},
        {"position": {"coin": "xyz:SKHX", "szi": "0", "entryPx": "0"}},  # 미보유 → skip
    ])
    gw, _, _ = _gw(info)
    details = await gw.get_position_details()
    assert set(details) == {Underlying.SAMSUNG}
    d = details[Underlying.SAMSUNG]
    assert d["margin"] == 12.3 and d["liq"] == 250.5
    assert d["cum_funding"] == -0.05 and d["max_leverage"] == 20.0
    assert d["leverage"] == 5.0 and d["leverage_cross"] is True


def test_default_symbols_are_measured_values() -> None:
    assert HL_SYMBOLS[Underlying.SAMSUNG] == "xyz:SMSN"
    assert HL_SYMBOLS[Underlying.SK_HYNIX] == "xyz:SKHX"
    assert HL_SYMBOLS[Underlying.HYUNDAI] == "xyz:HYUNDAI"
