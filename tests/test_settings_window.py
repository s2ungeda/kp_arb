"""공통설정 창 — 금액 포맷(지수표현 방지 + 3자리 콤마)·원달러선물 콤보 표시 순수 로직."""
from kp_arb.settings_window import _fmt_amount, fx_month_code, fx_month_label


def test_fx_month_label_and_code_roundtrip() -> None:
    # 원달러선물 콤보(2026-09-28): '최근월물|차근월물 코드 (N월)'로 보여 주고, 저장 땐 코드만.
    assert fx_month_label("A756A000", 202610, 0) == "최근월물 A756A000 (10월)"
    assert fx_month_label("A756B000", 202611, 1) == "차근월물 A756B000 (11월)"
    assert fx_month_code("최근월물 A756A000 (10월)") == "A756A000"
    assert fx_month_code("") == ""  # 콤보가 비었으면(코어 미접속) 빈 값


def test_fmt_amount_thousands_comma() -> None:
    assert _fmt_amount(1000) == "1,000"
    assert _fmt_amount(5_000_000_000) == "5,000,000,000"  # 5e+09 (지수) 아님
    assert _fmt_amount(0) == "0"


def test_fmt_amount_keeps_decimals() -> None:
    assert _fmt_amount(1234.5) == "1,234.50"
    assert _fmt_amount(999.99) == "999.99"
