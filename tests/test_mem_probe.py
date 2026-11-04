"""코어 메모리 계측(임시 진단, 2026-11-03) — 순수 부분."""
from __future__ import annotations

import sys
from collections import deque

from kp_arb.mem_probe import collect, format_line, process_memory, top_allocations


class _Session:
    requests = 1234
    _real_sent = 56789


class _Gw:
    _session = _Session()


class _Book:
    def __init__(self) -> None:
        self._orders = {("a", "1"): object(), ("a", "2"): object(), ("a", "3"): object()}

    def open_orders(self) -> list[object]:
        return [object()]


class _System:
    order_book = _Book()
    _gw = _Gw()
    fills = deque([{"seq": 1}, {"seq": 2}])
    cancels: deque[dict[str, int]] = deque()
    rejects = deque([{"seq": 3}])


def test_collect_reads_counters_from_system_shape() -> None:
    d = collect(_System(), log_backlog=7, tasks=11)
    assert d["orders"] == 3 and d["open_orders"] == 1
    assert (d["fills"], d["cancels"], d["rejects"]) == (2, 0, 1)
    assert d["xing_requests"] == 1234 and d["xing_real"] == 56789
    assert d["log_backlog"] == 7 and d["tasks"] == 11
    assert d["py_blocks"] > 0 and d["gc_objects"] > 0 and d["threads"] >= 1
    assert isinstance(d["com_proxies"], int) and "COM래퍼" in format_line(d)  # §4 두 번째 칸


def test_collect_tolerates_missing_pieces_and_format_shows_dash() -> None:
    d = collect(object())
    assert d["orders"] is None and d["xing_requests"] is None and d["fills"] is None
    line = format_line(d)
    assert line.startswith("[계측] 메모리 커밋") and "장부 -(열림 -)" in line
    assert "xing 요청 - 실시간 -" in line
    assert "체결/취소/거부 2/0/1" in format_line(collect(_System()))


def test_process_memory_on_windows_is_positive() -> None:
    mem = process_memory()
    if sys.platform == "win32":
        assert mem is not None and mem[0] > 0 and mem[1] > 0
    else:
        assert mem is None


def test_top_allocations_empty_when_not_tracing() -> None:
    assert list(top_allocations()) == []
