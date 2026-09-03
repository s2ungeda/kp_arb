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
CORE_WS_URL = "ws://127.0.0.1:8787/ws"  # 실시간 채널(DESIGN §12.1) — 메인창만 붙는다
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


def core_request_err(
    path: str, payload: dict[str, Any] | None = None, timeout: float = 1.0,
) -> tuple[dict[str, Any] | None, str | None]:
    """``core_request``와 같되 실패 사유 문자열도 돌려준다 — 화면 실패 로그용."""
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
            if isinstance(data, dict):
                return data, None
            return None, "응답이 JSON 객체가 아님"
    except Exception as exc:  # noqa: BLE001 - 클라이언트 헬퍼: 어떤 실패든 (None, 사유)
        return None, f"{type(exc).__name__}: {exc}"


def core_request(path: str, payload: dict[str, Any] | None = None,
                 timeout: float = 1.0) -> dict[str, Any] | None:
    """GET(payload 없음)/POST(payload=JSON) 요청. 실패는 None."""
    data, _err = core_request_err(path, payload, timeout)
    return data


def merge_poll(box: dict[str, Any], data: dict[str, Any] | None,
               err: str | None, now: float) -> str | None:
    """폴링 결과를 상태 박스에 반영 — **실패해도 마지막 데이터를 지우지 않는다.**

    (2026-08-31 주문리스트 실증: 조회 1회 실패로 미체결 목록이 통째로 비어 보임.)
    성공: data 교체 + ok_ts 갱신 + fails=0. 실패: data 유지, fails만 증가.
    로그로 남길 메시지를 돌려준다 — 첫 실패·매 60회째·복구 시 1회(스팸 방지). 순수 로직.
    """
    if data is not None:
        prev_fails = int(box.get("fails", 0) or 0)
        box["data"] = data
        box["ok_ts"] = now
        box["fails"] = 0
        if prev_fails:
            return f"조회 복구 — 연속 실패 {prev_fails}회 뒤 정상"
        return None
    box["fails"] = int(box.get("fails", 0) or 0) + 1
    if box["fails"] == 1 or box["fails"] % 60 == 0:
        return (f"조회 실패 {box['fails']}회째 — {err or '원인 미상'}"
                " (마지막 데이터 유지)")
    return None


def share_is_fresh(share_ts_ms: int, now_ms: float, stale_s: float = 3.0) -> bool:
    """공유메모리 수신시각이 아직 쓸 만한가 — 메인이 죽으면 시각이 멈춰 낡는다. 순수 로직."""
    return (now_ms - share_ts_ms) <= stale_s * 1000.0


def run_state_feed(
    box: dict[str, Any], *, log_tag: str, fallback_path: str = "/manual_state",
    interval_s: float = 0.1, poll_s: float = 0.5, stale_s: float = 3.0,
    max_ticks: int | None = None,
) -> None:
    """화면 뒷단 스레드 몸통(DESIGN §12.1) — **공유메모리 우선, 낡거나 없으면 HTTP 폴링 폴백.**

    0.1초마다 공유 파일의 버전만 확인해 바뀌었을 때만 JSON을 풀어 ``box``에 넣는다
    (``merge_poll`` 형태 그대로 — 표시 코드·지연 표시 무변경). 메인이 안 떠서 파일이 없거나
    수신시각이 ``stale_s``보다 낡으면 기존 0.5초 HTTP 조회로 자동 전환, 회복되면 복귀.
    전환은 화면 로그에 1회씩. ``max_ticks``는 테스트용(None=무한).
    """
    import json

    from .state_share import ShareReader, share_path_from_env

    reader: ShareReader | None = None
    last_version = -1
    last_poll = 0.0
    source = ""  # "share" | "http" — 전환 로그용
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        now = time.time()
        used_share = False
        if reader is None:
            path = share_path_from_env()
            if path:
                try:
                    reader = ShareReader(path)
                except OSError:
                    reader = None  # 메인이 아직 파일을 안 만들었거나 없음 → 폴백
        if reader is not None:
            try:
                got = reader.read()
            except (OSError, ValueError):
                got = None
            if got is not None:
                version, ts_ms, body = got
                if share_is_fresh(ts_ms, now * 1000.0, stale_s):
                    used_share = True
                    if version != last_version:
                        last_version = version
                        try:
                            data = json.loads(body.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            screen_log().warning("%s 공유메모리 JSON 오류 — %s", log_tag, exc)
                            data = None
                        if isinstance(data, dict):
                            merge_poll(box, data, None, ts_ms / 1000.0)
                    else:
                        box["ok_ts"] = ts_ms / 1000.0  # 하트비트 — 데이터 그대로, 신선함만 갱신
        if used_share:
            if source != "share":
                if source:
                    screen_log().info("%s 공유메모리 복귀(실시간)", log_tag)
                source = "share"
        else:
            if source != "http":
                screen_log().warning("%s 공유메모리 없음/낡음 — HTTP 폴링으로 폴백", log_tag)
                source = "http"
            if now - last_poll >= poll_s:
                last_poll = now
                data, err = core_request_err(fallback_path, timeout=2.0)
                msg = merge_poll(box, data, err, now)
                if msg is not None:
                    screen_log().warning("%s %s", log_tag, msg)
        if max_ticks is None or ticks < max_ticks:
            time.sleep(interval_s)


def stale_seconds(box: dict[str, Any], now: float) -> float | None:
    """마지막 성공 조회로부터 지난 초. 성공한 적 없으면 None. 순수 로직."""
    ts = box.get("ok_ts")
    if ts is None:
        return None
    return max(0.0, now - float(ts))


_screen_logger: Any = None


def screen_log() -> Any:
    """화면 프로세스 공용 파일 로그(logs/screen_날짜.log) — 첫 호출 때 1회 부착.

    화면들은 별도 프로세스라 코어 로그에 못 쓴다. 여러 창이 같은 파일에 덧붙이지만
    드문 경고만 남겨 겹침 무해. 파일을 못 만들면 조용히 무시(화면은 계속).
    """
    global _screen_logger
    if _screen_logger is not None:
        return _screen_logger
    import logging
    from datetime import datetime
    from pathlib import Path

    logger = logging.getLogger("kp_arb.screen")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            log_dir = Path("logs")
            log_dir.mkdir(exist_ok=True)
            handler: logging.Handler = logging.FileHandler(
                log_dir / f"screen_{datetime.now():%Y%m%d}.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"))
        except OSError:
            handler = logging.NullHandler()
        logger.addHandler(handler)
    _screen_logger = logger
    return logger
