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
    from kp_arb import ui_close

    monkeypatch.setattr(ui_close, "ask_yes_no", lambda *a, **k: True)  # 확인창 '예'
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
    from kp_arb import ui_close

    # '아니오' → 유지
    monkeypatch.setattr(ui_close, "ask_yes_no", lambda *a, **k: False)
    root = _FakeRoot()
    on_close = attach_auto_close(root, title="t", is_running=lambda: True,
                                 send_stop=lambda: None)
    on_close()
    assert not root.destroyed

    # '예'지만 코어가 안 풀림 → 3초 지나면 그냥 닫는다
    monkeypatch.setattr(ui_close, "ask_yes_no", lambda *a, **k: True)
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


def test_attach_auto_close_before_close_and_forced(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 결정 82(2026-10-16): before_close는 닫기로 정해진 뒤 destroy 전에 한 번. 메인 종료 중
    # (skip_confirm)이면 실행 중이어도 묻지 않고(확인창 금지) 정지 명령 없이 저장만 하고 닫는다.
    from kp_arb import ui_close

    def _no_ask(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("메인 종료 중엔 확인창을 띄우면 안 된다")

    monkeypatch.setattr(ui_close, "ask_yes_no", _no_ask)
    root = _FakeRoot()
    saved: list[int] = []
    stops: list[int] = []
    on_close = attach_auto_close(
        root, title="t", is_running=lambda: True, send_stop=lambda: stops.append(1),
        before_close=lambda: saved.append(1), skip_confirm=lambda: True)
    on_close()
    assert saved == [1] and stops == [] and root.destroyed

    # 실행 중 아님 + 메인 종료 아님: 저장하고 그냥 닫기. 저장이 터져도 닫힌다.
    root2 = _FakeRoot()

    def _boom() -> None:
        raise RuntimeError("저장 실패")

    on_close2 = attach_auto_close(root2, title="t", is_running=lambda: False,
                                  send_stop=lambda: None, before_close=_boom,
                                  skip_confirm=lambda: False)
    on_close2()
    assert root2.destroyed


def test_main_closing_flag(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # 플래그 파일: 세우면 True, 치우면 False, 너무 오래됐으면(메인이 죽어 못 치움) False.
    from kp_arb.ui_close import (
        MAIN_CLOSING_MAX_AGE_S,
        clear_main_closing,
        main_closing,
        mark_main_closing,
    )

    p = tmp_path / ".win_state" / "main_closing"
    assert main_closing(p) is False
    mark_main_closing(p)
    assert main_closing(p) is True
    assert main_closing(p, now=p.stat().st_mtime + MAIN_CLOSING_MAX_AGE_S + 1) is False
    clear_main_closing(p)
    assert main_closing(p) is False
    clear_main_closing(p)  # 없어도 조용히
