"""xingAPI 게이트웨이(xing.py) — 전송 단만 xing인 LSApiGateway. 가짜 XingSession, 라이브 없음."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kp_arb.config import LSAccount, LSAccounts
from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent
from kp_arb.gateways.ls_rest import RateLimitError, RestError, RestTimeoutError
from kp_arb.gateways.xing import XingGateway, XingQueryClient, in_block_name, to_blocks
from kp_arb.gateways.xing_com import LANE_ORDER, LANE_QUERY, QueryResult, XingTimeout


class FakeXingSession:
    """XingSession의 query/limits만 흉내 — 무엇을 어느 차선으로 보냈는지 기록."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str, bool]] = []
        self.responses: dict[str, QueryResult] = {}
        self.timeouts: set[str] = set()
        self.limit_state: dict[str, tuple[int, int, int, int]] = {}

    async def limits(self, tr: str) -> tuple[int, int, int, int]:
        return self.limit_state.get(tr, (10, 1, 200, 0))

    async def query(self, tr: str, blocks: dict[str, Any], *, lane: str = LANE_QUERY,
                    next_: bool = False, timeout_s: float | None = None) -> QueryResult:
        self.calls.append((tr, blocks, lane, next_))
        if tr in self.timeouts:
            raise XingTimeout(f"{tr} 응답 없음(시간 초과)")
        return self.responses.get(tr, QueryResult(tr, "00000", ""))


def _accounts() -> LSAccounts:
    return LSAccounts(
        LSAccount("2014-2871-001", "pw1", "k", "s"),
        LSAccount("2014-2871-002", "pw2", "k", "s"))


def test_to_blocks_wraps_flat_bodies(tmp_path: Path) -> None:
    # REST의 몇몇 조회는 블록 없이 {AcntNo, Pwd}로 보낸다 → xing은 InBlock 이름이 필요.
    assert to_blocks("t1102", {"t1102InBlock": {"shcode": "005930"}}) == {
        "t1102InBlock": {"shcode": "005930"}}
    assert to_blocks("CSPAQ22200", {"AcntNo": "1", "Pwd": "p"}) == {
        "CSPAQ22200InBlock1": {"AcntNo": "1", "Pwd": "p"}}  # Res 없으면 관례
    assert to_blocks("t0441", {"accno": "1"}) == {"t0441InBlock": {"accno": "1"}}
    assert to_blocks("t1102", None) == {} and to_blocks("t1102", {}) == {}
    # Res가 있으면 첫 입력 블록 이름을 쓴다
    (tmp_path / "CSPAQ22200.res").write_bytes(
        ("BEGIN_FUNCTION_MAP\n.Func,예수금,CSPAQ22200,attr;\nBEGIN_DATA_MAP\n"
         "CSPAQ22200InBlock1,입력,input;\nbegin\n계좌,AcntNo,AcntNo,char,20;\nend\n"
         "END_DATA_MAP\nEND_FUNCTION_MAP\n").encode("cp949"))
    assert in_block_name("CSPAQ22200", {}, tmp_path) == "CSPAQ22200InBlock1"
    assert in_block_name("t9999", {}, tmp_path) == "t9999InBlock"  # Res 없음 → 관례
    # 실측 2026-09-23: 선물 주문 Res의 비밀번호 필드는 Pwd(REST는 InptPwd) → 별칭으로 바꾸고,
    # Res에 없는 필드(예: REST만의 것)는 뺀다
    (tmp_path / "CFOAT00300.res").write_bytes(
        ("BEGIN_FUNCTION_MAP\n.Func,취소,CFOAT00300,attr;\nBEGIN_DATA_MAP\n"
         "CFOAT00300InBlock1,입력,input;\nbegin\n계좌,AcntNo,AcntNo,char,20;\n"
         "비번,Pwd,Pwd,char,8;\n종목,FnoIsuNo,FnoIsuNo,char,12;\n원주문,OrgOrdNo,OrgOrdNo,long,10;\n"
         "수량,CancQty,CancQty,long,16;\nend\nEND_DATA_MAP\nEND_FUNCTION_MAP\n").encode("cp949"))
    blocks = to_blocks("CFOAT00300", {"CFOAT00300InBlock1": {
        "AcntNo": "1", "InptPwd": "pw", "FnoIsuNo": "A", "OrgOrdNo": 5, "CancQty": 1,
        "NotInRes": "x"}}, tmp_path)
    assert blocks == {"CFOAT00300InBlock1": {
        "AcntNo": "1", "Pwd": "pw", "FnoIsuNo": "A", "OrgOrdNo": 5, "CancQty": 1}}


