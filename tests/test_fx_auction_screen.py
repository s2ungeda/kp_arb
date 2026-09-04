"""자동주문 화면 공통 닫기 규칙(ui_close) — 동시호가·자동T·자동M이 같이 쓴다."""
from typing import Any

from kp_arb.ui_close import attach_auto_close, close_plan


def test_close_plan_rules() -> None:
    # 실행 중 아니면 그냥 닫기 / 실행 중이면 '예'일 때만 정지 후 닫기, 아니면 그대로
    # (사용자 확정 2026-09-04).
    assert close_plan(False, None) == "close"
    assert close_plan(False, False) == "close"
    assert close_plan(True, True) == "stop_and_close"
    assert close_plan(True, False) == "stay"
    assert close_plan(True, None) == "stay"


class _FakeRoot:
    """tk 없이 attach_auto_close 흐름만 검증 — after()는 즉시 실행하지 않고 보관한다."""

    def __init__(self) -> None:
        self.destroyed = False
        self.pending: list[Any] = []
        self.protocol_fn: Any = None

    def protocol(self, _name: str, fn: Any) -> None:
        self.protocol_fn = fn

    def after(self, _ms: int, fn: Any) -> None:
        self.pending.append(fn)

    def destroy(self) -> None:
        self.destroyed = True


def test_attach_auto_close_waits_for_core_to_stop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import tkinter.messagebox as mb

    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)  # 확인창 '예'
    root = _FakeRoot()
    state = {"running": True}
    stops: list[int] = []
    clock = {"t": 100.0}
    on_close = attach_auto_close(
        root, title="t", is_running=lambda: state["running"],
        send_stop=lambda: stops.append(1), timeout_s=3.0, clock=lambda: clock["t"])
    assert root.protocol_fn is on_close

    on_close()
    assert stops == [1] and not root.destroyed and len(root.pending) == 1  # 정지 보내고 대기
    state["running"] = False  # 코어가 정지 반영
    root.pending.pop()()
    assert root.destroyed


def test_attach_auto_close_timeout_and_stay(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import tkinter.messagebox as mb

    # '아니오' → 유지
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: False)
    root = _FakeRoot()
    on_close = attach_auto_close(root, title="t", is_running=lambda: True,
                                 send_stop=lambda: None)
    on_close()
    assert not root.destroyed

    # '예'지만 코어가 안 풀림 → 3초 지나면 그냥 닫는다
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    root = _FakeRoot()
    clock = {"t": 100.0}
    on_close = attach_auto_close(root, title="t", is_running=lambda: True,
                                 send_stop=lambda: None, timeout_s=3.0,
                                 clock=lambda: clock["t"])
    on_close()
    clock["t"] = 104.0
    root.pending.pop()()
    assert root.destroyed

    # 실행 중 아님 → 묻지 않고 닫기
    root = _FakeRoot()
    attach_auto_close(root, title="t", is_running=lambda: False, send_stop=lambda: None)()
    assert root.destroyed
