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
    # 모의 성공코드 "00136"(운영 "00000"), 실필드 MnyOrdAbleAmt(현금주문가능).
    "CSPAQ22200": {"rsp_cd": "00136", "CSPAQ22200OutBlock2": {"MnyOrdAbleAmt": 5_000_000}},
    "CSPAQ12300": {
        "rsp_cd": "00136",
        # 실측 행 필드: 잔고는 BnsBaseBalQty(당일 매수 T+2 미결제 포함), 평단은 AvrUprc(문자열).
        "CSPAQ12300OutBlock3": [
            {"IsuNo": "005930", "BalQty": 0, "BnsBaseBalQty": 100, "AvrUprc": "70000.00"},
            {"IsuNo": "000660", "BalQty": 50, "BnsBaseBalQty": 50, "AvrUprc": "180000.00"},
        ],
    },
    "CFOBQ10500": {"rsp_cd": "00136", "CFOBQ10500OutBlock2": {"MnyOrdAbleAmt": 3_000_000}},
    # 실측 v6.5: 미체결 행 — IsuNo "A"접두, OrdPrc 문자열, MrcAbleQty=정정취소가능수량.
    "CSPAQ13700": {
        "rsp_cd": "00136",
        "CSPAQ13700OutBlock3": [
            {"OrdNo": 7267, "IsuNo": "A005930", "BnsTpCode": "2", "OrdQty": 1,
             "OrdPrc": "265000.00", "ExecQty": 0, "ExecPrc": "0.00",
             "MrcAbleQty": 1, "OrdprcPtnCode": "00"},
            {"OrdNo": 7000, "IsuNo": "A005930", "BnsTpCode": "1", "OrdQty": 2,
             "OrdPrc": "0.00", "ExecQty": 2, "ExecPrc": "292000.00",
             "MrcAbleQty": 0, "OrdprcPtnCode": "03"},  # 전량 체결 → 제외 대상
        ],
    },
    "CFOAQ50600": {
        "rsp_cd": "00136",
        "CFOAQ50600OutBlock3": [
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
    """headers의 tr_cd로 녹화 픽스처를 골라 돌려준다. 사용된 tr_cd·요청 body를 기록."""

    def __init__(self) -> None:
        self.seen_trs: list[str] = []
        self.bodies: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        tr = headers["tr_cd"]
        self.seen_trs.append(tr)
        self.bodies.append(body or {})
        return RestResponse(status_code=200, body=FIXTURES[tr])


def _gateway(transport: Any, *, etf_symbols: dict[Underlying, str] | None = None) -> LSApiGateway:
    clock = _Clock()
    tm = TokenManager("k", "s", _TokenStub(), now=clock)
    rl = RateLimiter(now=clock, default_per_second=100)
    rest = LSRestClient(BASE_URL, tm, transport, rl)
    return LSApiGateway({Account.KR_STOCK: rest, Account.KR_DERIV: rest},
                        etf_symbols=etf_symbols)


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
    assert transport.seen_trs == ["CFOBQ10500"]


async def test_balances_route_to_different_trs() -> None:
    transport = AccountTransport()
    gw = _gateway(transport)
    await gw.get_balance(Account.KR_STOCK)
    await gw.get_balance(Account.KR_DERIV)
    assert transport.seen_trs == ["CSPAQ22200", "CFOBQ10500"]


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
    assert transport.seen_trs == ["CSPAQ12300", "CFOAQ50600"]


async def test_etf_position_recognized() -> None:
    # 잔고 행의 종목코드가 ETF면 KR_ETF + 기초자산으로 해석.
    transport = AccountTransport()
    FIXTURES["CSPAQ12300"]["CSPAQ12300OutBlock3"].append(
        {"IsuNo": "0193W0", "BalQty": 10, "BnsBaseBalQty": 10, "AvrUprc": "17600.00"}
    )
    try:
        gw = _gateway(transport, etf_symbols={Underlying.SAMSUNG: "0193W0"})
        positions = await gw.get_positions(Account.KR_STOCK)
        etf = [p for p in positions if p.instrument is Instrument.KR_ETF]
        assert len(etf) == 1
        assert etf[0].underlying is Underlying.SAMSUNG
        assert etf[0].qty == 10 and etf[0].avg_price == 17_600
    finally:
        FIXTURES["CSPAQ12300"]["CSPAQ12300OutBlock3"].pop()


# --- 미체결 주문 스냅샷 ---


async def test_open_orders_parsed_and_filtered() -> None:
    from kp_arb.order_book import OrderStatus

    transport = AccountTransport()
    gw = _gateway(transport)
    orders = await gw.get_open_orders(Account.KR_STOCK)

    assert transport.seen_trs == ["CSPAQ13700"]
    # InBlock1 래핑 + ExecYn=2(미체결) 요청 확인
    blk = transport.bodies[-1]["CSPAQ13700InBlock1"]
    assert blk["ExecYn"] == "2"
    # MrcAbleQty>0 만 미체결로 남김(전량 체결 행 제외)
    assert len(orders) == 1
    o = orders[0]
    assert o.order_id == "7267"
    assert o.status is OrderStatus.ACCEPTED
    assert o.intent.underlying is Underlying.SAMSUNG  # "A005930" → 005930
    assert o.intent.side is Side.BUY and o.intent.qty == 1
    assert o.intent.price == 265_000.0


async def test_open_orders_deriv_not_implemented_returns_empty() -> None:
    gw = _gateway(AccountTransport())
    assert await gw.get_open_orders(Account.KR_DERIV) == []  # 선물 미체결 TR 미확인


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


async def test_deriv_positions_paper_unsupported_returns_empty() -> None:
    # 실측 v6.1: 모의는 CFOAQ50600 미제공(rsp_cd 01900) — 오류가 아니라 빈 결과.
    class Unsupported(AccountTransport):
        async def request(
            self,
            method: str,
            url: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> RestResponse:
            return RestResponse(
                status_code=200,
                body={"rsp_cd": "01900",
                      "rsp_msg": "모의투자에서는 해당업무가 제공되지 않습니다."},
            )

    gw = _gateway(Unsupported())
    assert await gw.get_positions(Account.KR_DERIV) == []


async def test_positions_skip_untracked_symbols() -> None:
    # 실계좌엔 취급 외 보유 종목이 있을 수 있다(실측: 252670) — 건너뛰고 계속.
    class MixedHoldings(AccountTransport):
        async def request(
            self,
            method: str,
            url: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> RestResponse:
            return RestResponse(status_code=200, body={
                "rsp_cd": "00136",
                "CSPAQ12300OutBlock3": [
                    {"IsuNo": "252670", "BalQty": 10, "BnsBaseBalQty": 10,
                     "AvrUprc": "3500.00"},   # 취급 외 → 무시
                    {"IsuNo": "005930", "BalQty": 0, "BnsBaseBalQty": 100,
                     "AvrUprc": "70000.00"},  # 삼성전자 → 추적
                ],
            })

    gw = _gateway(MixedHoldings())
    positions = await gw.get_positions(Account.KR_STOCK)
    assert len(positions) == 1
    assert positions[0].underlying is Underlying.SAMSUNG
