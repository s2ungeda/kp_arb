"""메인 화면 실행 명령 구성 테스트 — 개발/배포판(exe) 분기."""
import sys
from typing import Any

import pytest

from kp_arb.main_window import (
    _restart_step,
    launch_command,
    layout_choices,
    screens_to_save,
)


def test_layout_choices_lists_generations_with_names() -> None:
    # ui_state 세대 → (세대, "시각  화면이름들", 토큰) — 깨진 JSON은 건너뛰고 빈 목록은 "(없음)".
    gens = [
        (1, 0.0, '{"core": true, "screens": ["kp_arb.monitor", "kp_arb.order_autom"]}'),
        (2, 0.0, "not json"),
        (3, 0.0, '{"screens": []}'),
        (4, 0.0, '{"screens": ["kp_arb.order_hl 1", "evil.module"]}'),
    ]
    out = layout_choices(gens)
    assert [n for n, _l, _s in out] == [1, 3, 4]
    assert out[0][2] == ["kp_arb.monitor", "kp_arb.order_autom"]
    assert "시세 모니터, 체결쏴" in out[0][1]
    assert "(없음)" in out[1][1]
    assert out[2][2] == ["kp_arb.order_hl 1"]  # kp_arb. 밖 모듈은 버림
    assert "HL 일반주문" in out[2][1]


def test_retired_hl_first_screen_is_not_restored() -> None:
    # 사용자 2026-10-01(exec 결정 62): HL선 시험 중지 — 메인 메뉴에서 뺐고(모듈은 남김), 지난 화면
    # 구성에 남아 있어도 시동 복원·"화면 구성 되돌리기"에서 다시 띄우지 않는다.
    from kp_arb.main_window import restorable_screens

    saved = {"screens": ["kp_arb.order_autom", "kp_arb.order_autom_hl_first",
                         "kp_arb.order_autom_hl_first 2", "kp_arb.order_hl 1", 7, "evil.module"]}
    assert restorable_screens(saved) == ["kp_arb.order_autom", "kp_arb.order_hl 1"]
    assert restorable_screens({}) == []
    gens = [(1, 0.0, '{"screens": ["kp_arb.order_autom_hl_first", "kp_arb.monitor"]}')]
    out = layout_choices(gens)
    assert out[0][2] == ["kp_arb.monitor"] and "HL선" not in out[0][1]


def test_screens_to_save_keeps_saved_list_until_restored() -> None:
    # 복원 전(코어 시동 대기·복원 포기)엔 저장 목록 보존 — 2초 주기 저장이 빈 목록으로
    # 덮어써 이전 화면들이 날아가던 문제(실측 2026-09-07).
    saved = ["kp_arb.monitor", "kp_arb.order_autom"]
    assert screens_to_save([], restore_done=False, saved=saved) == saved
    assert screens_to_save(["kp_arb.order_hl"], restore_done=False, saved=saved) == saved
    # 복원 뒤(또는 사용자가 직접 창을 연 뒤)부터는 실제 열린 창이 진실 — 빈 목록도 그대로
    now = ["kp_arb.order_hl"]
    assert screens_to_save(now, restore_done=True, saved=saved) == now
    assert screens_to_save([], restore_done=True, saved=saved) == []


def _fresh(**over: Any) -> dict[str, Any]:
    st: dict[str, Any] = {"intentional": False, "down": 0, "cooldown": 0,
                          "fails": 0, "gave_up": False}
    st.update(over)
    return st


def _step(st: dict[str, Any], alive: bool) -> str:
    return _restart_step(st, alive, after=3, cooldown=6, max_restarts=5)


def test_restart_alive_resets_counters() -> None:
    st = _fresh(down=2, fails=3, gave_up=True)
    assert _step(st, alive=True) == "none"
    assert st["down"] == 0 and st["fails"] == 0 and st["gave_up"] is False


