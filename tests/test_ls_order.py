"""LS 현물 주문 게이트웨이 계약 테스트. 라이브 없음(mock 전송 + 녹화 픽스처)."""
from typing import Any

import pytest

from kp_arb.config import LSAccount, LSAccounts
from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent
from kp_arb.gateways.ls import (
    LSApiGateway,
    MarketClosedError,
    OrderGoneError,
    is_market_closed_reject,
)
from kp_arb.gateways.ls_tr import RestError, RestResponse
from kp_arb.routing import account_for

BASE_URL = "https://openapi.ls-sec.co.kr:8080"


class _Requester:
    """TrRequester 어댑터 — 옛 REST 전송 가짜(method, url, headers, body)를 그대로 재사용한다
    (2026-11-09 REST 경로 제거 뒤에도 TR 본문·응답 파싱 테스트는 전송 방식과 무관)."""

    supports_continuation = False

    def __init__(self, transport: Any) -> None:
        self.t = transport

    async def request(self, tr_cd: str, body: dict[str, Any] | None = None, *,
                      path: str = "/", method: str = "POST", tr_cont: str = "N") -> RestResponse:
        return await self.t.request(method, BASE_URL + path,
                                    {"tr_cd": tr_cd, "tr_cont": tr_cont}, body)


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t



class OrderTransport:
    """녹화 픽스처: 모든 주문 TR에 대해 성공 + OutBlock2.OrdNo(실측: 숫자)를 돌려준다."""

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
            # 실측 shape: 성공 rsp_cd "00040"(매수), OrdNo는 int.
            body={
                "rsp_cd": "00040",
                "rsp_msg": "모의투자 매수주문이 완료 되었습니다.",
                f"{tr}OutBlock2": {"OrdNo": 2900 + self.counter},
            },
        )


def inblk(req: dict[str, Any], tr_cd: str) -> dict[str, Any]:
    """주문 요청 body의 InBlock1(실측: 주문 TR은 InBlock1 래핑 필수)."""
    return req["body"][f"{tr_cd}InBlock1"]


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
    etf_symbols: dict[Underlying, str] | None = None,
    accounts: LSAccounts | None = None,
    next_futures_symbols: dict[Underlying, str] | None = None,
) -> LSApiGateway:
    rest = _Requester(transport)
    return LSApiGateway(
        # 테스트는 세 계좌 같은 mock rest (KR_FX 포함 — 원달러선물 발주 테스트용).
        {Account.KR_STOCK: rest, Account.KR_DERIV: rest, Account.KR_FX: rest},
        accounts=accounts,
        futures_symbols=futures_symbols,
        etf_symbols=etf_symbols,
        next_futures_symbols=next_futures_symbols,
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
    blk = inblk(req, LSApiGateway.SPOT_ORDER_TR)
    assert blk["account"] == Account.KR_STOCK.value
    assert blk["IsuNo"] == "A005930"  # 현물 주문은 A 접두(실측)


async def test_etf_routes_to_stock_account_with_etf_code() -> None:
    transport = OrderTransport()
    gw = _gateway(transport, etf_symbols={Underlying.SAMSUNG: "0193W0"})
    await gw.place_order(_intent(Instrument.KR_ETF))
    blk = inblk(transport.requests[-1], LSApiGateway.SPOT_ORDER_TR)
    assert blk["account"] == Account.KR_STOCK.value
    assert blk["IsuNo"] == "A0193W0"  # ETF는 자기 종목코드(기초자산 코드 아님)


async def test_etf_order_without_symbol_raises() -> None:
    gw = _gateway(OrderTransport())  # etf_symbols 미설정 → ETF 미취급
    with pytest.raises(RestError):
        await gw.place_order(_intent(Instrument.KR_ETF))


async def test_future_order_uses_cfoat_and_routes_to_deriv() -> None:
    # 선물 → 선물옵션계좌(라우팅 계약) + CFOAT00100 신규주문.
    assert account_for(Instrument.KR_STOCK_FUTURE) is Account.KR_DERIV
    transport = OrderTransport()
    gw = _gateway(transport, futures_symbols={Underlying.SAMSUNG: "1AB3000"})
    oid = await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE, side=Side.SELL))
    req = transport.requests[-1]
    assert req["headers"]["tr_cd"] == LSApiGateway.FUTURE_ORDER_TR
    blk = inblk(req, LSApiGateway.FUTURE_ORDER_TR)
    assert blk["account"] == Account.KR_DERIV.value
    assert blk["FnoIsuNo"] == "1AB3000"
    assert oid == "2901"


