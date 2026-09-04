"""자동주문 화면 공통 — 창 닫기(X) 규칙 (DESIGN-ui.md §6, 사용자 확정 2026-09-04).

자동주문(동시호가·자동T·자동M…)은 코어가 돌리고 창은 보기·조작만 하므로, 창을 닫아도 실행은
남는다(운영 실측 2026-09-04). 그래서 자동주문 화면은 모두 같은 규칙으로 닫는다:
  실행 중 아님 → 그냥 닫기 / 실행 중 → 확인창 → '예'면 정지 명령을 보내고 실행이 풀린 것을
  확인한 뒤 닫기(최대 timeout_s) / '아니오'면 창 유지.
새 자동주문 화면은 ``attach_auto_close``를 붙이기만 하면 된다.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def close_plan(running: bool, confirmed: bool | None) -> str:
    """닫기 판정 — 순수 로직. "close" | "stop_and_close" | "stay".

    실행 중 아니면 그냥 닫는다. 실행 중이면 확인창 답이 '예'일 때만 정지하고 닫고, 아니면
    그대로 둔다. confirmed는 실행 중일 때만 본다(None = 묻지 않음/답 없음 → 유지).
    """
    if not running:
        return "close"
    return "stop_and_close" if confirmed else "stay"


def attach_auto_close(
    root: Any, *, title: str, is_running: Callable[[], bool], send_stop: Callable[[], None],
    set_status: Callable[[str], None] | None = None, timeout_s: float = 3.0,
    clock: Callable[[], float] | None = None,
) -> Callable[[], None]:
    """tk 창에 공통 닫기 규칙을 붙인다. 돌려주는 on_close는 테스트·메뉴에서 직접 부를 수 있다.

    정지 뒤 바로 destroy하지 않고 코어 상태(is_running)가 풀린 것을 보고 닫는다 — 전송 스레드가
    창과 함께 죽어 정지 명령이 유실되지 않게. timeout_s가 지나면 그냥 닫는다.
    """
    import time
    import tkinter as tk

    now = clock or time.time
    deadline = {"t": 0.0}

    def _close_when_stopped() -> None:
        if not is_running() or now() > deadline["t"]:
            try:
                root.destroy()
            except tk.TclError:
                pass
            return
        try:
            root.after(100, _close_when_stopped)
        except tk.TclError:
            pass

    def on_close() -> None:
        from tkinter import messagebox

        running = bool(is_running())
        confirmed: bool | None = None
        if running:
            confirmed = bool(messagebox.askyesno(
                title, "자동주문이 실행 중입니다. 자동주문을 정지하고 화면을 닫을까요?",
                parent=root))
        plan = close_plan(running, confirmed)
        if plan == "stay":
            return
        if plan == "stop_and_close":
            send_stop()
            if set_status is not None:
                set_status("정지 명령 전송 — 확인 뒤 닫습니다")
            deadline["t"] = now() + timeout_s
            _close_when_stopped()
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    return on_close
