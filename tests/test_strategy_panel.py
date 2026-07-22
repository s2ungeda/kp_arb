"""전략 화면 모드별 위젯 상태·입력 파싱 테스트 (DESIGN §6.2-1)."""
from kp_arb.domain.enums import Instrument, Underlying
from kp_arb.strategy_panel import (
    COUNTER_MAP,
    UNDER_MAP,
    mode_ui_state,
    parse_qty,
    parse_threshold,
)


def test_parse_qty() -> None:
    assert parse_qty(" 10 ") == 10
    assert parse_qty("") == 0        # 빈칸/오타는 0 — 코어 검증에서 거부됨
    assert parse_qty("abc") == 0


def test_parse_threshold() -> None:
    assert parse_threshold("0.15") == 0.15
    assert parse_threshold("") is None
    assert parse_threshold("x") is None


def test_display_maps_match_domain() -> None:
    # 화면 표기 → 코어 enum 값이 도메인과 일치해야 명령이 통한다
    assert {Underlying(v) for v in UNDER_MAP.values()} == set(Underlying)
    assert all(Instrument(v) for v in COUNTER_MAP.values())


def test_manual_mode() -> None:
    ui = mode_ui_state("수동")
    assert ui.order_buttons_visible          # 주문 버튼 표시
    assert not ui.threshold_enabled          # 기준값 에디트 disable
    assert not ui.start_visible              # 시작 체크박스 숨김


def test_auto_modes() -> None:
    for mode in ("자동T", "자동M"):
        ui = mode_ui_state(mode)
        assert not ui.order_buttons_visible
        assert ui.threshold_enabled
        assert ui.start_visible
