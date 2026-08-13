"""공용 스타일 토큰 순수 로직 테스트 — sign_color 부호 판정."""
from kp_arb.ui_theme import C_BUY, C_SELL, C_ZERO, sign_color


def test_sign_color_by_sign() -> None:
    assert sign_color(1) == C_BUY        # 양수(매수/이익) = 빨강
    assert sign_color(0.0001) == C_BUY
    assert sign_color(-0.5) == C_SELL    # 음수(매도/손실) = 파랑
    assert sign_color(0) == C_ZERO       # 0 = 검정


def test_sign_color_non_numeric_is_zero() -> None:
    assert sign_color(None) == C_ZERO
    assert sign_color("-") == C_ZERO     # 미수신 표시('-') 등 숫자 아님 → 검정
