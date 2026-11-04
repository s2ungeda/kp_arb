"""코어 메모리 계측(임시 진단, 2026-11-03 운영 사고 — 32비트 코어가 7시간 만에 MemoryError).

10분마다 코어 로그에 한 줄: 프로세스 커밋·작업집합(OS가 본 전체) · 파이썬 할당 블록 수(파이썬이
아는 몫) · 스레드 수 · 장부 주문 수(열린 수) · 당일 체결/취소/거부 행 수 · 로그 큐 밀림 · asyncio
태스크 수 · xing 요청·실시간 수. 커밋은 느는데 파이썬 블록이 안 늘면 파이썬 바깥(xing DLL·pywin32)
이고, 어느 카운터와 비례하는지도 같은 줄에서 보인다. 환경변수 `KP_MEM_TRACE=1`이면 tracemalloc을 켜
30분마다 파이썬 할당 상위 10줄도 남긴다(메모리·CPU를 더 쓰므로 평소엔 끔).

원인이 잡히면 이 모듈과 호출(core_server `mem_probe_loop`)을 지운다 — DESIGN-ls-xing §7 [OPEN].
"""
from __future__ import annotations

import gc
import os
import sys
import threading
import tracemalloc
from collections.abc import Mapping, Sequence
from typing import Any

PROBE_INTERVAL_S = 600.0     # 한 줄 남기는 주기
TRACE_EVERY = 3              # tracemalloc 상위 목록은 몇 번째 계측마다(3 = 30분)
TRACE_TOP = 10
TRACE_ENV = "KP_MEM_TRACE"
_COM_WRAPPER_TYPES = frozenset({"EventsProxy", "COMEventClass"})


def process_memory() -> tuple[float, float] | None:
    """(작업집합 MB, 커밋 MB) — 윈도우만(psapi). 못 읽으면 None."""
    if sys.platform != "win32":
        return None
    import ctypes

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        p = _PMC()
        p.cb = ctypes.sizeof(_PMC)
        if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(p), p.cb):
            return None
        return p.WorkingSetSize / 1e6, p.PagefileUsage / 1e6
    except (AttributeError, OSError):
        return None


def trace_enabled() -> bool:
    return os.environ.get(TRACE_ENV, "").strip() not in ("", "0", "false", "no")


def _len(obj: Any, *path: str) -> int | None:
    """obj.path[0].path[1]… 의 길이(없으면 None)."""
    cur = obj
    for name in path:
        cur = getattr(cur, name, None)
        if cur is None:
            return None
    try:
        return len(cur)
    except TypeError:
        return None


def collect(system: Any, *, log_backlog: int | None = None,
            tasks: int | None = None) -> dict[str, Any]:
    """계측값 한 벌(dict). system은 LiveSystem(또는 비슷한 꼴) — 없는 항목은 None."""
    mem = process_memory()
    ob = getattr(system, "order_book", None)
    open_n: int | None = None
    if ob is not None and hasattr(ob, "open_orders"):
        try:
            open_n = len(ob.open_orders())
        except Exception:  # noqa: BLE001 - 계측이 코어를 흔들지 않게
            open_n = None
    gw = getattr(system, "_gw", None)
    sess = getattr(gw, "_session", None)
    objs = gc.get_objects()
    # 살아 있는 pywin32 이벤트 래퍼(DispatchWithEvents 결과) 수 — 수만이면 XAQuery 해제 자체가
    # 안 되는 것, 수십이면 해제는 되는데 DLL이 안 돌려주는 것(검토 2026-11-03 §4)
    com = sum(1 for o in objs if type(o).__name__ in _COM_WRAPPER_TYPES)
    return {
        "ws_mb": mem[0] if mem else None,
        "commit_mb": mem[1] if mem else None,
        "py_blocks": sys.getallocatedblocks(),
        "gc_objects": len(objs),
        "com_proxies": com,
        "threads": threading.active_count(),
        "tasks": tasks,
        "orders": _len(ob, "_orders"),
        "open_orders": open_n,
        "fills": _len(system, "fills"),
        "cancels": _len(system, "cancels"),
        "rejects": _len(system, "rejects"),
        "log_backlog": log_backlog,
        "xing_requests": getattr(sess, "requests", None),
        "xing_real": getattr(sess, "_real_sent", None),
    }


def format_line(d: Mapping[str, Any]) -> str:
    """로그 한 줄 — 없는 값은 '-'."""

    def f(key: str, fmt: str = "{}") -> str:
        v = d.get(key)
        return "-" if v is None else fmt.format(v)

    return (f"[계측] 메모리 커밋 {f('commit_mb', '{:.0f}')}MB 작업집합 {f('ws_mb', '{:.0f}')}MB | "
            f"파이썬 블록 {f('py_blocks', '{:,}')} 객체 {f('gc_objects', '{:,}')} "
            f"COM래퍼 {f('com_proxies')} | "
            f"스레드 {f('threads')} 태스크 {f('tasks')} | "
            f"장부 {f('orders', '{:,}')}(열림 {f('open_orders')}) "
            f"체결/취소/거부 {f('fills', '{:,}')}/{f('cancels', '{:,}')}/{f('rejects', '{:,}')} | "
            f"로그큐 {f('log_backlog')} | "
            f"xing 요청 {f('xing_requests', '{:,}')} 실시간 {f('xing_real', '{:,}')}")


def start_trace() -> None:
    if not tracemalloc.is_tracing():
        tracemalloc.start(5)


def top_allocations(n: int = TRACE_TOP) -> Sequence[str]:
    """tracemalloc 상위 n줄(파일:줄 크기) — 추적 중이 아니면 빈 목록."""
    if not tracemalloc.is_tracing():
        return ()
    snap = tracemalloc.take_snapshot()
    out: list[str] = []
    for st in snap.statistics("lineno")[:n]:
        fr = st.traceback[0]
        name = fr.filename.replace("\\", "/").split("/")[-1]
        out.append(f"{st.size / 1e6:.1f}MB {name}:{fr.lineno} ({st.count:,}개)")
    return out
