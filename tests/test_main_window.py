"""메인 화면 실행 명령 구성 테스트 — 개발/배포판(exe) 분기."""
import sys
from typing import Any

import pytest

from kp_arb.main_window import _restart_step, launch_command


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
    cmd = launch_command("kp_arb.order_panel", ("autoT",))
    assert cmd[-1] == "autoT"


def test_launch_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\dist\meme\meme.exe")
    assert launch_command("kp_arb.core_server", ())[0].endswith("meme-core.exe")
    assert launch_command("kp_arb.core_server", ())[-1] == "core"
    assert launch_command("kp_arb.monitor", ())[-1] == "monitor"
    assert launch_command("kp_arb.order_panel", ("autoM",))[-1] == "autoM"
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
