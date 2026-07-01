"""LS 현물 주문 게이트웨이 계약 테스트. 라이브 없음(mock 전송 + 녹화 픽스처)."""
from typing import Any

import pytest

from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent
from kp_arb.gateways.ls import LSApiGateway
from kp_arb.gateways.ls_auth import TokenManager, TokenResponse
from kp_arb.gateways.ls_rest import LSRestClient, RateLimiter, RestError, RestResponse
from kp_arb.routing import account_for

BASE_URL = "https://openapi.ls-sec.co.kr:8080"


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class _TokenStub:
    async def fetch_token(self, appkey: str, appsecret: str) -> TokenResponse:
        return TokenResponse(access_token="tok", expires_in=3600.0)


class OrderTransport:
    """녹화 픽스처: 모든 주문 TR에 대해 성공 + OutBlock2.OrdNo를 돌려준다."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.counter = 0

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        self.counter += 1
        self.requests.append({"url": url, "headers": headers, "body": body})
        tr = headers["tr_cd"]
        return RestResponse(
            status_code=200,
            body={
                "rsp_cd": "00000",
                "rsp_msg": "정상처리",
                f"{tr}OutBlock2": {"OrdNo": f"{self.counter:07d}"},
            },
        )


class RejectTransport:
    """거부 응답(rsp_cd != 00000)을 돌려주는 픽스처."""

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        return RestResponse(status_code=200, body={"rsp_cd": "40510", "rsp_msg": "잔고부족"})


def _gateway(transport: Any) -> LSApiGateway:
    clock = _Clock()
    tm = TokenManager("k", "s", _TokenStub(), now=clock)
    rl = RateLimiter(now=clock, default_per_second=100)
    return LSApiGateway(LSRestClient(BASE_URL, tm, transport, rl))


def _intent(instrument: Instrument, *, side: Side = Side.BUY) -> OrderIntent:
    return OrderIntent(
        venue=Venue.LS,
        underlying=Underlying.SAMSUNG,
        instrument=instrument,
        side=side,
        qty=10,
        order_type=OrderType.LIMIT,
        price=70_000.0,
    )


# --- 계좌 라우팅 (불변식) ---


async def test_stock_routes_to_stock_account() -> None:
    transport = OrderTransport()
    gw = _gateway(transport)
    await gw.place_order(_intent(Instrument.KR_STOCK))
    req = transport.requests[-1]
    assert req["headers"]["tr_cd"] == LSApiGateway.SPOT_ORDER_TR
    assert req["body"]["account"] == Account.KR_STOCK.value


async def test_etf_routes_to_stock_account() -> None:
    transport = OrderTransport()
    gw = _gateway(transport)
    await gw.place_order(_intent(Instrument.KR_ETF))
    assert transport.requests[-1]["body"]["account"] == Account.KR_STOCK.value


@pytest.mark.parametrize(
    "instrument",
    [Instrument.KR_STOCK_FUTURE],
)
async def test_futures_route_to_deriv_but_tr_open(instrument: Instrument) -> None:
    # 라우팅 계약: 선물 → 선물옵션계좌. 단 주문 TR은 [OPEN]이라 가드.
    assert account_for(instrument) is Account.KR_DERIV
    gw = _gateway(OrderTransport())
    with pytest.raises(NotImplementedError):
        await gw.place_order(_intent(instrument, side=Side.SELL))


async def test_rejects_hl_order() -> None:
    gw = _gateway(OrderTransport())
    oi = OrderIntent(
        venue=Venue.HYPERLIQUID,
        underlying=Underlying.SAMSUNG,
        instrument=Instrument.HL_PERP,
        side=Side.BUY,
        qty=1,
        order_type=OrderType.MARKET,
    )
    with pytest.raises(ValueError):
        await gw.place_order(oi)


# --- 응답 파싱 ---


async def test_parses_order_id() -> None:
    gw = _gateway(OrderTransport())
    oid = await gw.place_order(_intent(Instrument.KR_STOCK))
    assert oid == "0000001"


async def test_rejected_response_raises() -> None:
    gw = _gateway(RejectTransport())
    with pytest.raises(RestError):
        await gw.place_order(_intent(Instrument.KR_STOCK))


# --- 정정/취소: 원주문 컨텍스트 보존 ---


async def test_cancel_preserves_original_context() -> None:
    transport = OrderTransport()
    gw = _gateway(transport)
    oid = await gw.place_order(_intent(Instrument.KR_STOCK))

    await gw.cancel_order(oid)
    cancel_req = transport.requests[-1]
    assert cancel_req["headers"]["tr_cd"] == LSApiGateway.SPOT_CANCEL_TR
    assert cancel_req["body"]["OrgOrdNo"] == oid  # 원주문 참조
    assert cancel_req["body"]["account"] == Account.KR_STOCK.value
    assert cancel_req["body"]["IsuNo"] == Underlying.SAMSUNG.krx_code


async def test_amend_links_to_original() -> None:
    transport = OrderTransport()
    gw = _gateway(transport)
    oid = await gw.place_order(_intent(Instrument.KR_STOCK))

    new_id = await gw.amend_order(oid, qty=5, price=71_000.0)
    assert new_id != oid
    amend_req = transport.requests[-1]
    assert amend_req["headers"]["tr_cd"] == LSApiGateway.SPOT_AMEND_TR
    assert amend_req["body"]["OrgOrdNo"] == oid
    assert amend_req["body"]["OrdQty"] == 5
    assert amend_req["body"]["OrdPrc"] == 71_000.0


async def test_cancel_unknown_order_raises() -> None:
    gw = _gateway(OrderTransport())
    with pytest.raises(ValueError):
        await gw.cancel_order("nope")
