"""LS 계좌별 잔고·포지션 조회 계약 테스트. 라이브 없음(녹화 픽스처)."""
from typing import Any

import pytest

from kp_arb.domain.enums import Account, Instrument, Side, Underlying
from kp_arb.gateways.ls import LSApiGateway
from kp_arb.gateways.ls_auth import TokenManager, TokenResponse
from kp_arb.gateways.ls_rest import LSRestClient, RateLimiter, RestError, RestResponse

BASE_URL = "https://openapi.ls-sec.co.kr:8080"

# 녹화 픽스처: TR별 응답.
FIXTURES: dict[str, dict[str, Any]] = {
    "CSPAQ22200": {"rsp_cd": "00000", "CSPAQ22200OutBlock2": {"DpsAmt": 5_000_000}},
    "CSPAQ12300": {
        "rsp_cd": "00000",
        "CSPAQ12300OutBlock3": [
            {"IsuNo": "005930", "BalQty": 100, "AvrPrc": 70_000},
            {"IsuNo": "000660", "BalQty": 50, "AvrPrc": 180_000},
        ],
    },
    "FOCCQ33600": {
        "rsp_cd": "00000",
        "FOCCQ33600OutBlock2": {"OrdAbleAmt": 3_000_000},
        "FOCCQ33600OutBlock3": [
            {"IsuNo": "005930", "BalQty": 2, "AvrPrc": 71_000, "BnsTpCode": "1"},
        ],
    },
}


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class _TokenStub:
    async def fetch_token(self, appkey: str, appsecret: str) -> TokenResponse:
        return TokenResponse(access_token="tok", expires_in=3600.0)


class AccountTransport:
    """headers의 tr_cd로 녹화 픽스처를 골라 돌려준다. 사용된 tr_cd를 기록."""

    def __init__(self) -> None:
        self.seen_trs: list[str] = []

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        tr = headers["tr_cd"]
        self.seen_trs.append(tr)
        return RestResponse(status_code=200, body=FIXTURES[tr])


def _gateway(transport: Any) -> LSApiGateway:
    clock = _Clock()
    tm = TokenManager("k", "s", _TokenStub(), now=clock)
    rl = RateLimiter(now=clock, default_per_second=100)
    return LSApiGateway(LSRestClient(BASE_URL, tm, transport, rl))


# --- 잔고(예수금/증거금) ---


async def test_stock_balance_uses_deposit_tr() -> None:
    transport = AccountTransport()
    gw = _gateway(transport)
    bal = await gw.get_balance(Account.KR_STOCK)
    assert bal == 5_000_000
    assert transport.seen_trs == ["CSPAQ22200"]


async def test_deriv_balance_uses_margin_tr() -> None:
    transport = AccountTransport()
    gw = _gateway(transport)
    bal = await gw.get_balance(Account.KR_DERIV)
    assert bal == 3_000_000
    assert transport.seen_trs == ["FOCCQ33600"]


async def test_balances_route_to_different_trs() -> None:
    transport = AccountTransport()
    gw = _gateway(transport)
    await gw.get_balance(Account.KR_STOCK)
    await gw.get_balance(Account.KR_DERIV)
    assert transport.seen_trs == ["CSPAQ22200", "FOCCQ33600"]


# --- 포지션 ---


async def test_stock_positions_parsed_to_stock_account() -> None:
    gw = _gateway(AccountTransport())
    positions = await gw.get_positions(Account.KR_STOCK)
    assert len(positions) == 2
    assert {p.underlying for p in positions} == {Underlying.SAMSUNG, Underlying.SK_HYNIX}
    assert all(p.account is Account.KR_STOCK for p in positions)
    assert all(p.instrument is Instrument.KR_STOCK for p in positions)
    assert all(p.side is Side.BUY for p in positions)
    samsung = next(p for p in positions if p.underlying is Underlying.SAMSUNG)
    assert samsung.qty == 100 and samsung.avg_price == 70_000


async def test_deriv_positions_parsed_to_deriv_account() -> None:
    gw = _gateway(AccountTransport())
    positions = await gw.get_positions(Account.KR_DERIV)
    assert len(positions) == 1
    pos = positions[0]
    assert pos.account is Account.KR_DERIV
    assert pos.instrument is Instrument.KR_STOCK_FUTURE
    assert pos.side is Side.SELL  # BnsTpCode "1" = 매도
    assert pos.underlying is Underlying.SAMSUNG
    assert pos.qty == 2 and pos.avg_price == 71_000


async def test_positions_route_to_different_trs() -> None:
    transport = AccountTransport()
    gw = _gateway(transport)
    await gw.get_positions(Account.KR_STOCK)
    await gw.get_positions(Account.KR_DERIV)
    assert transport.seen_trs == ["CSPAQ12300", "FOCCQ33600"]


# --- 응답 오류 ---


async def test_rejected_balance_raises() -> None:
    class RejectTransport:
        async def request(
            self,
            method: str,
            url: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> RestResponse:
            return RestResponse(status_code=200, body={"rsp_cd": "40510", "rsp_msg": "오류"})

    gw = _gateway(RejectTransport())
    with pytest.raises(RestError):
        await gw.get_balance(Account.KR_STOCK)
