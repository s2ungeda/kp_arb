"""HLWebSocketClient 계약 테스트 — 가짜 커넥터(라이브 없음), 공식 WS 스키마 기반."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from kp_arb.domain.enums import Underlying
from kp_arb.gateways.hl import Mark
from kp_arb.gateways.hl_ws import HLWebSocketClient
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


async def test_bbo_merges_recent_l2book_depth() -> None:
    # 1호가는 bbo(빠름) 값으로, 2호가 아래는 최근 l2Book 것으로 붙는다.
    client = HLWebSocketClient(FakeConnector([l2book_frame(), bbo_frame()]))
    quotes = []
    client.on_quote.append(quotes.append)

    await client.run()

    q = quotes[-1]
    assert q.bid == 183.55 and q.ask == 183.65             # bbo 최신값
    assert q.bids is not None and q.bids[0] == (183.55, 7.0)
    assert q.bids[1] == (183.4, 20.0)                      # l2Book 하위 단계
    assert q.asks is not None and q.asks[1] == (183.7, 10.0)


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
    # 머지 구독 중엔 원시 1호가(bbo)를 머지 사다리에 섞지 않는다 (단위가 다름).
    client = HLWebSocketClient(connector=None)  # type: ignore[arg-type]
    coin = client._symbols[Underlying.SK_HYNIX]
    client._l2_extra[coin] = {"nSigFigs": 5, "mantissa": 5}
    client._parse_l2book({"coin": coin, "time": 1, "levels": [
        [{"px": "184.1", "sz": "5"}], [{"px": "184.15", "sz": "7"}]]})

    quote = client._parse_bbo({"coin": coin, "time": 2, "bbo": [
        {"px": "184.12", "sz": "1"}, {"px": "184.13", "sz": "2"}]})
    assert quote is not None
    assert quote.bid == 184.12                 # 1호가 표시는 bbo 원시
    assert quote.bids == [(184.1, 5.0)]        # 사다리는 머지 그대로 (스플라이스 없음)
    assert quote.asks == [(184.15, 7.0)]
