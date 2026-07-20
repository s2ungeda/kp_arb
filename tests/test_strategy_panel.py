"""전략 화면 모드별 위젯 상태 테스트 (DESIGN §6.2-1)."""
from kp_arb.strategy_panel import mode_ui_state


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
