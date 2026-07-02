"""LS WS 클라이언트 계약 테스트. 라이브 없음(가짜 WS 서버 + 녹화 프레임)."""
import json
from collections.abc import AsyncIterator

import pytest

from kp_arb.domain.enums import Underlying
from kp_arb.domain.models import Quote
from kp_arb.gateways.ls_ws import Fill, LSWebSocketClient, MarketStatus

SAMSUNG_CODE = Underlying.SAMSUNG.krx_code


def quote_frame(code: str = SAMSUNG_CODE, *, bid: float = 69_900, ask: float = 70_000) -> str:
    # 실측 shape: 값은 문자열, 1호가 bidho1/offerho1, hotime(HHMMSS), body에 shcode.
    return json.dumps(
        {
            "header": {"tr_cd": "H1_", "tr_key": code},
            "body": {
                "shcode": code,
                "bidho1": str(bid),
                "offerho1": str(ask),
                "hotime": "085224",
            },
        }
    )


def fill_frame() -> str:
    # 실측 SC1 shape: 값은 문자열, ordno/execno/execqty/execprc/exectime.
    return json.dumps(
        {
            "header": {"tr_cd": "SC1"},
            "body": {"execno": "48086", "ordno": "9852", "execqty": "10",
                     "execprc": "70000", "exectime": "100932000"},
        }
    )


