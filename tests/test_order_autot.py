"""자동T 화면 순수 로직 테스트 (파싱·입력필터·매핑)."""
from kp_arb.domain.enums import Underlying
from kp_arb.order_autot import (
    AGG_CHOICES,
    ORDER_TYPES,
    UNDER_MAP,
    is_decimal_text,
    is_int_text,
    is_time_text,
    parse_qty,
    parse_threshold,
)


def test_input_filters() -> None:
    # 정수칸: 숫자만 / 소수칸: 부호·소수점 / 시간칸: 숫자·콜론 (입력 중간 상태 허용)
    assert is_int_text("") and is_int_text("120")
    assert not is_int_text("1.5") and not is_int_text("abc") and not is_int_text("-3")
    assert is_decimal_text("") and is_decimal_text("-") and is_decimal_text("0.075")
    assert is_decimal_text("-.3") and is_decimal_text("12.")
    assert not is_decimal_text("1.2.3") and not is_decimal_text("1e3")
    assert is_time_text("") and is_time_text("08:30:10")
    assert not is_time_text("08-30") and not is_time_text("8h")


def test_parse_qty() -> None:
    assert parse_qty(" 10 ") == 10
    assert parse_qty("") == 0        # 빈칸/오타는 0
    assert parse_qty("abc") == 0


def test_parse_threshold() -> None:
    assert parse_threshold("0.5") == 0.5
    assert parse_threshold("-1.2") == -1.2   # 역방향 음수 기준값
    assert parse_threshold("") is None
    assert parse_threshold("x") is None


def test_under_map_matches_domain() -> None:
    assert {Underlying(v) for v in UNDER_MAP.values()} == set(Underlying)


def test_maps_present() -> None:
    assert AGG_CHOICES["원시"] == (None, None) and "10배" in AGG_CHOICES
    assert ORDER_TYPES["003"] == "유통/자기융자신규"
    assert ORDER_TYPES["105"] == "유통대주상환"