async def test_next_month_future_order_uses_next_code() -> None:
    # 차근월물(§5.11): 같은 CFOAT00100·선물계좌, 종목코드만 차근 코드. 정정·취소도 차근 코드 유지.
    transport = OrderTransport()
    gw = _gateway(transport, futures_symbols={Underlying.SAMSUNG: "1AB3000"},
                  next_futures_symbols={Underlying.SAMSUNG: "1AB6000"})
    oid = await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE_NEXT, side=Side.SELL))
    req = transport.requests[-1]
    assert req["headers"]["tr_cd"] == LSApiGateway.FUTURE_ORDER_TR
    blk = inblk(req, LSApiGateway.FUTURE_ORDER_TR)
    assert blk["account"] == Account.KR_DERIV.value
    assert blk["FnoIsuNo"] == "1AB6000"
    await gw.amend_order(oid, qty=5, price=71_000.0)
    assert inblk(transport.requests[-1], LSApiGateway.FUTURE_AMEND_TR)["FnoIsuNo"] == "1AB6000"


async def test_next_month_order_without_next_code_raises() -> None:
    # 근월물 코드만 있고 차근이 없으면(차근 미상장) 차근 주문은 거부 — 근월물로 대체 발주 금지.
    gw = _gateway(OrderTransport(), futures_symbols={Underlying.SAMSUNG: "1AB3000"})
    with pytest.raises(RestError):
        await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE_NEXT))


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
    blk = inblk(req, LSApiGateway.FUTURE_AMEND_TR)
    assert blk["OrgOrdNo"] == int(oid)  # 원주문 보존(실측: 숫자)
    assert blk["FnoIsuNo"] == "1AB3000"
    assert blk["MdfyQty"] == 5
    assert new_id != oid


async def test_future_cancel_uses_cfoat00300() -> None:
    transport = OrderTransport()
    gw = _gateway(transport, futures_symbols={Underlying.SAMSUNG: "1AB3000"})
    oid = await gw.place_order(_intent(Instrument.KR_STOCK_FUTURE))
    await gw.cancel_order(oid)
    req = transport.requests[-1]
    assert req["headers"]["tr_cd"] == LSApiGateway.FUTURE_CANCEL_TR
    blk = inblk(req, LSApiGateway.FUTURE_CANCEL_TR)
    assert blk["OrgOrdNo"] == int(oid)
    assert blk["CancQty"] == 10  # 원주문 수량
    # 남은 수량을 넘기면 그 수량으로 — 원주문 수량으로 보내면 부분체결 뒤 01443 거부(실측 09-09)
    await gw.cancel_order(oid, qty=7)
    blk2 = inblk(transport.requests[-1], LSApiGateway.FUTURE_CANCEL_TR)
    assert blk2["CancQty"] == 7


async def test_fx_futures_uses_cfoat00100_on_kr_fx() -> None:
    # 원달러선물 발주 — 선물옵션 TR(CFOAT00100), 콤보 종목코드를 FnoIsuNo에, 매도=BnsTpCode 1.
    transport = OrderTransport()
    gw = _gateway(transport)
    oid = await gw.place_fx_futures("175V9000", Side.SELL, 2, 1420.5)
    req = transport.requests[-1]
    assert req["headers"]["tr_cd"] == LSApiGateway.FUTURE_ORDER_TR
    blk = inblk(req, LSApiGateway.FUTURE_ORDER_TR)
    assert blk["FnoIsuNo"] == "175V9000"      # 콤보 종목코드 그대로
    assert blk["OrdQty"] == 2
    assert blk["FnoOrdPrc"] == 1420.5
    assert blk["BnsTpCode"] == "1"            # 매도
    assert blk["FnoOrdprcPtnCode"] == "00"    # 지정가
    assert oid


async def test_fx_futures_buy_bnstpcode() -> None:
    transport = OrderTransport()
    gw = _gateway(transport)
    await gw.place_fx_futures("175V9000", Side.BUY, 1, 1422.5)
    blk = inblk(transport.requests[-1], LSApiGateway.FUTURE_ORDER_TR)
    assert blk["BnsTpCode"] == "2"            # 매수


