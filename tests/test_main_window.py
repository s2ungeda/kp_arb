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