def test_restart_after_consecutive_downs() -> None:
    st = _fresh()
    assert _step(st, alive=False) == "none"  # down 1
    assert _step(st, alive=False) == "none"  # down 2
    assert _step(st, alive=False) == "restart"  # down 3 → 재기동
    assert st["cooldown"] == 6 and st["fails"] == 1 and st["down"] == 0


def test_restart_cooldown_blocks_counting() -> None:
    st = _fresh(cooldown=2)
    assert _step(st, alive=False) == "none" and st["cooldown"] == 1
    assert _step(st, alive=False) == "none" and st["cooldown"] == 0
    assert st["down"] == 0  # 유예 동안엔 세지 않음


def test_restart_skipped_when_intentional() -> None:
    st = _fresh(intentional=True)
    for _ in range(10):
        assert _step(st, alive=False) == "none"  # 안전종료 — 되살리지 않음
    assert st["down"] == 0


def test_restart_gives_up_after_max() -> None:
    st = _fresh()
    actions = []
    for _ in range(100):  # 계속 죽어 있는 상태
        actions.append(_step(st, alive=False))
    assert actions.count("restart") == 5  # MAX_RESTARTS만큼만
    assert "give_up" in actions
    assert st["gave_up"] is True
    # 포기 후엔 더 이상 재기동 시도 없음
    assert _step(st, alive=False) == "none"
    # 다시 살아나면 초기화
    assert _step(st, alive=True) == "none" and st["gave_up"] is False


def test_launch_command_dev() -> None:
    cmd = launch_command("kp_arb.monitor", ())
    assert cmd[0] == sys.executable and cmd[1:] == ["-m", "kp_arb.monitor"]
    cmd = launch_command("kp_arb.order_autom", ())
    assert cmd[1:] == ["-m", "kp_arb.order_autom"]


def test_launch_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\dist\meme\meme.exe")
    assert launch_command("kp_arb.core_server", ())[0].endswith("meme-core.exe")
    assert launch_command("kp_arb.core_server", ())[-1] == "core"
    assert launch_command("kp_arb.monitor", ())[-1] == "monitor"
    assert launch_command("kp_arb.order_autom", ())[-1] == "autoM"
    assert launch_command("kp_arb.order_autom_stock", ())[-1] == "autoMS"  # 주식 화면(09-16)
    assert launch_command("kp_arb.order_autom_hl_first", ())[-1] == "autoMH"  # HL선 시험(09-22)
    assert launch_command("kp_arb.main_window", ())[0].endswith("meme.exe")


def test_launch_command_fx_monitor_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\dist\meme\meme.exe")
    cmd = launch_command("kp_arb.fx_monitor", ())
    assert cmd[0].endswith("meme.exe") and cmd[-1] == "fx_monitor"


def test_watch_parent_exit_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # KP_PARENT_PID 없으면 아무것도 안 함(감시 스레드 미시작)
    from kp_arb.core_client import watch_parent_exit
    monkeypatch.delenv("KP_PARENT_PID", raising=False)
    watch_parent_exit()  # 예외 없이 즉시 반환


def test_pid_alive_self() -> None:
    import os

    from kp_arb.core_client import _pid_alive
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(999_999_99) is False  # 존재하지 않는 PID


def test_auto_running_detects_running_set(monkeypatch: pytest.MonkeyPatch) -> None:
    import kp_arb.main_window as mw
    state = {"screens": {"autoM": {"entry_sets": [{"running": True}],
                                    "exit_sets": [{"running": False}]}}}
    monkeypatch.setattr(mw, "core_request", lambda *a, **k: state)
    assert mw._auto_running() is True
    state["screens"]["autoM"]["entry_sets"][0]["running"] = False
    assert mw._auto_running() is False
    monkeypatch.setattr(mw, "core_request", lambda *a, **k: None)
    assert mw._auto_running() is False  # 미접속이면 False


