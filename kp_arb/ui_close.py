"""자동주문 화면 공통 — 창 닫기(X) 규칙 (DESIGN-ui.md §6, 사용자 확정 2026-09-04).

자동주문(동시호가·자동T·자동M…)은 코어가 돌리고 창은 보기·조작만 하므로, 창을 닫아도 실행은
남는다(운영 실측 2026-09-04). 그래서 자동주문 화면은 모두 같은 규칙으로 닫는다:
  실행 중 아님 → 그냥 닫기 / 실행 중 → 확인창 → '예'면 정지 명령을 보내고 실행이 풀린 것을
  확인한 뒤 닫기(최대 timeout_s) / '아니오'면 창 유지.
새 자동주문 화면은 ``attach_auto_close``를 붙이기만 하면 된다.

메인 종료(결정 82, 사용자 2026-10-16): 메인은 코어를 내리기 전에 화면들에 닫기(WM_CLOSE)를 보내고,
그동안 ``main_closing`` 플래그 파일(있으면 true, 없으면 false — 프로세스가 달라 변수를 못 나눈다)을
둔다. 플래그 파일이 있으면 화면은 확인창 없이 ``before_close``(자동M: 세트 행 값을 코어에 저장)만
하고 닫는다 — 실행은 코어가 메인 종료로 함께 멈춘다.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .ui_dialog import ask_yes_no

MAIN_CLOSING_MAX_AGE_S = 30.0  # 플래그 파일이 이보다 오래됐으면(메인이 죽어 못 지움) 무시


def main_closing_path() -> Path:
    """메인 종료 중 플래그 파일 — 창 저장 폴더(.win_state) 안."""
    from .logs import base_dir

    return base_dir() / ".win_state" / "main_closing"


def mark_main_closing(path: Path | None = None) -> None:
    """메인이 종료를 시작할 때 세운다(화면들에 닫기를 보내기 전)."""
    p = path or main_closing_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        logging.getLogger("kp_arb.ui").warning("메인 종료 플래그 파일을 못 만듦: %s", p)


def clear_main_closing(path: Path | None = None) -> None:
    """메인 시작 때·종료 끝에 치운다(묵은 플래그 파일이 다음 날 창 닫기를 조용히 만들지 않게)."""
    p = path or main_closing_path()
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def main_closing(path: Path | None = None, now: float | None = None) -> bool:
    """지금 메인이 종료 중인가 — 플래그 파일이 있고 MAIN_CLOSING_MAX_AGE_S 안에 세운 것일 때만."""
    p = path or main_closing_path()
    try:
        age = (now if now is not None else time.time()) - p.stat().st_mtime
    except OSError:
        return False
    return 0 <= age <= MAIN_CLOSING_MAX_AGE_S


def close_message(what: str = "") -> str:
    """닫기 확인창 글 — what("삼성 진입·청산")이 있으면 앞에 붙인다. (순수 함수)"""
    head = f"{what} 자동주문이 실행 중입니다." if what else "자동주문이 실행 중입니다."
    return f"{head} 자동주문을 정지하고 화면을 닫을까요?"


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
    before_close: Callable[[], None] | None = None,
    skip_confirm: Callable[[], bool] | None = None,
    describe_running: Callable[[], str] | None = None,
) -> Callable[[], None]:
    """tk 창에 공통 닫기 규칙을 붙인다. 돌려주는 on_close는 테스트·메뉴에서 직접 부를 수 있다.

    정지 뒤 바로 destroy하지 않고 코어 상태(is_running)가 풀린 것을 보고 닫는다 — 전송 스레드가
    창과 함께 죽어 정지 명령이 유실되지 않게. timeout_s가 지나면 그냥 닫는다.
    before_close: 닫기로 정해진 뒤(정지 명령·destroy 전) 한 번 부른다 — 자동M은 세트 행 값을
    코어에 저장(결정 82). skip_confirm: True면 실행 중이어도 묻지 않고 정지 명령 없이 닫는다
    (메인 종료 중 — 실행은 코어가 메인 종료로 멈춘다). describe_running: 확인창에 무엇이 도는지
    적어 줄 글("삼성 진입·청산") — 보고 있던 종목이 아닌 다른 종목이 켜져 있을 때 헷갈리지 않게
    (NXT 개장 매매, 사용자 2026-10-22).
    """
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
        running = bool(is_running())
        forced = bool(skip_confirm()) if skip_confirm is not None else False
        confirmed: bool | None = None
        if running and not forced:
            what = describe_running() if describe_running is not None else ""
            confirmed = ask_yes_no(  # 모니터 중앙 팝업(ui_dialog)
                root, title, close_message(what))
        plan = "close" if forced else close_plan(running, confirmed)
        if plan == "stay":
            return
        if before_close is not None:
            try:
                before_close()
            except Exception:  # noqa: BLE001 - 저장 실패가 닫기를 막으면 안 된다(로그만)
                logging.getLogger("kp_arb.ui").exception("%s 닫기 전 저장 실패 — 그냥 닫음", title)
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