async def test_fx_futures_missing_kr_fx_raises() -> None:
    # KR_FX 미등록(선택 계좌) 환경에서 발주하면 명확히 거부.
    transport = OrderTransport()
    rest = _Requester(transport)
    gw = LSApiGateway({Account.KR_STOCK: rest})  # KR_FX 없음
    with pytest.raises(RestError):
        await gw.place_fx_futures("175V9000", Side.SELL, 1, 1420.5)


async def test_order_injects_account_number_and_password() -> None:
    # 실 계좌번호·비번(env→LSAccounts)이 주문 body에 주입됨. (테스트는 더미값)
    accounts = LSAccounts(
        LSAccount("STK-1", "spw", "sak", "sas"),
        LSAccount("DRV-1", "dpw", "dak", "das"),
    )
    transport = OrderTransport()
    gw = _gateway(transport, accounts=accounts)
    await gw.place_order(_intent(Instrument.KR_STOCK))
    blk = inblk(transport.requests[-1], LSApiGateway.SPOT_ORDER_TR)
    assert blk["AcntNo"] == "STK1"  # 대시 제거됨
    assert blk["InptPwd"] == "spw"  # 주문은 InptPwd(실측 — 조회는 Pwd)
    assert "account" not in blk  # 플레이스홀더 대신 실 계좌필드


async def test_routes_to_per_account_client() -> None:
    def _rest(tx: OrderTransport) -> _Requester:
        return _Requester(tx)

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
    assert oid == "2901"  # 실측: OutBlock2.OrdNo(숫자) → str


async def test_rejected_response_raises() -> None:
    gw = _gateway(RejectTransport())
    with pytest.raises(RestError):
        await gw.place_order(_intent(Instrument.KR_STOCK))


class Reject01xxxTransport:
    """모의 거부 코드 "01xxx" 픽스처 (실측 v6.15: 01433 정정할 수량 없음)."""

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        return RestResponse(
            status_code=200,
            body={"rsp_cd": "01433", "rsp_msg": "모의투자 정정/취소할 수량이 없습니다."},
        )


async def test_rejected_01xxx_raises() -> None:
    # 성공 판정은 "00" 시작 — "01433"을 성공으로 오판해 주문번호를 찾으면 안 된다.
    # 잔량 없음(01433)은 정상 흐름의 거부이므로 전용 유형(OrderGoneError)으로 구분된다.
    gw = _gateway(Reject01xxxTransport())
    with pytest.raises(OrderGoneError, match="01433"):
        await gw.place_order(_intent(Instrument.KR_STOCK))


class Reject03416Transport:
    """운영 거부 코드 03416 픽스처 (실측 2026-09-14: 체결과 교차한 취소·중복 취소)."""

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        return RestResponse(
            status_code=200,
            body={"rsp_cd": "03416", "rsp_msg": "정정취소가능수량이 없습니다."},
        )


class RejectCodeTransport:
    """임의 거부 코드·문구 픽스처(장마감 02611 등)."""

    def __init__(self, rsp_cd: str, rsp_msg: str) -> None:
        self.body = {"rsp_cd": rsp_cd, "rsp_msg": rsp_msg}

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        return RestResponse(status_code=200, body=dict(self.body))


async def test_reject_02611_with_market_closed_word_is_market_closed() -> None:
    # 운영 실측 2026-11-06 20:00 NXT 애프터마켓 마감 뒤 취소: CSPAT00801 rsp_cd=02611
    # "장시작 전 또는 장마감 되었습니다." → 재시도 대상이 아닌 전용 오류(결정 90).
    gw = _gateway(RejectCodeTransport("02611", "장시작 전 또는 장마감 되었습니다."))
    with pytest.raises(MarketClosedError, match="02611"):
        await gw.place_order(_intent(Instrument.KR_STOCK))


async def test_reject_02611_without_market_closed_word_is_plain_reject() -> None:
    # 사용자 2026-11-09 "반드시 '장마감'": 코드만 같고 문구에 '장마감'이 없으면 일반 거부로 둔다
    # (같은 코드가 다른 문구로 올 가능성 — 추측으로 넓히지 않음).
    gw = _gateway(RejectCodeTransport("02611", "주문 가능 시간이 아닙니다."))
    with pytest.raises(RestError, match="02611") as info:
        await gw.place_order(_intent(Instrument.KR_STOCK))
    assert not isinstance(info.value, MarketClosedError)


