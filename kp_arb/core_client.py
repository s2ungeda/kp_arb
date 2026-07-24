"""코어 API 클라이언트 공용 — 화면(메인/전략/모니터)들이 공유 (DESIGN §12).

코어는 localhost에서만 듣는다. 실패(미접속 등)는 None — 화면은 표시만 한다.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

CORE_URL = "http://127.0.0.1:8787"
PARENT_PID_ENV = "KP_PARENT_PID"  # 메인이 자식 창에 자기 PID를 넘겨 고아 방지


def _pid_alive(pid: int) -> bool:
    """프로세스 생존 확인 (윈도우/기타). 죽었으면 False."""
    if sys.platform == "win32":
        import ctypes

        query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(query, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == still_active
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch_parent_exit() -> None:
    """부모(메인) 프로세스가 죽으면 이 창도 종료 — 고아 창 방지 (강제 종료·크래시 대비).

    메인이 KP_PARENT_PID로 자기 PID를 넘긴 경우에만 동작. 뒷단 스레드에서 감시하다
    부모가 사라지면 프로세스를 즉시 끝낸다(뷰어 창이라 정리할 상태 없음).
    """
    raw = os.environ.get(PARENT_PID_ENV)
    if not raw:
        return
    try:
        pid = int(raw)
    except ValueError:
        return

    def _watch() -> None:
        while True:
            time.sleep(2.0)
            if not _pid_alive(pid):
                os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()


def core_request(path: str, payload: dict[str, Any] | None = None,
                 timeout: float = 1.0) -> dict[str, Any] | None:
    """GET(payload 없음)/POST(payload=JSON) 요청. 실패는 None."""
    url = f"{CORE_URL}{path}"
    try:
        if payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - 클라이언트 헬퍼: 어떤 실패든 None(폴링 스레드 보호)
        return None
