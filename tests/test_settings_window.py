"""공통설정 창 — 금액 포맷(지수표현 방지 + 3자리 콤마) 순수 로직."""
from kp_arb.settings_window import _fmt_amount


def test_fmt_amount_thousands_comma() -> None:
    assert _fmt_amount(1000) == "1,000"
    assert _fmt_amount(5_000_000_000) == "5,000,000,000"  # 5e+09 (지수) 아님
    assert _fmt_amount(0) == "0"


def test_fmt_amount_keeps_decimals() -> None:
    assert _fmt_amount(1234.5) == "1,234.50"
    assert _fmt_amount(999.99) == "999.99"
