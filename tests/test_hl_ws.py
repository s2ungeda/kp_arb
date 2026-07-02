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