def test_alarm_skip_reason() -> None:
    # 자동매매 중지(체결차·타임아웃·환율 없음)·3회 거부 → 코어 에러 카운터 → 메인 에러 알람.
    # 재생/건너뜀/실패를 화면 로그에 남기기 위한 판정(사용자 2026-09-16: 소리 났는지 확인 불가였음).
    from kp_arb.main_window import alarm_skip_reason

    assert alarm_skip_reason({}) == "꺼짐"
    assert alarm_skip_reason({"enabled": False, "path": "a.wav"}) == "꺼짐"
    assert alarm_skip_reason({"enabled": True, "path": " "}) == "wav 경로 없음"
    assert alarm_skip_reason({"enabled": True, "path": "C:/snd/error.wav"}) is None


def test_ls_disconnect_events_only_ls_connected_to_disconnected() -> None:
    # DESIGN-settings §2a(사용자 2026-10-14): LS(xing) 채널이 연결→끊김으로 바뀔 때만, 같은 끊김에
    # 한 번.
    # 첫 관측·HL 채널·끊김→연결은 알리지 않는다.
    from kp_arb.main_window import ls_disconnect_events

    prev: dict[str, bool] = {}
    ls_up = {"venue": "LS", "name": "LS xing", "connected": True}
    hl_up = {"venue": "HL", "name": "HL", "connected": True}
    assert ls_disconnect_events(prev, [ls_up, hl_up]) == []                     # 첫 관측 = 기준
    ls_dn = dict(ls_up, connected=False)
    hl_dn = dict(hl_up, connected=False)
    assert ls_disconnect_events(prev, [ls_dn, hl_dn]) == ["LS xing"]            # LS만
    assert ls_disconnect_events(prev, [ls_dn, hl_dn]) == []                     # 같은 끊김은 한 번
    assert ls_disconnect_events(prev, [ls_up, hl_up]) == []                     # 다시 연결
    assert ls_disconnect_events(prev, [ls_dn]) == ["LS xing"]                   # 또 끊기면 또


def test_core_down_event_once_per_transition() -> None:
    from kp_arb.main_window import core_down_event

    box: dict[str, object] = {}
    assert core_down_event(box, False) is False   # 시동 직후 미접속은 기준만(알림 없음)
    assert core_down_event(box, True) is False
    assert core_down_event(box, False) is True    # 있음 → 없음
    assert core_down_event(box, False) is False   # 계속 없음은 다시 안 알림
    assert core_down_event(box, True) is False
    assert core_down_event(box, False) is True


def test_close_screens_gracefully_waits_then_forces() -> None:
    # 결정 82(2026-10-16): 메인 종료 때 플래그 파일을 세운 뒤 체결쏴 화면들이 세트 값을 코어에
    # 저장하고 스스로 닫힐 때까지 기다린다. 닫힌 화면은 두고, wait_s 안에 안 닫힌 화면만 종전처럼
    # 강제 종료.
    from kp_arb.main_window import close_screens_gracefully

    class _Proc:
        def __init__(self, pid: int, closes: bool) -> None:
            self.pid, self.closes, self.rc, self.terminated = pid, closes, None, False

        def poll(self) -> int | None:
            return self.rc

        def terminate(self) -> None:
            self.terminated, self.rc = True, -1

    clock = {"t": 0.0}
    procs = [_Proc(11, True), _Proc(12, False), _Proc(13, True)]
    procs[2].rc = 0  # 이미 닫힌 창 — 센 대상 아님

    def _sleep(s: float) -> None:
        clock["t"] += s
        for p in procs:  # 플래그 파일을 본 착한 창은 다음 틱에 저장하고 닫힌다
            if p.closes and p.rc is None:
                p.rc = 0

    text = close_screens_gracefully(procs, wait_s=1.0, sleep=_sleep, now=lambda: clock["t"])
    assert procs[0].rc == 0 and not procs[0].terminated          # 저장하고 스스로 닫힘
    assert procs[1].terminated                                    # 안 닫혀 강제 종료
    assert not procs[2].terminated                                # 이미 닫힌 창은 건드리지 않음
    assert clock["t"] >= 1.0                                      # 고집쟁이 때문에 끝까지 기다림
    assert text == "저장 후 닫힘 1/2, 강제 종료 1"
