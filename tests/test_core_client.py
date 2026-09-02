"""화면 폴링 헬퍼(core_client) — 실패해도 마지막 데이터 유지.

2026-08-31 실증 보강: 조회 1회 실패로 주문리스트가 통째로 비어 보이던 결함의 재발 방지.
"""
from kp_arb.core_client import merge_poll, stale_seconds


def test_success_updates_box() -> None:
    box: dict = {}
    assert merge_poll(box, {"a": 1}, None, 100.0) is None
    assert box["data"] == {"a": 1}
    assert box["ok_ts"] == 100.0
    assert box["fails"] == 0


def test_failure_keeps_last_data() -> None:
    # 핵심 — 실패해도 마지막 데이터를 지우지 않는다(빈 화면 방지).
    box: dict = {}
    merge_poll(box, {"a": 1}, None, 100.0)
    msg = merge_poll(box, None, "URLError: timeout", 100.5)
    assert box["data"] == {"a": 1}
    assert box["fails"] == 1
    assert msg is not None and "URLError" in msg


def test_failure_logs_first_and_every_60th() -> None:
    # 스팸 방지 — 첫 실패와 매 60회째만 로그 메시지.
    box: dict = {}
    msgs = [merge_poll(box, None, "x", float(i)) for i in range(1, 121)]
    hits = [i for i, m in enumerate(msgs, 1) if m is not None]
    assert hits == [1, 60, 120]


def test_recovery_message_once() -> None:
    box: dict = {}
    merge_poll(box, None, "x", 1.0)
    msg = merge_poll(box, {"a": 2}, None, 2.0)
    assert msg is not None and "복구" in msg
    assert merge_poll(box, {"a": 3}, None, 3.0) is None  # 정상 지속 — 조용


def test_stale_seconds() -> None:
    box: dict = {}
    assert stale_seconds(box, 10.0) is None  # 성공한 적 없음
    merge_poll(box, {"a": 1}, None, 10.0)
    assert stale_seconds(box, 13.5) == 3.5
