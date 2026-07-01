"""LS 현물 주문 게이트웨이 계약 테스트. 라이브 없음(mock 전송 + 녹화 픽스처)."""
from typing import Any

import pytest

from kp_arb.config import LSAccount, LSAccounts
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


def _gateway(
    transport: Any,
    *,
    futures_symbols: dict[Underlying, str] | None = None,
    accounts: LSAccounts | None = None,
) -> LSApiGateway:
    clock = _Clock()
    tm = TokenManager("k", "s", _TokenStub(), now=clock)
    rl = RateLimiter(now=clock, default_per_second=100)
    rest = LSRestClient(BASE_URL, tm, transport, rl)
    return LSApiGateway(
        {Account.KR_STOCK: rest, Account.KR_DERIV: rest},  # 테스트는 두 계좌 같은 mock
        accounts=accounts,
        futures_symbols=futures_symbols,
    )


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


async def test_future_order_uses_cfoat_and_routes_to_deriv() -> None:
    # 선물 → 선물옵션계좌(라우팅 계약) + CFOAT00100 신규주문.
    assert account_for(Instrument.KR_STOCK_FUTURE) is Account.KR_DERIV
    transport = OrderTransport()
    gw = _gateway(transport, futures_symbols={Underlying.SAMSUNG: "1AB3000"})
    oid = await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE, side=Side.SELL))
    req = transport.requests[-1]
    assert req["headers"]["tr_cd"] == LSApiGateway.FUTURE_ORDER_TR
    assert req["body"]["account"] == Account.KR_DERIV.value
    assert req["body"]["FnoIsuNo"] == "1AB3000"
    assert oid == "0000001"


async def test_future_order_without_symbol_raises() -> None:
    gw = _gateway(OrderTransport())  # futures_symbols 미설정
    with pytest.raises(RestError):
        await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE))


async def test_future_amend_uses_cfoat00200() -> None:
    transport = OrderTransport()
    gw = _gateway(transport, futures_symbols={Underlying.SAMSUNG: "1AB3000"})
    oid = await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE))
    new_id = await gw.amend_order(oid, qty=5, price=71_000.0)
    req = transport.requests[-1]
    assert req["headers"]["tr_cd"] == LSApiGateway.FUTURE_AMEND_TR
    assert req["body"]["OrgOrdNo"] == oid  # 원주문 보존
    assert req["body"]["FnoIsuNo"] == "1AB3000"
    assert req["body"]["MdfyQty"] == 5
    assert new_id != oid


async def test_future_cancel_uses_cfoat00300() -> None:
    transport = OrderTransport()
    gw = _gateway(transport, futures_symbols={Underlying.SAMSUNG: "1AB3000"})
    oid = await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE))
    await gw.cancel_order(oid)
    req = transport.requests[-1]
    assert req["headers"]["tr_cd"] == LSApiGateway.FUTURE_CANCEL_TR
    assert req["body"]["OrgOrdNo"] == oid
    assert req["body"]["CancQty"] == 10  # 원주문 수량


async def test_order_injects_account_number_and_password() -> None:
    # 실 계좌번호·비번(env→LSAccounts)이 주문 body에 주입됨. (테스트는 더미값)
    accounts = LSAccounts(
        LSAccount("STK-1", "spw", "sak", "sas"),
        LSAccount("DRV-1", "dpw", "dak", "das"),
    )
    transport = OrderTransport()
    gw = _gateway(transport, accounts=accounts)
    await gw.place_order(_intent(Instrument.KR_STOCK))
    body = transport.requests[-1]["body"]
    assert body["AcntNo"] == "STK1"  # 대시 제거됨
    assert body["Pwd"] == "spw"
    assert "account" not in body  # 플레이스홀더 대신 실 계좌필드


class _AppkeyTokenTransport:
    """appkey별로 다른 토큰을 발급 → 계좌별 토큰 라우팅 확인용."""

    async def fetch_token(self, appkey: str, appsecret: str) -> TokenResponse:
        return TokenResponse(access_token=f"tok-{appkey}", expires_in=3600.0)


async def test_from_accounts_uses_per_account_token() -> None:
    accounts = LSAccounts(
        LSAccount("STK-1", "spw", "stock-ak", "stock-as"),
        LSAccount("DRV-1", "dpw", "deriv-ak", "deriv-as"),
    )
    rest_tx = OrderTransport()
    gw = LSApiGateway.from_accounts(
        accounts,
        token_transport=_AppkeyTokenTransport(),
        rest_transport=rest_tx,
        futures_symbols={Underlying.SAMSUNG: "1AB3000"},
    )
    await gw.place_order(_intent(Instrument.KR_STOCK))          # 주식계좌 토큰
    await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE))   # 선물계좌 토큰

    # 계좌별로 서로 다른 appkey→토큰으로 요청됨.
    assert rest_tx.requests[0]["headers"]["authorization"] == "Bearer tok-stock-ak"
    assert rest_tx.requests[1]["headers"]["authorization"] == "Bearer tok-deriv-ak"
    assert rest_tx.requests[0]["body"]["AcntNo"] == "STK1"  # 계좌 자격 주입(대시 제거)


async def test_routes_to_per_account_client() -> None:
    clock = _Clock()

    def _rest(tx: OrderTransport) -> LSRestClient:
        tm = TokenManager("k", "s", _TokenStub(), now=clock)
        rl = RateLimiter(now=clock, default_per_second=100)
        return LSRestClient(BASE_URL, tm, tx, rl)

    stock_tx, deriv_tx = OrderTransport(), OrderTransport()
    gw = LSApiGateway(
        {Account.KR_STOCK: _rest(stock_tx), Account.KR_DERIV: _rest(deriv_tx)},
        futures_symbols={Underlying.SAMSUNG: "1AB3000"},
    )
    await gw.place_order(_intent(Instrument.KR_STOCK))          # → 주식 클라이언트
    await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE))   # → 선물 클라이언트

    assert len(stock_tx.requests) == 1 and len(deriv_tx.requests) == 1
    assert stock_tx.requests[0]["headers"]["tr_cd"] == LSApiGateway.SPOT_ORDER_TR
    assert deriv_tx.requests[0]["headers"]["tr_cd"] == LSApiGateway.FUTURE_ORDER_TR


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
