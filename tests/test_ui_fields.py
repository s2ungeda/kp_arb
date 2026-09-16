"""주문 화면 공용 입력 헬퍼(ui_fields) — 파싱·입력필터·매핑.

옛 자동T 화면 테스트에서 옮김(2026-09-16, 자동T 화면 삭제)."""
from kp_arb.domain.enums import Underlying
from kp_arb.ui_fields import (
    ORDER_TYPES,
    UNDER_MAP,
    format_qty,
    is_decimal_text,
    is_int_text,
    is_qty_text,
    is_signed_int_text,
    is_time_text,
    parse_qty,
    parse_threshold,
)


def test_input_filters() -> None:
    # 정수칸: 숫자만 / 소수칸: 부호·소수점 / 시간칸: 숫자·콜론 (입력 중간 상태 허용)
    assert is_int_text("") and is_int_text("120")
    assert not is_int_text("1.5") and not is_int_text("abc") and not is_int_text("-3")
    # 부호 정수칸(역방향 RT 수동 입력, 2026-09-15): '-' 입력 중 상태와 음수 허용
    assert is_signed_int_text("") and is_signed_int_text("-") and is_signed_int_text("-3")
    assert is_signed_int_text("7") and not is_signed_int_text("1.5")
    assert not is_signed_int_text("--1")
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


def test_order_types_present() -> None:
    assert ORDER_TYPES["003"] == "유통/자기융자신규"
    assert ORDER_TYPES["105"] == "유통대주상환"


def test_qty_text_allows_thousand_commas() -> None:
    # 체결쏴 주식 세트설정(사용자 2026-09-16): 목표수량·1회주문수량 천 단위 쉼표 표시·입력
    assert is_qty_text("") and is_qty_text("10,000") and is_qty_text("1000")
    assert not is_qty_text("1.5") and not is_qty_text("-1")
    assert parse_qty("10,000") == 10000 and parse_qty("1,0,0") == 100 and parse_qty(",") == 0
    assert format_qty(10000) == "10,000" and format_qty(0) == "0"
