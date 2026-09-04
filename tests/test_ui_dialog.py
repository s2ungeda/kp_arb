"""공통 팝업 — 부모 창 중앙 배치 계산(순수 함수)."""
from kp_arb.ui_dialog import centered_geometry


def test_centered_geometry_on_parent() -> None:
    # 부모 창 (100,200) 크기 800x600에 400x100 팝업 → 부모 중앙. 화면 밖(음수)은 0으로.
    assert centered_geometry(400, 100, 100, 200, 800, 600) == "+300+450"
    assert centered_geometry(1000, 100, 10, 10, 800, 600) == "+0+260"
