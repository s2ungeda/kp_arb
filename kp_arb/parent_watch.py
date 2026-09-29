"""부모(메인) 프로세스 감시 — 메인이 사라지면 코어가 스스로 안전종료, 그래도 안 끝나면 강제 종료.

운영 사고 2026-09-29 14:55(DESIGN §5 "화면 구조", exec 결정 49): 코어 HTTP가 막혀 메인의 종료
명령이 안 닿아 메인을 닫아도 코어가 살아 자동M이 계속 발주. 세 겹 중 3겹 — 코어 안의 별도
스레드가 메인 pid를 보다가 ① 사라지면 안전종료(걸린 주문 취소)를 걸고 ② grace_s 안에 프로세스가
안 끝나면 hard_exit(os._exit).
(1겹 = 메인의 안전종료→강제 종료, 2겹 = 윈도우 Job Object KILL_ON_JOB_CLOSE.)
순수 로직(is_alive·on_gone·hard_exit·sleep 주입) — 테스트에서 가짜로 돌린다.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

log = logging.getLogger("kp_arb.core")


class ParentWatch:
    def __init__(self, is_alive: Callable[[], bool], on_gone: Callable[[], None],
                 hard_exit: Callable[[], None], *, interval_s: float = 2.0,
                 grace_s: float = 10.0, sleep: Callable[[float], None] = time.sleep,
                 misses: int = 2) -> None:
        self._is_alive = is_alive
        self._on_gone = on_gone
        self._hard_exit = hard_exit
        self._interval = interval_s
        self._grace = grace_s
        self._sleep = sleep
        self._misses = misses  # 연속 n번 '없음'이어야 사라진 것으로(순간 조회 실패 오판 방지)
        self.stopped = False
        self.fired = False

    def run(self) -> None:
        """감시 루프(스레드 본체). 부모가 사라지면 on_gone → grace_s 뒤 hard_exit."""
        miss = 0
        while not self.stopped:
            self._sleep(self._interval)
            if self.stopped:
                return
            if self._is_alive():
                miss = 0
                continue
            miss += 1
            if miss < self._misses:
                continue
            self.fired = True
            log.error("메인(부모) 프로세스 사라짐 — 코어 안전종료 시작, %.0f초 안에 안 끝나면 "
                      "강제 종료", self._grace)
            try:
                self._on_gone()
            except Exception:  # noqa: BLE001 - 안전종료 걸기 실패해도 강제 종료로
                log.exception("안전종료 걸기 실패")
            self._sleep(self._grace)
            if not self.stopped:
                log.error("코어가 %.0f초 안에 안 내려감 — 강제 종료(os._exit)", self._grace)
                self._hard_exit()
            return


def start_parent_watch(on_gone: Callable[[], None], *, grace_s: float = 10.0) -> ParentWatch | None:
    """KP_PARENT_PID(메인이 넘김)가 있으면 감시 스레드 시작. 없으면 None(단독 실행)."""
    from .core_client import PARENT_PID_ENV, _pid_alive

    raw = os.environ.get(PARENT_PID_ENV)
    if not raw or not raw.isdigit():
        return None
    pid = int(raw)
    watch = ParentWatch(lambda: _pid_alive(pid), on_gone, lambda: os._exit(3), grace_s=grace_s)
    threading.Thread(target=watch.run, daemon=True, name="parent-watch").start()
    log.info("메인 프로세스 감시 시작 — pid %d 사라지면 안전종료(%.0f초 뒤 강제)", pid, grace_s)
    return watch