async def test_query_client_lanes_timeouts_and_limits() -> None:
    fake = FakeXingSession()
    client = XingQueryClient(fake)  # type: ignore[arg-type]
    fake.responses["t1102"] = QueryResult("t1102", "00000", "",
                                          {"t1102OutBlock": {"price": "70100"}})
    resp = await client.request("t1102", {"t1102InBlock": {"shcode": "005930"}},
                                path="/stock/market-data")
    assert resp.status_code == 200
    assert resp.body == {"t1102OutBlock": {"price": "70100"}, "rsp_cd": "00000", "rsp_msg": ""}
    assert fake.calls[-1] == ("t1102", {"t1102InBlock": {"shcode": "005930"}}, LANE_QUERY, False)
    # 주문 TR은 주문 차선, 연속조회는 next
    await client.request("CFOAT00100", {"CFOAT00100InBlock1": {"FnoIsuNo": "A"}})
    assert fake.calls[-1][2] == LANE_ORDER
    await client.request("t0434", {"t0434InBlock": {"accno": "1"}}, tr_cont="Y")
    assert fake.calls[-1][3] is True
    # 응답 없음: 주문은 RestTimeoutError(결과 모름), 조회는 RestError
    fake.timeouts.update({"CFOAT00300", "t1102"})
    with pytest.raises(RestTimeoutError):
        await client.request("CFOAT00300", {"CFOAT00300InBlock1": {"OrgOrdNo": 1}})
    with pytest.raises(RestError):
        await client.request("t1102", {"t1102InBlock": {"shcode": "1"}})
    # 한도: xing 누적 카운터가 한도에 닿으면, 또는 초당 건수를 넘기면 RateLimitError(전송 안 함)
    fake.limit_state["t8402"] = (10, 1, 200, 200)
    with pytest.raises(RateLimitError, match="요청 한도"):
        await client.request("t8402", {"t8402InBlock": {"focode": "A"}})
    fake.limit_state["t2111"] = (2, 1, 200, 0)
    await client.request("t2111", {"t2111InBlock": {"focode": "A"}})
    await client.request("t2111", {"t2111InBlock": {"focode": "A"}})
    with pytest.raises(RateLimitError, match="초당 한도"):
        await client.request("t2111", {"t2111InBlock": {"focode": "A"}})
    assert sum(1 for c in fake.calls if c[0] == "t8402") == 0  # 한도에 걸린 건 전송 안 됨


async def test_stock_order_tr_is_renamed_to_xing_res_name() -> None:
    # 실측 2026-09-23: xing Res의 주식 주문은 CSPAT00600/00700/00800(REST는 …601/701/801).
    # 요청 블록·응답 블록 접두를 바꿔 게이트웨이는 REST 이름 그대로 본문을 만들고 OrdNo를 읽는다.
    from kp_arb.gateways.xing import rename_tr_prefix

    assert rename_tr_prefix({"CSPAT00601InBlock1": {"a": 1}, "rsp_cd": "0"},
                            "CSPAT00601", "CSPAT00600") == {
        "CSPAT00600InBlock1": {"a": 1}, "rsp_cd": "0"}
    fake = FakeXingSession()
    gw = XingGateway.from_session(fake, _accounts())  # type: ignore[arg-type]
    fake.responses["CSPAT00600"] = QueryResult(
        "CSPAT00600", "00040", "매수 주문이 완료되었습니다.",
        {"CSPAT00600OutBlock2": {"OrdNo": "77"}})
    intent = OrderIntent(venue=Venue.LS, underlying=Underlying.SAMSUNG,
                         instrument=Instrument.KR_STOCK, side=Side.BUY, qty=1,
                         order_type=OrderType.LIMIT, price=70_000.0, market="nxt")
    assert await gw.place_order(intent) == "77"
    tr, blocks, lane, _ = fake.calls[-1]
    assert tr == "CSPAT00600" and lane == LANE_ORDER
    body = blocks["CSPAT00600InBlock1"]
    assert body["IsuNo"] == "A005930" and body["MbrNo"] == "NXT" and body["MgntrnCode"] == "000"
    assert "CSPAT00601InBlock1" not in blocks