def test_is_market_closed_reject_needs_both_code_and_word() -> None:
    assert is_market_closed_reject("02611", "장시작 전 또는 장마감 되었습니다.")
    assert not is_market_closed_reject("02611", "장시작 전입니다.")      # 문구에 '장마감' 없음
    assert not is_market_closed_reject("03416", "장마감 되었습니다.")    # 코드 다름
    assert not is_market_closed_reject("02611", None)
    assert not is_market_closed_reject(None, "장마감")


async def test_live_cancel_reject_03416_is_order_gone() -> None:
    # 운영 실측 2026-09-14: 모의 01433에 해당하는 운영 코드는 03416. 취소 실패가 아니라 "이미 없음".
    # (거부 판정 _check_ok는 발주·취소 공통 — 발주 경로로 확인)
    gw = _gateway(Reject03416Transport())
    with pytest.raises(OrderGoneError, match="03416"):
        await gw.place_order(_intent(Instrument.KR_STOCK))


# --- 정정/취소: 원주문 컨텍스트 보존 ---


async def test_cancel_preserves_original_context() -> None:
    transport = OrderTransport()
    gw = _gateway(transport)
    oid = await gw.place_order(_intent(Instrument.KR_STOCK))

    await gw.cancel_order(oid)
    cancel_req = transport.requests[-1]
    assert cancel_req["headers"]["tr_cd"] == LSApiGateway.SPOT_CANCEL_TR
    blk = inblk(cancel_req, LSApiGateway.SPOT_CANCEL_TR)
    assert blk["OrgOrdNo"] == int(oid)  # 원주문 참조(실측: 숫자)
    assert blk["account"] == Account.KR_STOCK.value
    assert blk["IsuNo"] == "A005930"  # 현물 주문 계열은 A 접두(실측)


async def test_amend_links_to_original() -> None:
    transport = OrderTransport()
    gw = _gateway(transport)
    oid = await gw.place_order(_intent(Instrument.KR_STOCK))

    new_id = await gw.amend_order(oid, qty=5, price=71_000.0)
    assert new_id != oid
    amend_req = transport.requests[-1]
    assert amend_req["headers"]["tr_cd"] == LSApiGateway.SPOT_AMEND_TR
    blk = inblk(amend_req, LSApiGateway.SPOT_AMEND_TR)
    assert blk["OrgOrdNo"] == int(oid)
    assert blk["OrdQty"] == 5
    assert blk["OrdPrc"] == 71_000


async def test_cancel_unknown_order_raises() -> None:
    gw = _gateway(OrderTransport())
    with pytest.raises(ValueError):
        await gw.cancel_order("nope")


class PriceInfoTransport:
    """t1102/t8402 현재가 응답 픽스처 — 현재가 + 상한가·하한가(Res 칸 uplmtprice/dnlmtprice)."""

    def __init__(self, block: dict[str, Any]) -> None:
        self.block = block
        self.trs: list[str] = []

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        tr = next(iter(body or {})).replace("InBlock", "")
        self.trs.append(tr)
        return RestResponse(status_code=200,
                            body={"rsp_cd": "00000", f"{tr}OutBlock": dict(self.block)})


async def test_get_price_info_reads_price_and_limits() -> None:
    # 결정 91: 현재가 조회 한 번으로 상한가·하한가까지 읽는다(주식 t1102 / 선물 t8402 같은 칸).
    tr = PriceInfoTransport({"price": "70100", "uplmtprice": "91100", "dnlmtprice": "49100"})
    gw = _gateway(tr, futures_symbols={Underlying.SAMSUNG: "A1167000"})
    info = await gw.get_price_info("005930")
    assert (info.price, info.upper, info.lower) == (70_100.0, 91_100.0, 49_100.0)
    assert await gw.get_last_price("005930") == 70_100.0          # 옛 호출은 그대로
    infos = await gw.get_price_infos()
    assert infos[(Underlying.SAMSUNG, Instrument.KR_STOCK_FUTURE)].upper == 91_100.0
    assert "t8402" in tr.trs and "t1102" in tr.trs
    # 칸이 없거나 0이면 None(관문 건너뜀) — 가격 스냅샷은 가격만
    tr2 = PriceInfoTransport({"price": "70100", "uplmtprice": "0"})
    gw2 = _gateway(tr2)
    info2 = await gw2.get_price_info("005930")
    assert (info2.price, info2.upper, info2.lower) == (70_100.0, None, None)
    assert (await gw2.get_price_snapshots())[(Underlying.SAMSUNG, Instrument.KR_STOCK)] == 70_100.0
