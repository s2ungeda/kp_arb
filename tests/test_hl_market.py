"""HL 마크·펀딩·포지션 계약 테스트. 라이브 없음(mock transport + 가짜 WS + 녹화 픽스처)."""
import json
from collections.abc import AsyncIterator
from typing import Any

from kp_arb.domain.enums import Instrument, Side, Underlying, Venue
from kp_arb.gateways.hl import HLApiGateway, Mark
from kp_arb.gateways.hl_auth import Signature

SYMBOLS = {Underlying.SAMSUNG: "SAMSUNG-PERP", Underlying.SK_HYNIX: "HYNIX-PERP"}


class MockSigner:
    @property
    def address(self) -> str:
        return "0xAGENT"

    def sign_l1_action(self, action: dict[str, Any], nonce: int) -> Signature:
        return Signature(r="0xrr", s="0xss", v=27)


class InfoTransport:
    """/info 쿼리 type별 녹화 픽스처."""

    def __init__(self, fixtures: dict[str, dict[str, Any]]) -> None:
        self.fixtures = fixtures
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, body))
        return self.fixtures[body["type"]]


class FakeMarkConn:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def _gen(self) -> AsyncIterator[str]:
        for f in self.frames:
            yield f

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen()


def _gateway(transport: Any) -> HLApiGateway:
    return HLApiGateway(MockSigner(), transport, symbols=SYMBOLS, nonce_fn=lambda: 1)


def mark_frame(coin: str, mark: str) -> str:
    return json.dumps({"channel": "mark", "data": {"coin": coin, "mark": mark, "ts": 1.0}})


async def test_get_funding() -> None:
    gw = _gateway(InfoTransport({"funding": {"funding": "0.0001"}}))
    rate = await gw.get_funding(Underlying.SAMSUNG)
    assert rate == 0.0001


async def test_get_positions_maps_and_filters() -> None:
    fixture = {
        "clearinghouseState": {
            "assetPositions": [
                {"position": {"coin": "SAMSUNG-PERP", "szi": "3", "entryPx": "70000"}},
                {"position": {"coin": "HYNIX-PERP", "szi": "-2", "entryPx": "180000"}},
                {"position": {"coin": "SAMSUNG-PERP", "szi": "0", "entryPx": "0"}},  # skip
                {"position": {"coin": "UNKNOWN", "szi": "5", "entryPx": "1"}},  # skip
            ]
        }
    }
    gw = _gateway(InfoTransport(fixture))
    positions = await gw.get_positions()
    assert len(positions) == 2
    long_pos = next(p for p in positions if p.underlying is Underlying.SAMSUNG)
    short_pos = next(p for p in positions if p.underlying is Underlying.SK_HYNIX)
    assert long_pos.venue is Venue.HYPERLIQUID
    assert long_pos.instrument is Instrument.HL_PERP
    assert long_pos.account is None  # HL은 KR 계좌 없음
    assert long_pos.side is Side.BUY and long_pos.qty == 3
    assert short_pos.side is Side.SELL and short_pos.qty == 2


async def test_subscribe_and_stream_marks() -> None:
    gw = _gateway(InfoTransport({}))
    gw.subscribe_mark(Underlying.SAMSUNG)
    conn = FakeMarkConn([mark_frame("SAMSUNG-PERP", "70010.5"), mark_frame("UNKNOWN", "1")])
    marks: list[Mark] = []
    gw.on_mark.append(marks.append)

    await gw.stream_marks(conn)

    assert len(marks) == 1  # 미지 코인 프레임은 무시
    assert marks[0].underlying is Underlying.SAMSUNG
    assert marks[0].price == 70_010.5
    assert conn.sent  # 구독 메시지 전송됨
    assert json.loads(conn.sent[0])["subscription"]["coin"] == "SAMSUNG-PERP"
