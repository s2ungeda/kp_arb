"""WS 세션 현황 상태 객체 테스트 (Phase 8-3a — 순수 로직)."""
from __future__ import annotations

from kp_arb.ws_status import WsStatus, order_block_reason


def _quote() -> WsStatus:
    return WsStatus(venue="LS", name="LS 시세", kind="시세", expects_stream=True)


def _order() -> WsStatus:
    return WsStatus(venue="HL", name="HL 주문", kind="주문", expects_stream=False)


def test_on_message_counts_and_stamps() -> None:
    s = _quote()
    assert s.rx_count == 0 and s.last_rx is None
    s.on_message(10.0)
    s.on_message(11.0)
    assert s.rx_count == 2
    assert s.last_rx == 11.0


def test_connect_disconnect_toggle_and_count() -> None:
    s = _quote()
    s.on_connect()
    assert s.connected and s.connects == 1
    s.on_disconnect()
    assert not s.connected and s.disconnects == 1
    s.on_connect()
    assert s.connected and s.connects == 2


def test_stale_when_disconnected() -> None:
    s = _quote()  # 연결 전
    assert s.is_stale(now=100.0, max_idle_s=5.0) is True


def test_quote_stale_by_idle() -> None:
    s = _quote()
    s.on_connect()
    assert s.is_stale(now=100.0, max_idle_s=5.0) is True  # 연결됐지만 아직 무데이터
    s.on_message(now=100.0)
    assert s.is_stale(now=104.0, max_idle_s=5.0) is False  # 4초 전 → 신선
    assert s.is_stale(now=106.0, max_idle_s=5.0) is True   # 6초 전 → 지연


def test_order_feed_never_stale_by_idle() -> None:
    s = _order()
    s.on_connect()
    # 주문 피드는 체결 없어도(무데이터) 연결만 되어 있으면 정상
    assert s.is_stale(now=10_000.0, max_idle_s=5.0) is False


def test_block_reason_no_channels() -> None:
    assert order_block_reason([], now=1.0, max_idle_s=5.0) == "WS 미접속"


def test_block_reason_disconnected_channel() -> None:
    up = _quote()
    up.on_connect()
    up.on_message(10.0)
    down = _order()  # 연결 안 됨
    reason = order_block_reason([up, down], now=11.0, max_idle_s=5.0)
    assert reason is not None and "끊김" in reason and "HL 주문" in reason


def test_block_reason_stale_quote() -> None:
    q = _quote()
    q.on_connect()
    q.on_message(10.0)
    reason = order_block_reason([q], now=20.0, max_idle_s=5.0)  # 10초 무데이터
    assert reason is not None and "지연" in reason


def test_block_reason_ok_when_all_healthy() -> None:
    q = _quote()
    q.on_connect()
    q.on_message(10.0)
    o = _order()
    o.on_connect()  # 주문 피드는 무데이터라도 연결이면 정상
    assert order_block_reason([q, o], now=12.0, max_idle_s=5.0) is None


def test_to_dict_shape() -> None:
    s = _quote()
    s.on_connect()
    s.on_message(3.0)
    d = s.to_dict()
    assert d == {
        "venue": "LS", "name": "LS 시세", "kind": "시세",
        "connected": True, "rx_count": 1, "disconnects": 0, "last_rx": 3.0,
    }
