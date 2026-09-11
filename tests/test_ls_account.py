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
    # 스냅샷으로 알게 된 주문의 취소(재시동 뒤 취소 문맥 복원 검증용)
    "CFOAT00300": {"rsp_cd": "00000"},
    "CSPAT00801": {"rsp_cd": "00000"},
    # 실측 v6.5: 미체결 행 — IsuNo "A"접두, OrdPrc 문자열, MrcAbleQty=정정취소가능수량.
    "CSPAQ13700": {
        "rsp_cd": "00136",
        "CSPAQ13700OutBlock3": [
            {"OrdNo": 7267, "IsuNo": "A005930", "BnsTpCode": "2", "OrdQty": 1,
             "OrdPrc": "265000.00", "ExecQty": 0, "ExecPrc": "0.00",
             "MrcAbleQty": 1, "OrdprcPtnCode": "00", "OrdTime": "090922123"},
            {"OrdNo": 7000, "IsuNo": "A005930", "BnsTpCode": "1", "OrdQty": 2,
             "OrdPrc": "0.00", "ExecQty": 2, "ExecPrc": "292000.00",
             "MrcAbleQty": 0, "OrdprcPtnCode": "03"},  # 전량 체결 → 제외 대상
        ],
    },
    # t0434 공식 문서 예시 행(2026-09-09, 실측 대기): ordrem(잔량)>0만 미체결. 첫 행은 전량 체결.
    "t0434": {
        "rsp_cd": "00000",
        "t0434OutBlock1": [
            {"orgordno": 0, "hogatype": "L", "ordrem": 0, "ordgb": "지정가", "cheqty": 5,
             "ordno": 69104, "price": "34225.00", "qty": 5, "expcode": "A1167000",
             "medosu": "매수", "cheprice": "34225.00", "status": "완료"},
            {"orgordno": 0, "hogatype": "L", "ordrem": 4, "ordgb": "지정가", "cheqty": 1,
             "ordno": 69105, "price": "34225.00", "qty": 5, "expcode": "A1167000",
             "medosu": "매도", "cheprice": "34225.00", "status": "접수",
             "ordtime": "101530"},
            {"orgordno": 0, "hogatype": "L", "ordrem": 1, "ordgb": "지정가", "cheqty": 0,
             "ordno": 69106, "price": "1000.00", "qty": 1, "expcode": "ZZZ",
             "medosu": "매수", "cheprice": "0.00", "status": "접수"},  # 취급 외 종목
        ],
        "t0434OutBlock": {"cts_ordno": ""},
    },
    # t0441 실측 행(운영): expcode(선물코드)/medocd(1매도 2매수)/jqty/pamt.
    "t0441": {
        "rsp_cd": "00000",
        "t0441OutBlock1": [
            {"expcode": "A1167000", "medocd": "1", "jqty": 2, "pamt": "71000.00",
             "price": "71500.00", "cqty": 2},
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


def _gateway(
    transport: Any,
    *,
    etf_symbols: dict[Underlying, str] | None = None,
    futures_symbols: dict[Underlying, str] | None = None,
    next_futures_symbols: dict[Underlying, str] | None = None,
) -> LSApiGateway:
    clock = _Clock()
    tm = TokenManager("k", "s", _TokenStub(), now=clock)
    rl = RateLimiter(now=clock, default_per_second=100)
    rest = LSRestClient(BASE_URL, tm, transport, rl)
    return LSApiGateway({Account.KR_STOCK: rest, Account.KR_DERIV: rest},
                        etf_symbols=etf_symbols,
                        futures_symbols=futures_symbols
                        or {Underlying.SAMSUNG: "A1167000"},
                        next_futures_symbols=next_futures_symbols)


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
    assert pos.side is Side.SELL  # medocd "1" = 매도 (t0441 실측)
    assert pos.underlying is Underlying.SAMSUNG  # expcode A1167000 → 삼성 선물
    assert pos.qty == 2 and pos.avg_price == 71_000


async def test_deriv_position_code_maps_to_next_month() -> None:
    # t0441 expcode가 차근 코드와 일치하면 KR_STOCK_FUTURE_NEXT 포지션(§5.11). 근월물과 코드 분리.
    gw = _gateway(AccountTransport(), futures_symbols={Underlying.SAMSUNG: "A1166000"},
                  next_futures_symbols={Underlying.SAMSUNG: "A1167000"})
    positions = await gw.get_positions(Account.KR_DERIV)
    assert len(positions) == 1
    assert positions[0].instrument is Instrument.KR_STOCK_FUTURE_NEXT
    assert positions[0].underlying is Underlying.SAMSUNG


async def test_positions_route_to_different_trs() -> None:
    transport = AccountTransport()
    gw = _gateway(transport)
    await gw.get_positions(Account.KR_STOCK)
    await gw.get_positions(Account.KR_DERIV)
    assert transport.seen_trs == ["CSPAQ12300", "t0441"]


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
    assert o.placed_at == "09:09:22"  # 접수시각(OrdTime HHMMSSmmm) — 주문 리스트 칸


async def test_deriv_open_orders_use_t0434_and_gate_reconcile() -> None:
    # 2026-09-09: 선물 미체결은 t0434(취급 코드마다 전체 조회 → ordrem>0만). 실제로 성공한 뒤에만
    # open_orders_supported(KR_DERIV)가 참 — 그 전엔 재동기 유령 정리에서 제외(#20851 재발 방지).
    from kp_arb.order_book import OrderStatus

    transport = AccountTransport()
    gw = _gateway(transport, futures_symbols={Underlying.SAMSUNG: "A1167000"},
                  next_futures_symbols={Underlying.SAMSUNG: "A1168000"})
    assert not gw.open_orders_supported(Account.KR_DERIV)  # 조회 성공 전
    orders = await gw.get_open_orders(Account.KR_DERIV)
    assert transport.seen_trs == ["t0434", "t0434"]  # 근·차근 코드마다 1회
    blk = transport.bodies[0]["t0434InBlock"]
    assert blk["expcode"] == "A1167000" and blk["chegb"] == "2"  # 공식 문서: 2=미체결
    assert blk["cts_ordno"] == " "  # 처음 조회는 Space
    assert transport.bodies[1]["t0434InBlock"]["expcode"] == "A1168000"
    # 잔량 0(전량 체결)·취급 외 종목 제외, 같은 주문은 한 번만
    assert [o.order_id for o in orders] == ["69105"]
    o = orders[0]
    assert o.intent.account is Account.KR_DERIV and o.intent.underlying is Underlying.SAMSUNG
    assert o.intent.instrument is Instrument.KR_STOCK_FUTURE and o.intent.side is Side.SELL
    assert o.intent.qty == 5 and o.intent.price == 34_225.0
    assert o.status is OrderStatus.PARTIAL and o.filled_qty == 1
    assert o.placed_at == "10:15:30"  # 접수시각(ordtime)
    assert gw.open_orders_supported(Account.KR_DERIV)  # 성공 뒤 유령 정리 대상


def test_placed_at_parsers() -> None:
    from kp_arb.gateways.base import placed_at_from_hhmmss, placed_at_from_ms

    assert placed_at_from_hhmmss("090922123") == "09:09:22"
    assert placed_at_from_hhmmss("101530") == "10:15:30"
    assert placed_at_from_hhmmss("") == "" and placed_at_from_hhmmss(None) == ""
    assert placed_at_from_hhmmss("1015") == ""  # 자릿수 부족
    assert placed_at_from_ms(None) == "" and placed_at_from_ms("x") == ""
    assert placed_at_from_ms(0) == ""
    assert len(placed_at_from_ms(1789084276518)) == 8  # 'HH:MM:SS'(현지 시각)


def test_placed_epoch_parsers() -> None:
    import time as _t

    from kp_arb.gateways.base import placed_epoch_from_hhmmss, placed_epoch_from_ms

    assert placed_epoch_from_ms(1789084276518) == 1789084276.518
    assert placed_epoch_from_ms(None) == 0.0 and placed_epoch_from_ms("x") == 0.0
    # LS 시각 → 오늘 날짜의 epoch(정렬용). 같은 날 안에서 순서가 맞으면 된다.
    now = _t.mktime((2026, 9, 11, 12, 0, 0, 0, 0, -1))
    a = placed_epoch_from_hhmmss("090922123", now)
    b = placed_epoch_from_hhmmss("101530", now)
    assert 0 < a < b and b - a == (1 * 3600 + 6 * 60 + 8)
    assert placed_epoch_from_hhmmss("", now) == 0.0


async def test_snapshot_orders_can_be_cancelled_after_restart() -> None:
    # 2026-09-09(사용자): 재시동 뒤에도 시동 미체결 조회로 알게 된 주문은 취소돼야 한다.
    # 주문 문맥(_orders)이 비어 "unknown order"였던 것을 스냅샷 행으로 복원.
    transport = AccountTransport()
    gw = _gateway(transport)
    await gw.get_open_orders(Account.KR_DERIV)
    await gw.get_open_orders(Account.KR_STOCK)
    await gw.cancel_order("69105", qty=4)          # 선물 — 남은 수량 4
    await gw.cancel_order("7267", qty=1)           # 주식
    assert transport.seen_trs[-2:] == ["CFOAT00300", "CSPAT00801"]
    fut = transport.bodies[-2]["CFOAT00300InBlock1"]
    assert fut["FnoIsuNo"] == "A1167000" and fut["OrgOrdNo"] == 69105 and fut["CancQty"] == 4
    spot = transport.bodies[-1]["CSPAT00801InBlock1"]
    assert spot["IsuNo"] == "A005930" and spot["OrgOrdNo"] == 7267 and spot["OrdQty"] == 1


async def test_deriv_open_orders_paper_unsupported_stays_unreconciled() -> None:
    # 모의 미제공(01900)이면 빈 결과 + 유령 정리 제외 유지(빈 결과를 성공으로 보면 안 됨).
    transport = AccountTransport()
    saved = FIXTURES["t0434"]
    FIXTURES["t0434"] = {"rsp_cd": "01900", "rsp_msg": "모의투자 미제공"}
    try:
        gw = _gateway(transport)
        assert await gw.get_open_orders(Account.KR_DERIV) == []
        assert not gw.open_orders_supported(Account.KR_DERIV)
    finally:
        FIXTURES["t0434"] = saved


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
    # 실측 v6.1: 모의 미제공 TR(rsp_cd 01900)은 오류가 아니라 빈 결과.
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
