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
        if getattr(self, "fill_on_place", False):  # 발주 즉시체결(크로싱) 흉내
            statuses: list[dict[str, Any]] = [
                {"filled": {"totalSz": str(sz), "avgPx": "168.23", "oid": 485478010353}}]
        else:
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

    def modify_order(self, oid: int, coin: str, is_buy: bool, sz: float, px: float,
                     order_type: dict[str, Any], reduce_only: bool = False) -> dict[str, Any]:
        self.modifies: list[tuple[Any, ...]] = getattr(self, "modifies", [])
        self.modifies.append((oid, coin, is_buy, sz, px, reduce_only, order_type))
        if getattr(self, "cross_reject", False):  # HL always_place=false 크로싱 거부 흉내
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
                {"error": "Post only order would have immediately matched"}]}}}
        statuses = [{"resting": {"oid": oid + 1}}]
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}


class StubInfo:
    """실측 shape 픽스처를 돌려주는 /info 스텁."""

    def __init__(self, positions: list[dict[str, Any]] | None = None,
                 account_value: str = "19.6",
                 active_data: dict[str, Any] | None = None,
                 open_orders: list[dict[str, Any]] | None = None) -> None:
        self._positions = positions or []
        self._account_value = account_value
        self._active = active_data or {}  # coin -> activeAssetData 응답
        self._open_orders = open_orders or []  # frontendOpenOrders 행
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
        if body["type"] == "activeAssetData":
            return self._active.get(body["coin"], {})  # 미설정 코인 → 빈 응답
        if body["type"] == "frontendOpenOrders":
            assert body["dex"] == "xyz"
            return self._open_orders
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


async def test_place_immediate_fill_exposed_via_pop() -> None:
    # 발주 즉시체결(응답 filled)이면 (체결수량, 평균가)를 pop_place_fill로 1회 노출한다
    # — place()가 이걸 OrderBook에 반영해 미체결로 안 남게 한다.
    gw, ex, _ = _gw()
    ex.fill_on_place = True
    await gw.place_order(_intent(Side.SELL, price=165.0))  # qty 0.1
    assert gw.pop_place_fill() == (0.1, 168.23)  # (수량, 평균가)
    assert gw.pop_place_fill() is None            # 1회 소비


async def test_place_resting_has_no_place_fill() -> None:
    gw, _, _ = _gw()
    await gw.place_order(_intent(Side.SELL, price=180.0))  # resting(미체결)
    assert gw.pop_place_fill() is None


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


async def test_leverage_settings_from_active_asset_data() -> None:
    # 포지션 없어도 activeAssetData로 코인별 설정 레버리지를 읽는다(§D 캡션 보정).
    info = StubInfo(active_data={
        "xyz:SMSN": {"leverage": {"type": "cross", "value": 10}},
        "xyz:SKHX": {"leverage": {"type": "isolated", "value": 20, "rawUsd": "0.0"}},
        # xyz:HYUNDAI 응답 없음(빈 dict) → 결과에서 빠짐
    })
    gw, _, _ = _gw(info)
    out = await gw.get_leverage_settings()
    assert out[Underlying.SAMSUNG] == {"leverage": 10.0, "leverage_cross": True}
    assert out[Underlying.SK_HYNIX] == {"leverage": 20.0, "leverage_cross": False}
    assert Underlying.HYUNDAI not in out


def test_lev_from_active_asset_parsing() -> None:
    from kp_arb.gateways.hl_live import _lev_from_active_asset
    assert _lev_from_active_asset(
        {"leverage": {"type": "cross", "value": 10}}
    ) == {"leverage": 10.0, "leverage_cross": True}
    assert _lev_from_active_asset(
        {"leverage": {"type": "isolated", "value": 20, "rawUsd": "0.0"}}
    ) == {"leverage": 20.0, "leverage_cross": False}
    assert _lev_from_active_asset({}) is None       # 레버리지 없음
    assert _lev_from_active_asset(None) is None      # 이상 응답


async def test_snapshot_orders_allow_amend() -> None:
    # get_open_orders(스냅샷)로 로드된 주문도 정정 가능해야 한다 — _order_ctx를 채워야
    # "context required for modify" 거부가 안 난다(코어 재시작 후 정정, 특히 매도).
    info = StubInfo(open_orders=[
        {"coin": "xyz:SMSN", "side": "A", "origSz": "0.14", "sz": "0.14",
         "limitPx": "185.0", "oid": 777}])
    gw, ex, _ = _gw(info)
    await gw.get_open_orders()  # place_order 없이 스냅샷만 로드
    new_oid = await gw.amend_order("777", qty=0.14, price=184.0)
    assert new_oid == "778"  # 예외 없이 정정 — 새 oid
    assert ex.modifies[0][:3] == (777, "xyz:SMSN", False)  # 매도(is_buy=False) 보존


async def test_amend_uses_explicit_reduce_and_post() -> None:
    # 정정 시 reduce_only·post_only는 **명시 인자**로 전달(원주문 상속 안 함). 안 넘기면
    # 벗겨져 소액 reduce 주문이 'Attempted to modify to invalid new order'로 거부(실측).
    gw, ex, _ = _gw()
    oid = await gw.place_order(_intent(Side.SELL, price=163.0))  # 원주문 옵션 무관
    await gw.amend_order(oid, qty=0.054, price=163.1, reduce_only=True, post_only=True)
    *_, reduce_only, order_type = ex.modifies[-1]
    assert reduce_only is True                        # 명시 reduce 전달
    assert order_type == {"limit": {"tif": "Alo"}}    # post_only → Alo


async def test_crossing_amend_rejected_clearly() -> None:
    # HL modify가 크로싱 Gtc를 ALO로 강제해 거부하면(always_place=false), 명확히 안내하고
    # 거부한다(폴백 없음 — 사용자 확정 "정정 안 되면 빼도 됨"). 신규 주문은 안 낸다.
    gw, ex, _ = _gw()
    ex.cross_reject = True  # modify가 'immediately matched'로 거부
    oid = await gw.place_order(_intent(Side.BUY, price=166.0))
    with pytest.raises(HLError, match="취소 후 신규"):
        await gw.amend_order(oid, qty=0.1, price=171.0)
    assert len(ex.orders) == 1 and not ex.cancels  # 폴백 없음(신규·취소 안 함)


async def test_amend_default_is_gtc_no_reduce() -> None:
    gw, ex, _ = _gw()
    oid = await gw.place_order(_intent(Side.SELL, price=163.0))
    await gw.amend_order(oid, qty=0.054, price=163.1)  # 옵션 미지정
    *_, reduce_only, order_type = ex.modifies[-1]
    assert reduce_only is False and order_type == {"limit": {"tif": "Gtc"}}


def test_default_symbols_are_measured_values() -> None:
    assert HL_SYMBOLS[Underlying.SAMSUNG] == "xyz:SMSN"
    assert HL_SYMBOLS[Underlying.SK_HYNIX] == "xyz:SKHX"
    assert HL_SYMBOLS[Underlying.HYUNDAI] == "xyz:HYUNDAI"
