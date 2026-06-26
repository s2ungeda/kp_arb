"""LS WS 클라이언트 계약 테스트. 라이브 없음(가짜 WS 서버 + 녹화 프레임)."""
import json
from collections.abc import AsyncIterator

import pytest

from kp_arb.domain.enums import Underlying
from kp_arb.domain.models import Quote
from kp_arb.gateways.ls_ws import Fill, LSWebSocketClient, MarketStatus

SAMSUNG_CODE = Underlying.SAMSUNG.krx_code


def quote_frame(code: str = SAMSUNG_CODE, *, bid: float = 69_900, ask: float = 70_000) -> str:
    return json.dumps(
        {"header": {"tr_cd": "H1_", "tr_key": code}, "body": {"bid": bid, "ask": ask, "ts": 1.0}}
    )


def fill_frame() -> str:
    return json.dumps(
        {
            "header": {"tr_cd": "SC1"},
            "body": {"fill_id": "F1", "order_id": "0000001", "qty": 10, "price": 70_000, "ts": 2.0},
        }
    )


def status_frame(code: str = SAMSUNG_CODE) -> str:
    return json.dumps(
        {"header": {"tr_cd": "JIF", "tr_key": code}, "body": {"jang_cd": "20"}}
    )


class FakeConnection:
    """녹화 프레임을 재생하는 가짜 WS 세션. fail_after 프레임 후 끊김(ConnectionError)."""

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


class FakeConnector:
    def __init__(self, sessions: list[FakeConnection]) -> None:
        self.sessions = sessions
        self.connects = 0

    async def connect(self) -> FakeConnection:
        conn = self.sessions[self.connects]
        self.connects += 1
        return conn


async def test_replay_emits_all_event_types() -> None:
    session = FakeConnection([quote_frame(), fill_frame(), status_frame()])
    client = LSWebSocketClient(FakeConnector([session]))
    quotes: list[Quote] = []
    fills: list[Fill] = []
    statuses: list[MarketStatus] = []
    client.on_quote.append(quotes.append)
    client.on_fill.append(fills.append)
    client.on_market_status.append(statuses.append)
    client.subscribe_quotes(Underlying.SAMSUNG)
    client.subscribe_fills()
    client.subscribe_market_status(Underlying.SAMSUNG)

    await client.run()

    assert len(quotes) == 1
    assert quotes[0].underlying is Underlying.SAMSUNG
    assert quotes[0].bid == 69_900 and quotes[0].ask == 70_000
    assert len(fills) == 1 and fills[0].order_id == "0000001"
    assert len(statuses) == 1 and statuses[0].tr_key == SAMSUNG_CODE


async def test_subscribe_sends_register_for_all_trs() -> None:
    session = FakeConnection([])
    client = LSWebSocketClient(FakeConnector([session]))
    client.subscribe_quotes(Underlying.SAMSUNG)
    client.subscribe_fills()
    client.subscribe_market_status(Underlying.SAMSUNG)

    await client.run()

    sent_trs = {json.loads(m)["body"]["tr_cd"] for m in session.sent}
    assert {"H1_", "NH1", "JIF"} <= sent_trs
    assert {"SC0", "SC1", "SC2", "SC3", "SC4"} <= sent_trs


async def test_reconnect_resubscribes_and_recovers() -> None:
    # 1세션: 프레임1 방출 후 끊김. 2세션: 프레임1 정상.
    s1 = FakeConnection([quote_frame(bid=1), quote_frame(bid=2)], fail_after=1)
    s2 = FakeConnection([quote_frame(bid=3)])
    connector = FakeConnector([s1, s2])
    client = LSWebSocketClient(connector)
    quotes: list[Quote] = []
    client.on_quote.append(quotes.append)
    client.subscribe_quotes(Underlying.SAMSUNG)

    await client.run()

    assert connector.connects == 2  # 재연결됨
    assert [q.bid for q in quotes] == [1, 3]  # 끊기기 전 1개 + 복구 후 1개
    assert s2.sent  # 재구독됨


async def test_reconnect_exhausted_raises() -> None:
    s1 = FakeConnection([quote_frame(), quote_frame()], fail_after=1)
    s2 = FakeConnection([quote_frame(), quote_frame()], fail_after=1)
    connector = FakeConnector([s1, s2])
    client = LSWebSocketClient(connector, max_reconnects=1)
    client.subscribe_quotes(Underlying.SAMSUNG)

    with pytest.raises(ConnectionError):
        await client.run()
    assert connector.connects == 2


async def test_unknown_tr_is_ignored() -> None:
    frame = json.dumps({"header": {"tr_cd": "XXX"}, "body": {}})
    session = FakeConnection([frame])
    client = LSWebSocketClient(FakeConnector([session]))
    quotes: list[Quote] = []
    client.on_quote.append(quotes.append)

    await client.run()  # 예외 없이 통과
    assert quotes == []
