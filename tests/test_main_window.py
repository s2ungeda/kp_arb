"""메인 화면 실행 명령 구성 테스트 — 개발/배포판(exe) 분기."""
import sys

import pytest

from kp_arb.main_window import launch_command


def test_launch_command_dev() -> None:
    cmd = launch_command("kp_arb.monitor", ())
    assert cmd[0] == sys.executable and cmd[1:] == ["-m", "kp_arb.monitor"]
    cmd = launch_command("kp_arb.order_panel", ("autoT",))
    assert cmd[-1] == "autoT"


def test_launch_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\dist\kp-arb\kp-arb.exe")
    assert launch_command("kp_arb.core_server", ())[0].endswith("kp-arb-core.exe")
    assert launch_command("kp_arb.core_server", ())[-1] == "core"
    assert launch_command("kp_arb.monitor", ())[-1] == "monitor"
    assert launch_command("kp_arb.order_panel", ("autoM",))[-1] == "autoM"
    assert launch_command("kp_arb.main_window", ())[0].endswith("kp-arb.exe")


def test_launch_command_fx_monitor_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\dist\kp-arb\kp-arb.exe")
    cmd = launch_command("kp_arb.fx_monitor", ())
    assert cmd[0].endswith("kp-arb.exe") and cmd[-1] == "fx_monitor"


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