async def test_gateway_inherits_bodies_and_parsing() -> None:
    # XingGateway = LSApiGateway + xing 전송: 선물 발주 본문(FnoIsuNo·InptPwd…)·주문번호 파싱·
    # 거부(rsp_cd) 처리가 전부 상속된 그대로 동작해야 한다.
    fake = FakeXingSession()
    gw = XingGateway.from_session(fake, _accounts(),  # type: ignore[arg-type]
                                  futures_symbols={Underlying.SK_HYNIX: "A1167000"})
    fake.responses["CFOAT00100"] = QueryResult(
        "CFOAT00100", "00039", "매도 주문이 완료되었습니다.", {
            "CFOAT00100OutBlock1": {"RecCnt": "1"},
            "CFOAT00100OutBlock2": {"OrdNo": "605"}})
    intent = OrderIntent(venue=Venue.LS, underlying=Underlying.SK_HYNIX,
                         instrument=Instrument.KR_STOCK_FUTURE, side=Side.SELL, qty=1,
                         order_type=OrderType.LIMIT, price=1_770_000.0)
    assert await gw.place_order(intent) == "605"
    tr, blocks, lane, _ = fake.calls[-1]
    assert tr == "CFOAT00100" and lane == LANE_ORDER
    body = blocks["CFOAT00100InBlock1"]
    assert body["FnoIsuNo"] == "A1167000" and body["BnsTpCode"] == "1"
    assert body["AcntNo"] == "20142871002" and body["InptPwd"] == "pw2"  # 선물 계좌
    assert body["OrdQty"] == 1 and body["FnoOrdPrc"] == 1_770_000.0
    # 취소는 원주문 문맥(상속)으로 CFOAT00300, 거부 rsp_cd → RestError
    fake.responses["CFOAT00300"] = QueryResult("CFOAT00300", "01433", "정정/취소할 수량이 없습니다")
    from kp_arb.gateways.ls import OrderGoneError

    with pytest.raises(OrderGoneError):
        await gw.cancel_order("605", 1)
    assert fake.calls[-1][0] == "CFOAT00300"
    assert fake.calls[-1][1]["CFOAT00300InBlock1"]["OrgOrdNo"] == 605
    # 잔고 조회(flat 본문) → InBlock1로 싸서 보내고 OutBlock2를 그대로 읽는다
    fake.responses["CFOBQ10500"] = QueryResult(
        "CFOBQ10500", "00000", "", {"CFOBQ10500OutBlock2": {"MnyOrdAbleAmt": "1234567"}})
    assert await gw.get_balance(Account.KR_DERIV) == 1_234_567.0
    assert "CFOBQ10500InBlock1" in fake.calls[-1][1]
    assert fake.calls[-1][1]["CFOBQ10500InBlock1"]["AcntNo"] == "20142871002"
    # FX 계좌가 없으면 KR_FX 클라이언트도 없다(상속된 place_fx_futures가 거부)
    with pytest.raises(RestError, match="KR_FX"):
        await gw.place_fx_futures("A7569000", Side.SELL, 1, 1380.0)
    await asyncio.sleep(0)
