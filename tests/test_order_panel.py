"""주문 화면 순수 로직 테스트 (파싱·매핑·운영시간 표시)."""
from kp_arb.domain.enums import Underlying
from kp_arb.order_panel import (
    UNDER_MAP,
    operating_text,
    parse_qty,
    parse_threshold,
)
from kp_arb.strategy_core import ScreenKind


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
