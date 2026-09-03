"""HLWebSocketClient 계약 테스트 — 가짜 커넥터(라이브 없음), 공식 WS 스키마 기반."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from kp_arb.domain.enums import Underlying
from kp_arb.gateways.hl import Mark
from kp_arb.gateways.hl_ws import HLWebSocketClient, OrderUpdate
from kp_arb.gateways.ls_ws import Fill

ADDR = "0x" + "a" * 40


class FakeConnection:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def _gen(self) -> AsyncIterator[str]:
        for frame in self.frames:
            yield frame

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen()


class FakeConnector:
    def __init__(self, frames: list[str]) -> None:
        self.conn = FakeConnection(frames)

    async def connect(self) -> FakeConnection:
        return self.conn


def mark_frame(coin: str = "xyz:SMSN", mark: str = "184.1") -> str:
    return json.dumps({"channel": "activeAssetCtx",
                       "data": {"coin": coin, "ctx": {"markPx": mark}}})


def fills_frame(*, snapshot: bool = False) -> str:
    data = {"user": ADDR, "fills": [
        {"coin": "xyz:SMSN", "px": "183.87", "sz": "0.1", "side": "B",
         "oid": 485489797671, "tid": 111, "time": 1751400000000, "fee": "0.008"},
        {"coin": "xyz:NVDA", "px": "1.0", "sz": "1", "side": "B",
         "oid": 999, "tid": 112, "time": 1751400000000},  # 대상 외
    ]}
    if snapshot:
        data["isSnapshot"] = True
    return json.dumps({"channel": "userFills", "data": data})


def test_l2_aggregation_reports_active_merge() -> None:
    # 코어가 스냅샷에 실어 보낼 '현재 적용 머지' — set 후 그대로 읽혀야(단일 진실).
    client = HLWebSocketClient(FakeConnector([]))
    client.subscribe_l2book()  # l2Book 구독돼야 머지 설정이 붙는다
    assert client.l2_aggregation(Underlying.SAMSUNG) == (None, None)  # 원시(미설정)
    client.set_l2_aggregation(Underlying.SAMSUNG, 5, 2)
    assert client.l2_aggregation(Underlying.SAMSUNG) == (5, 2)
    assert client.l2_aggregation(Underlying.SK_HYNIX) == (None, None)  # 종목별 독립
    client.set_l2_aggregation(Underlying.SAMSUNG, None, None)  # 원시로 복귀
    assert client.l2_aggregation(Underlying.SAMSUNG) == (None, None)


async def test_subscribes_marks_and_fills() -> None:
    connector = FakeConnector([])
    client = HLWebSocketClient(connector)
    client.subscribe_marks()
    client.subscribe_user_fills(ADDR)
    await client.run()

    subs = [json.loads(m)["subscription"] for m in connector.conn.sent]
    coins = {s["coin"] for s in subs if s["type"] == "activeAssetCtx"}
    assert coins == {"xyz:SMSN", "xyz:SKHX", "xyz:HYUNDAI"}
    assert {"type": "userFills", "user": ADDR} in subs


def order_update_frame(status: str = "canceled", coin: str = "xyz:SMSN",
                        oid: int = 485489797671) -> str:
    return json.dumps({"channel": "orderUpdates", "data": [
        {"order": {"coin": coin, "side": "B", "limitPx": "183.87", "sz": "0.086",
                   "oid": oid, "timestamp": 1751400000000, "origSz": "0.14"},
         "status": status, "statusTimestamp": 1751400000001},
        {"order": {"coin": "xyz:NVDA", "side": "B", "limitPx": "1.0", "sz": "1",
                   "oid": 999, "timestamp": 1751400000000, "origSz": "1"},
         "status": "canceled", "statusTimestamp": 1751400000001},  # 대상 외 코인
    ]})


async def test_subscribe_order_updates_registers() -> None:
    connector = FakeConnector([])
    client = HLWebSocketClient(connector)
    client.subscribe_order_updates(ADDR)
    await client.run()
    subs = [json.loads(m)["subscription"] for m in connector.conn.sent]
    assert {"type": "orderUpdates", "user": ADDR} in subs


async def test_order_update_parsed_and_filtered() -> None:
    client = HLWebSocketClient(FakeConnector([order_update_frame("canceled")]))
    got: list[OrderUpdate] = []
    client.on_order_update.append(got.append)
    await client.run()
    assert len(got) == 1  # 대상 외(xyz:NVDA) 제외
    u = got[0]
    assert u.oid == "485489797671" and u.coin == "xyz:SMSN"
    assert u.status == "canceled" and u.sz == 0.086 and u.orig_sz == 0.14
    assert u.is_terminal_cancel is True and u.is_rejected is False


def test_terminal_cancel_covers_status_families() -> None:
    def upd(status: str) -> OrderUpdate:
        return OrderUpdate(oid="1", coin="xyz:SMSN", status=status, side="B",
                           sz=0.0, orig_sz=0.1, limit_px=None)
    # 취소·거부 계열 = 종료(제거 대상)
    for s in ("canceled", "marginCanceled", "reduceOnlyCanceled",
              "selfTradeCanceled", "rejected", "tickRejected"):
        assert upd(s).is_terminal_cancel is True, s
    # 살아있음/체결은 제거 대상 아님(체결은 userFills가 담당)
    for s in ("open", "triggered", "filled"):
        assert upd(s).is_terminal_cancel is False, s
    assert upd("tickRejected").is_rejected is True
    assert upd("canceled").is_rejected is False


async def test_mark_parsed() -> None:
    client = HLWebSocketClient(FakeConnector([mark_frame()]))
    marks: list[Mark] = []
    client.on_mark.append(marks.append)
    await client.run()
    assert len(marks) == 1
    assert marks[0].underlying is Underlying.SAMSUNG and marks[0].price == 184.1


async def test_user_fill_parsed_and_filtered() -> None:
    client = HLWebSocketClient(FakeConnector([fills_frame()]))
    fills: list[Fill] = []
    client.on_fill.append(fills.append)
    await client.run()
    assert len(fills) == 1  # 대상 외 코인 제외
    f = fills[0]
    assert f.order_id == "485489797671" and f.qty == 0.1
    assert f.price == 183.87 and f.fee == 0.008


async def test_public_trades_parsed_as_ticks() -> None:
    # 공개 체결(trades): data가 리스트 — 현재가(TradeTick)로 해석.
    frame = json.dumps({"channel": "trades", "data": [
        {"coin": "xyz:SKHX", "side": "B", "px": "1434.5", "sz": "0.2",
         "time": 1751500000000, "tid": 9},
        {"coin": "xyz:NVDA", "px": "1.0", "sz": "1", "time": 1, "tid": 10},  # 대상 외
    ]})
    client = HLWebSocketClient(FakeConnector([frame]))
    ticks = []
    client.on_trade.append(ticks.append)
    await client.run()
    assert len(ticks) == 1
    assert ticks[0].underlying is Underlying.SK_HYNIX
    assert ticks[0].price == 1434.5 and ticks[0].market == "hl"


async def test_snapshot_fills_skipped() -> None:
    client = HLWebSocketClient(FakeConnector([fills_frame(snapshot=True)]))
    fills: list[Fill] = []
    client.on_fill.append(fills.append)
    await client.run()
    assert fills == []  # 과거 체결 일괄은 이벤트 아님


async def test_subscription_ack_ignored() -> None:
    ack = json.dumps({"channel": "subscriptionResponse",
                      "data": {"method": "subscribe"}})
    client = HLWebSocketClient(FakeConnector([ack]))
    await client.run()  # 예외 없이 통과


class _FailingConn:
    """fail_after 프레임 후 끊기는 세션(재연결 테스트용)."""

    def __init__(self, frames: list[str], *, fail_after: int | None = None) -> None:
        self.frames = frames
        self.fail_after = fail_after
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def _gen(self) -> AsyncIterator[str]:
        for i, frame in enumerate(self.frames):
            if self.fail_after is not None and i >= self.fail_after:
                raise ConnectionError("dropped")
            yield frame

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen()


class _MultiConnector:
    def __init__(self, sessions: list[_FailingConn]) -> None:
        self.sessions = sessions
        self.n = 0

    async def connect(self) -> _FailingConn:
        conn = self.sessions[self.n]
        self.n += 1
        return conn


async def test_on_reconnect_fires_only_after_reconnect() -> None:
    # Phase 8-4 — 최초 연결엔 안 울리고, 재연결(재구독 완료) 시 1회.
    s1 = _FailingConn([mark_frame(), mark_frame()], fail_after=1)
    s2 = _FailingConn([mark_frame()])
    client = HLWebSocketClient(_MultiConnector([s1, s2]))
    fired: list[int] = []
    client.on_reconnect.append(lambda: fired.append(1))
    await client.run()
    assert fired == [1]


async def test_ws_status_tracks_rx_and_connection() -> None:
    # WS 세션 현황(Phase 8-3) — 연결·수신카운트·끊김 추적.
    client = HLWebSocketClient(FakeConnector([mark_frame(), mark_frame()]),
                               clock=lambda: 42.0)
    assert not client.status.connected and client.status.rx_count == 0
    await client.run()
    assert client.status.rx_count == 2       # 2 프레임 수신
    assert client.status.connects == 1
    assert client.status.last_rx == 42.0
    assert not client.status.connected       # 스트림 정상 종료 = 끊김
    assert client.status.disconnects == 1


def l2book_frame(coin: str = "xyz:SKHX", *, levels_per_side: int = 3) -> str:
    # l2Book 프레임: levels=[[매수단계...],[매도단계...]] — 각 {px, sz, n}.
    bids = [{"px": f"{183.5 - i * 0.1:.1f}", "sz": str((i + 1) * 10), "n": 1}
            for i in range(levels_per_side)]
    asks = [{"px": f"{183.6 + i * 0.1:.1f}", "sz": str((i + 1) * 5), "n": 1}
            for i in range(levels_per_side)]
    return json.dumps({
        "channel": "l2Book",
        "data": {"coin": coin, "time": 1000, "levels": [bids, asks]},
    })


def bbo_frame(coin: str = "xyz:SKHX", *, bid: str = "183.55", ask: str = "183.65") -> str:
    return json.dumps({
        "channel": "bbo",
        "data": {"coin": coin, "time": 1001,
                 "bbo": [{"px": bid, "sz": "7"}, {"px": ask, "sz": "8"}]},
    })


async def test_l2book_quote_carries_depth() -> None:
    client = HLWebSocketClient(FakeConnector([l2book_frame()]))
    quotes = []
    client.on_quote.append(quotes.append)

    await client.run()

    q = quotes[0]
    assert q.underlying is Underlying.SK_HYNIX
    assert q.bid == 183.5 and q.ask == 183.6
    assert q.bids is not None and q.bids[0] == (183.5, 10.0) and len(q.bids) == 3
    assert q.asks is not None and q.asks[2] == (183.8, 15.0)


async def test_l2book_depth_max_20() -> None:
    # 서버 최대인 한쪽당 20단계까지 전부 보관 (est-pr·머지 표시용).
    client = HLWebSocketClient(FakeConnector([l2book_frame(levels_per_side=25)]))
    quotes = []
    client.on_quote.append(quotes.append)

    await client.run()

    assert quotes[0].bids is not None and len(quotes[0].bids) == 20
    assert quotes[0].asks is not None and len(quotes[0].asks) == 20


async def test_bbo_scalar_fast_ladder_from_l2book() -> None:
    # 스칼라 bid/ask는 bbo(빠름), 호가창은 정합적 l2Book 그대로(대칭·크로스 없음).
    client = HLWebSocketClient(FakeConnector([l2book_frame(), bbo_frame()]))
    quotes = []
    client.on_quote.append(quotes.append)

    await client.run()

    q = quotes[-1]
    assert q.bid == 183.55 and q.ask == 183.65             # bbo 최신 스칼라(빠름)
    assert q.bids is not None and q.asks is not None
    assert q.bids[0] == (183.5, 10.0) and len(q.bids) == 3   # l2Book 그대로(대칭)
    assert q.asks[0] == (183.6, 5.0) and len(q.asks) == 3
    assert max(b[0] for b in q.bids) < min(a[0] for a in q.asks)  # 크로스 없음


async def test_bbo_without_l2book_has_no_depth() -> None:
    client = HLWebSocketClient(FakeConnector([bbo_frame()]))
    quotes = []
    client.on_quote.append(quotes.append)

    await client.run()

    assert quotes[0].bids is None and quotes[0].asks is None


async def test_bad_frame_does_not_kill_stream() -> None:
    # 예상 밖 프레임(필드 누락 등)으로 파싱이 실패해도 스트림은 계속 — 채널 사망 방지.
    bad_fill = json.dumps({"channel": "userFills",
                           "data": {"fills": [{"coin": "xyz:SKHX"}]}})  # oid 없음
    good = json.dumps({"channel": "bbo",
                       "data": {"coin": "xyz:SKHX", "time": 1,
                                "bbo": [{"px": "1500.0", "sz": "1"},
                                        {"px": "1500.5", "sz": "1"}]}})
    client = HLWebSocketClient(FakeConnector([bad_fill, good]))
    quotes = []
    client.on_quote.append(quotes.append)

    await client.run()  # 예외 없이 끝까지

    assert len(quotes) == 1 and quotes[0].bid == 1500.0  # 뒤 프레임은 정상 처리


def test_l2_aggregation_resubscribe() -> None:
    # 머지 변경 = 구독 취소 + 재구독 (사용자 확정). 희망 상태도 갱신(재연결 대비).
    client = HLWebSocketClient(connector=None)  # type: ignore[arg-type]
    client.subscribe_l2book()
    coin = client._symbols[Underlying.SK_HYNIX]

    client.set_l2_aggregation(Underlying.SK_HYNIX, 5, 5)
    target = next(s for s in client._subs
                  if s.get("type") == "l2Book" and s.get("coin") == coin)
    assert target["nSigFigs"] == 5 and target["mantissa"] == 5
    first, second = list(client._control)
    assert first["method"] == "unsubscribe"
    assert "nSigFigs" not in first["subscription"]  # 옛 구독 그대로 취소
    assert second["method"] == "subscribe"
    assert second["subscription"]["mantissa"] == 5

    client.set_l2_aggregation(Underlying.SK_HYNIX, None)  # 원시 복귀
    assert "nSigFigs" not in target and "mantissa" not in target


def test_bbo_keeps_merged_ladder_intact() -> None:
    # 머지 구독 중엔 원시 1호가(bbo)를 머지 호가창에 섞지 않는다 (단위가 다름).
    client = HLWebSocketClient(connector=None)  # type: ignore[arg-type]
    coin = client._symbols[Underlying.SK_HYNIX]
    client._l2_extra[coin] = {"nSigFigs": 5, "mantissa": 5}
    client._parse_l2book({"coin": coin, "time": 1, "levels": [
        [{"px": "184.1", "sz": "5"}], [{"px": "184.15", "sz": "7"}]]})

    quote = client._parse_bbo({"coin": coin, "time": 2, "bbo": [
        {"px": "184.12", "sz": "1"}, {"px": "184.13", "sz": "2"}]})
    assert quote is not None
    assert quote.bid == 184.12                 # 1호가 표시는 bbo 원시
    assert quote.bids == [(184.1, 5.0)]        # 호가창은 머지 그대로 (스플라이스 없음)
    assert quote.asks == [(184.15, 7.0)]


def _snapshot_frame(fills: list[dict]) -> str:  # type: ignore[type-arg]
    return json.dumps({"channel": "userFills",
                       "data": {"isSnapshot": True, "user": ADDR, "fills": fills}})


def _fill(tid: int, time_ms: int, oid: int = 485489797671) -> dict:  # type: ignore[type-arg]
    return {"coin": "xyz:SMSN", "px": "183.87", "sz": "0.1", "side": "B",
            "oid": oid, "tid": tid, "time": time_ms, "fee": "0.008"}


async def test_startup_snapshot_skipped() -> None:
    # 시동 시 첫 스냅샷 — 시동 전 체결이라 이벤트로 흘리지 않는다(장부는 REST 기준).
    import time as _t

    client = HLWebSocketClient(FakeConnector([
        _snapshot_frame([_fill(1, int(_t.time() * 1000))])]))
    got: list[Fill] = []
    client.on_fill.append(got.append)
    await client.run()
    assert got == []


async def test_reconnect_snapshot_recovers_only_gap_fills() -> None:
    # 재접속 스냅샷: 끊김 이전 체결·이미 처리한 tid는 건너뛰고, 끊긴 사이 신규 체결만 흘린다.
    import time as _t

    now_ms = int(_t.time() * 1000)
    live = json.dumps({"channel": "userFills", "data": {"user": ADDR,
                                                         "fills": [_fill(100, now_ms)]}})
    s1 = _FailingConn([live, mark_frame()], fail_after=1)  # tid=100 처리 후 끊김(오류 경로)
    s2 = _FailingConn([_snapshot_frame([
        _fill(50, now_ms - 60_000),   # 끊김 1분 전 — 건너뜀
        _fill(100, now_ms),           # 이미 처리 — 중복 건너뜀
        _fill(200, now_ms + 500),     # 끊긴 사이 신규 — 처리
    ])])
    client = HLWebSocketClient(_MultiConnector([s1, s2]))
    got: list[Fill] = []
    client.on_fill.append(got.append)
    await client.run()
    assert [f.fill_id for f in got] == ["100", "200"]


async def test_reconnect_hook_fires_after_snapshot() -> None:
    # 재동기 훅(REST)은 스냅샷 처리 **뒤**에 — 순서가 바뀌면 스냅샷 체결이 이중 반영된다.
    import time as _t

    now_ms = int(_t.time() * 1000)
    s1 = _FailingConn([mark_frame(), mark_frame()], fail_after=1)
    s2 = _FailingConn([_snapshot_frame([_fill(300, now_ms + 100)])])
    client = HLWebSocketClient(_MultiConnector([s1, s2]))
    order: list[str] = []
    client.on_fill.append(lambda f: order.append("fill"))
    client.on_reconnect.append(lambda: order.append("hook"))
    await client.run()
    assert order == ["fill", "hook"]


def test_ping_interval_default_20s() -> None:
    # HL 유지용 핑 주기 — 20초(60초 무통신 규정 대비 여유, 사용자 지정 2026-09-02).
    import inspect

    sig = inspect.signature(HLWebSocketClient._ping_loop)
    assert sig.parameters["interval_s"].default == 20.0
