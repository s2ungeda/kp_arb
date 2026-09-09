"""공통 팝업 — 부모 창 중앙 배치 계산(순수 함수)."""
from kp_arb.ui_dialog import centered_geometry


def test_centered_geometry_on_parent() -> None:
    # 부모 창 (100,200) 크기 800x600에 400x100 팝업 → 부모 중앙. 화면 밖(음수)은 0으로.
    assert centered_geometry(400, 100, 100, 200, 800, 600) == "+300+450"
    assert centered_geometry(1000, 100, 10, 10, 800, 600) == "+0+260"


def test_centered_geometry_clamps_into_parent_monitor() -> None:
    # 실측 2026-09-09: 멀티모니터에서 음수 좌표를 0으로 밀어 팝업이 보이는 범위 밖 → 모달 잠금.
    # 부모가 왼쪽 모니터(-1920~0)에 있으면 그 안에 놓는다(음수 좌표 그대로).
    left = (-1920, 0, 0, 1040)
    assert centered_geometry(400, 100, -1500, 200, 800, 600, left) == "+-1300+450"
    # 팝업이 모니터 오른쪽·아래로 넘치면 안으로 밀어 넣는다
    assert centered_geometry(400, 100, -300, 900, 800, 600, left) == "+-400+940"
    # 팝업이 모니터보다 크면 왼쪽·위 모서리에 맞춘다
    assert centered_geometry(3000, 100, -1500, 200, 800, 600, left) == "+-1920+450"
    # 오른쪽 모니터(1920~3840): 그대로 중앙
    assert centered_geometry(400, 100, 2500, 300, 800, 600, (1920, 0, 3840, 1040)) == "+2700+550"
