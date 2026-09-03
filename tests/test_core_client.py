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


def test_share_is_fresh_rule() -> None:
    from kp_arb.core_client import share_is_fresh

    assert share_is_fresh(10_000, 12_000, stale_s=3.0)       # 2초 전 — 신선
    assert not share_is_fresh(10_000, 13_500, stale_s=3.0)   # 3.5초 전 — 낡음


def test_state_feed_reads_share_and_skips_unchanged(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 공유메모리가 있고 신선하면 그것을 읽고(버전 바뀔 때만 파싱), HTTP는 부르지 않는다.
    import json
    import time

    from kp_arb import core_client
    from kp_arb.core_client import run_state_feed
    from kp_arb.state_share import SHARE_PATH_ENV, ShareWriter

    path = str(tmp_path / "share.bin")
    monkeypatch.setenv(SHARE_PATH_ENV, path)
    calls = {"http": 0}

    def fake_http(*_a, **_k):  # type: ignore[no-untyped-def]
        calls["http"] += 1
        return None, "x"

    monkeypatch.setattr(core_client, "core_request_err", fake_http)
    w = ShareWriter(path)
    try:
        now_ms = int(time.time() * 1000)
        w.write(json.dumps({"open_orders": [1]}).encode(), now_ms)
        box: dict = {}
        run_state_feed(box, log_tag="t", interval_s=0.0, max_ticks=3)
        assert box["data"] == {"open_orders": [1]}
        assert box["fails"] == 0 and calls["http"] == 0
        w.touch(now_ms + 50)  # 하트비트 — 데이터 유지, ok_ts만 갱신
        run_state_feed(box, log_tag="t", interval_s=0.0, max_ticks=1)
        assert box["ok_ts"] == (now_ms + 50) / 1000.0
    finally:
        w.close()


def test_state_feed_falls_back_to_http_when_share_missing(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 파일이 없으면(메인 안 뜸) HTTP 조회로 폴백 — 결과는 merge_poll 형태로 담긴다.
    from kp_arb import core_client
    from kp_arb.core_client import run_state_feed
    from kp_arb.state_share import SHARE_PATH_ENV

    monkeypatch.setenv(SHARE_PATH_ENV, str(tmp_path / "none.bin"))
    monkeypatch.setattr(core_client, "core_request_err",
                        lambda *_a, **_k: ({"open_orders": []}, None))
    box: dict = {}
    run_state_feed(box, log_tag="t", interval_s=0.0, max_ticks=1)
    assert box["data"] == {"open_orders": []} and box["fails"] == 0


def test_state_feed_falls_back_when_share_stale(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 파일은 있지만 수신시각이 낡았으면(메인 죽음) HTTP로 폴백.
    from kp_arb import core_client
    from kp_arb.core_client import run_state_feed
    from kp_arb.state_share import SHARE_PATH_ENV, ShareWriter

    path = str(tmp_path / "share.bin")
    monkeypatch.setenv(SHARE_PATH_ENV, path)
    monkeypatch.setattr(core_client, "core_request_err",
                        lambda *_a, **_k: ({"from": "http"}, None))
    w = ShareWriter(path)
    try:
        w.write(b'{"from":"share"}', 1_000)  # 아주 옛날 시각
        box: dict = {}
        run_state_feed(box, log_tag="t", interval_s=0.0, max_ticks=1)
        assert box["data"] == {"from": "http"}
    finally:
        w.close()


def test_stale_seconds() -> None:
    box: dict = {}
    assert stale_seconds(box, 10.0) is None  # 성공한 적 없음
    merge_poll(box, {"a": 1}, None, 10.0)
    assert stale_seconds(box, 13.5) == 3.5
