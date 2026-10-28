"""동시호가 주문 화면 순수 부분 — 예약 행 잠금 판정."""
from kp_arb.auction_order_screen import any_reserved


def test_any_reserved_locks_top_row_when_any_row_is_on() -> None:
    # 사용자 2026-10-26: 예약 행이 하나라도 켜져 있으면 월물·호가단위 콤보와 '적'을 잠근다
    assert not any_reserved({})
    assert not any_reserved({("buy", 0): False, ("sell", 3): False})
    assert any_reserved({("buy", 0): False, ("sell", 3): True})


def test_row_missing_names_empty_spread_or_qty() -> None:
    # 사용자 2026-10-26: Sprd·수량이 없으면 예약을 못 켠다 — 빠진 것 이름을 알림에
    from kp_arb.auction_order_screen import row_missing

    assert row_missing("0.3", "20") is None and row_missing("-0.1", "1,000") is None
    assert row_missing("", "20") == "주문Sprd" and row_missing("0.3", "") == "수량"
    assert row_missing("0.3", "0") == "수량" and row_missing("", "") == "주문Sprd·수량"
    assert row_missing("x", "-1") == "주문Sprd·수량"


def test_fx_missing_requires_tick_hedge_and_unit() -> None:
    # 사용자 2026-10-26: 틱·헤지비율·1회주문수량이 없으면 환 예약실행을 못 켠다(틱은 0 허용)
    from kp_arb.auction_order_screen import fx_missing

    assert fx_missing("20", "100", "10") is None and fx_missing("0", "50", "1") is None
    assert fx_missing("-5", "100", "10") is None   # 틱 음수 허용(사용자 2026-10-27)
    assert fx_missing("-", "100", "10") == "틱"    # '-'만 친 상태는 아직 값 아님
    assert fx_missing("", "100", "10") == "틱" and fx_missing("20", "0", "10") == "헤지비율"
    assert fx_missing("20", "100", "") == "1회주문수량"
    assert fx_missing("", "", "x") == "틱·헤지비율·1회주문수량"


def test_hl_missing_and_pt_missing() -> None:
    # 사용자 2026-10-26: 주문설정 a~d는 비면 0(스펙 §5 유지 — "이것만 원복"), 숫자 아닌 글만 HL
    # 예약실행을 막는다. 정정PT가 비거나 0 이하면 정정 못 함. 취소는 입력이 없어 검사 없음(사용자
    # 확인)
    from kp_arb.auction_order_screen import hl_missing, pt_missing

    assert hl_missing({"a": "0.5", "b": "", "c": "-0.5", "d": "0"}) is None
    assert hl_missing({"a": "", "b": "0", "c": "x", "d": "1"}) == "c"
    assert not pt_missing("0.5") and pt_missing("") and pt_missing("0") and pt_missing("-1")
