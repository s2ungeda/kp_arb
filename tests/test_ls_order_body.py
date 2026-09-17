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
