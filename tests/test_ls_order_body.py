"""LS 현물 주문 본문 — 신용거래코드·NXT 시장(체결쏴 주식, 2026-09-17)."""
from types import SimpleNamespace

from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent
from kp_arb.gateways.ls import LSApiGateway


def _fake() -> SimpleNamespace:
    return SimpleNamespace(
        SPOT_ORDER_TR="CSPAT00601",
        _order_account_fields=lambda account: {"AcntNo": "1", "InptPwd": "p"},
        _spot_isu=lambda intent: "A000660")


def _intent(**kw: object) -> OrderIntent:
    return OrderIntent(venue=Venue.LS, underlying=Underlying.SK_HYNIX,
                       instrument=Instrument.KR_STOCK, side=Side.BUY, qty=4,
                       order_type=OrderType.LIMIT, price=100_200.0, **kw)  # type: ignore[arg-type]


def test_spot_order_body_default_is_cash_krx() -> None:
    # 기본(일반주문창 등): 신용 없음 000, 회원사번호 KRX(LS 문서: 필수, 그 외 값은 KRX 처리)
    body = LSApiGateway._spot_order_body(_fake(), _intent(), Account.KR_STOCK)  # type: ignore[arg-type]
    block = body["CSPAT00601InBlock1"]
    assert block["MgntrnCode"] == "000" and block["MbrNo"] == "KRX"
    assert block["IsuNo"] == "A000660" and block["BnsTpCode"] == "2" and block["OrdPrc"] == 100_200


def test_spot_order_body_credit_and_nxt() -> None:
    # 체결쏴 주식 신용 세트 진입(003, 추측값) + 거래소 NXT → MbrNo "NXT"(LS 카탈로그 예시 본문)
    intent = _intent(credit_code="003", market="nxt")
    block = LSApiGateway._spot_order_body(_fake(), intent, Account.KR_STOCK)[  # type: ignore[arg-type]
        "CSPAT00601InBlock1"]
    assert block["MgntrnCode"] == "003" and block["MbrNo"] == "NXT" and block["LoanDt"] == ""
    # 상환(101)은 대출일 필수(실측 2026-09-18 01486) — 엔진이 채운 loan_date가 LoanDt로
    repay = _intent(credit_code="101", loan_date="20260918")
    block = LSApiGateway._spot_order_body(_fake(), repay, Account.KR_STOCK)[  # type: ignore[arg-type]
        "CSPAT00601InBlock1"]
    assert block["MgntrnCode"] == "101" and block["LoanDt"] == "20260918"


def test_merge_stock_positions_sums_cash_and_credit_rows() -> None:
    # 같은 종목이 현금·신용(대출일별) 행으로 나뉘어 오면 합산(수량 합, 평균단가 가중) — 행마다
    # 내면 장부가 마지막 행으로 덮어써 수량이 줄어 보인다(2026-09-18)
    from kp_arb.domain.models import Position
    from kp_arb.gateways.ls import merge_stock_positions

    def pos(u: Underlying, qty: float, avg: float) -> Position:
        return Position(venue=Venue.LS, instrument=Instrument.KR_STOCK, underlying=u,
                        side=Side.BUY, qty=qty, avg_price=avg, account=Account.KR_STOCK)

    merged = merge_stock_positions([pos(Underlying.SAMSUNG, 2, 250_000.0),
                                    pos(Underlying.SAMSUNG, 1, 256_000.0),
                                    pos(Underlying.SK_HYNIX, 3, 100_000.0)])
    by = {p.underlying: p for p in merged}
    assert by[Underlying.SAMSUNG].qty == 3 and by[Underlying.SAMSUNG].avg_price == 252_000.0
    assert by[Underlying.SK_HYNIX].qty == 3 and len(merged) == 2
