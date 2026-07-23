"""주문 화면 순수 로직 테스트 (파싱·매핑·운영시간 표시)."""
from kp_arb.domain.enums import Underlying
from kp_arb.order_panel import (
    UNDER_MAP,
    fraction_to_pct_text,
    is_decimal_text,
    is_int_text,
    operating_text,
    parse_qty,
    parse_threshold,
    threshold_to_fraction,
)
from kp_arb.strategy_core import ScreenKind


def test_input_filters() -> None:
    # 정수칸: 숫자만 / 소수칸: 부호·소수점 포함 숫자 형태만 (입력 중간 상태 허용)
    assert is_int_text("") and is_int_text("120")
    assert not is_int_text("1.5") and not is_int_text("abc") and not is_int_text("-3")
    assert is_decimal_text("") and is_decimal_text("-") and is_decimal_text("0.075")
    assert is_decimal_text("-.3") and is_decimal_text("12.")
    assert not is_decimal_text("1.2.3") and not is_decimal_text("1e3")
    assert not is_decimal_text("abc") and not is_decimal_text("0.0%")


def test_threshold_pct_conversion() -> None:
    # 입력은 %(괴리보드와 동일 단위), 코어는 소수: 0.075(%) → 0.00075
    assert threshold_to_fraction("0.075") == 0.00075
    assert threshold_to_fraction("-0.3") == -0.003
    assert threshold_to_fraction("") is None
    assert fraction_to_pct_text(0.00075) == "0.075"
    assert fraction_to_pct_text(-0.003) == "-0.3"


def test_parse_qty() -> None:
    assert parse_qty(" 10 ") == 10
    assert parse_qty("") == 0        # 빈칸/오타는 0 — 코어 검증에서 거부됨
    assert parse_qty("abc") == 0


def test_parse_threshold() -> None:
    assert parse_threshold("0.006") == 0.006
    assert parse_threshold("") is None
    assert parse_threshold("x") is None


def test_under_map_matches_domain() -> None:
    assert {Underlying(v) for v in UNDER_MAP.values()} == set(Underlying)


def test_operating_text() -> None:
    assert operating_text(ScreenKind.AUTO_M) == "08:45~15:35"
    assert operating_text(ScreenKind.AUTO_T).count("/") == 2