def status_frame(*, jstatus: str = "21") -> str:
    # 실측 shape: JIF는 시장 단위(tr_key "0"), body={jangubun, jstatus}.
    return json.dumps(
        {"header": {"tr_cd": "JIF", "tr_key": "0"}, "body": {"jangubun": "1", "jstatus": jstatus}}
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
    client.subscribe_market_status()

    await client.run()

    assert len(quotes) == 1
    assert quotes[0].underlying is Underlying.SAMSUNG
    assert quotes[0].bid == 69_900 and quotes[0].ask == 70_000
    assert len(fills) == 1 and fills[0].order_id == "9852"
    assert fills[0].qty == 10 and fills[0].price == 70_000
    assert len(statuses) == 1 and statuses[0].body["jstatus"] == "21"


async def test_subscribe_sends_register_for_all_trs() -> None:
    session = FakeConnection([])
    client = LSWebSocketClient(FakeConnector([session]))
    client.subscribe_quotes(Underlying.SAMSUNG)
    client.subscribe_fills()
    client.subscribe_market_status()

    await client.run()

    sent = [json.loads(m) for m in session.sent]
    sent_trs = {m["body"]["tr_cd"] for m in sent}
    assert {"H1_", "NH1", "JIF"} <= sent_trs
    assert {"SC0", "SC1", "SC2", "SC3", "SC4"} <= sent_trs
    jif = next(m for m in sent if m["body"]["tr_cd"] == "JIF")
    assert jif["body"]["tr_key"] == "0"      # JIF는 시장 단위 구독(실측)
    assert jif["header"]["tr_type"] == "3"   # 시세 등록
    sc1 = next(m for m in sent if m["body"]["tr_cd"] == "SC1")
    assert sc1["header"]["tr_type"] == "1"   # 계좌 이벤트 등록(실측 — "3"이면 미수신)


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


async def test_ack_frame_without_body_is_skipped() -> None:
    # LS 구독 등록 ACK 등 body 없는 프레임은 크래시 없이 무시(on_raw로는 관측).
    frame = json.dumps({"header": {"tr_cd": "JIF", "rsp_cd": "00000"}, "body": None})
    session = FakeConnection([frame])
    client = LSWebSocketClient(FakeConnector([session]))
    statuses: list[MarketStatus] = []
    raws: list[str] = []
    client.on_market_status.append(statuses.append)
    client.on_raw.append(raws.append)
    client.subscribe_market_status()

    await client.run()  # 예외 없이 통과

    assert statuses == []       # 데이터로 처리 안 함
    assert len(raws) == 1       # 원시 프레임은 관측됨


async def test_etf_quote_subscribed_and_parsed() -> None:
    # ETF 설정이 있으면 호가 구독에 ETF 코드가 추가되고, 프레임은 KR_ETF로 해석.
    from kp_arb.domain.enums import Instrument

    etf_frame = json.dumps({
        "header": {"tr_cd": "H1_", "tr_key": "0193W0"},
        "body": {"shcode": "0193W0", "bidho1": "17595", "offerho1": "17605",
                 "hotime": "100000"},
    })
    session = FakeConnection([etf_frame])
    client = LSWebSocketClient(FakeConnector([session]),
                               etf_symbols={Underlying.SAMSUNG: "0193W0"})
    quotes: list[Quote] = []
    client.on_quote.append(quotes.append)
    client.subscribe_quotes(Underlying.SAMSUNG)

    await client.run()

    subscribed = {json.loads(m)["body"]["tr_key"] for m in session.sent}
    assert {"005930", "0193W0"} <= subscribed  # 주식 + ETF 둘 다 구독
    assert len(quotes) == 1
    assert quotes[0].instrument is Instrument.KR_ETF
    assert quotes[0].underlying is Underlying.SAMSUNG
    assert quotes[0].mid == 17_600


async def test_order_events_dispatched_by_kind() -> None:
    # SC0=접수(ack) / SC3=취소(cancel, orgordno=원주문) → OrderEvent로 분화.
    ack = json.dumps({"header": {"tr_cd": "SC0"}, "body": {"ordno": "9852", "orgordno": "0"}})
    cxl = json.dumps({"header": {"tr_cd": "SC3"}, "body": {"ordno": "9901", "orgordno": "9852"}})
    session = FakeConnection([ack, cxl])
    client = LSWebSocketClient(FakeConnector([session]))
    events = []
    client.on_order_event.append(events.append)
    client.subscribe_fills()

    await client.run()

    assert [e.kind for e in events] == ["ack", "cancel"]
    assert events[0].order_id == "9852" and events[0].org_order_id is None
    assert events[1].order_id == "9901" and events[1].org_order_id == "9852"


async def test_futures_fill_c01_parsed() -> None:
    # 실측 C01: ordno 10자리 zero-pad, cheprice는 원화의 1/100(3000.00 = 300,000원).
    frame = json.dumps({"header": {"tr_cd": "C01"},
                        "body": {"ordno": "0000010996", "chevol": "1",
                                 "cheprice": "3000.00", "chetime": "103212739",
                                 "yakseq": "00000016809", "expcode": "KR4A11670002"}})
    session = FakeConnection([frame])
    client = LSWebSocketClient(FakeConnector([session]))
    fills: list[Fill] = []
    client.on_fill.append(fills.append)

    await client.run()

    assert len(fills) == 1
    assert fills[0].order_id == "10996"   # zero-pad 정규화
    assert fills[0].qty == 1
    assert fills[0].price == 300_000.0    # ×100 단위 변환


async def test_futures_cancel_h01_event() -> None:
    # 실측 H01: 원주문 필드는 ordordno.
    frame = json.dumps({"header": {"tr_cd": "H01"},
                        "body": {"ordno": "0000010974", "ordordno": "0000010963",
                                 "qty": "1"}})
    session = FakeConnection([frame])
    client = LSWebSocketClient(FakeConnector([session]))
    events = []
    client.on_order_event.append(events.append)

    await client.run()

    assert events[0].kind == "cancel"
    assert events[0].order_id == "10974"
    assert events[0].org_order_id == "10963"  # 정규화된 원주문


async def test_unknown_tr_is_ignored() -> None:
    frame = json.dumps({"header": {"tr_cd": "XXX"}, "body": {}})
    session = FakeConnection([frame])
    client = LSWebSocketClient(FakeConnector([session]))
    quotes: list[Quote] = []
    client.on_quote.append(quotes.append)

    await client.run()  # 예외 없이 통과
    assert quotes == []
