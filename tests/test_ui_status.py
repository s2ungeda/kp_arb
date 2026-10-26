"""상태줄 콤보(ui_status) 순수 부분 — 이력 쌓기·글자색 스타일. NXT 개장·체결쏴 공용."""
from kp_arb.ui_status import HISTORY_MAX, push_history, strip_stamp, style_for


def test_status_history_dedupes_and_caps() -> None:
    # 사용자 2026-10-23: 상태줄 콤보 이력 — 같은 글 연속은 안 쌓고, 50건까지만
    h: list[str] = []
    assert push_history(h, "대기") and not push_history(h, "대기")
    assert push_history(h, "창 열림") and h == ["창 열림", "대기"]
    for i in range(60):
        push_history(h, f"글 {i}")
    assert len(h) == HISTORY_MAX == 50 and h[0] == "글 59" and "대기" not in h
    # 시각 붙이기(사용자 2026-10-23 "상태바 로그에 시간 추가") — 같은 글 판정은 시각을 뺀 글로
    t: list[str] = []
    assert push_history(t, "접수 #1", stamp="09:00:01") and t == ["09:00:01 접수 #1"]
    assert not push_history(t, "접수 #1", stamp="09:00:02")  # 같은 글, 시각만 달라도 안 쌓음
    assert push_history(t, "체결 #1", stamp="09:00:03") and t[0] == "09:00:03 체결 #1"
    assert strip_stamp("09:00:03 체결 #1") == "체결 #1" and strip_stamp("체결 #1") == "체결 #1"


def test_style_for_color_maps_known_colors_only() -> None:
    # 체결쏴 LS 처리 멈춤 알림(빨강)·재개(초록)는 콤보 스타일로, 모르는 색은 기본
    assert style_for("red") == "Stall.TCombobox" and style_for("dark green") == "Resume.TCombobox"
    assert style_for("black") == "TCombobox" == style_for("blue")


def test_stall_banner_text_and_ls_stalled_judgement() -> None:
    # 사용자 2026-10-26: 지연 띠 글은 "LS 처리 지연", 판정은 LS 채널이 연결된 채 stalled일 때만
    from kp_arb.ui_status import STALL_BANNER_TEXT, ls_stalled

    assert STALL_BANNER_TEXT.startswith("LS 처리 지연")
    assert ls_stalled([{"venue": "LS", "connected": True, "stalled": True}])
    assert not ls_stalled([{"venue": "LS", "connected": False, "stalled": True}])  # 끊김은 딴 길
    assert not ls_stalled([{"venue": "HL", "connected": True, "stalled": True}])
    assert not ls_stalled([])
