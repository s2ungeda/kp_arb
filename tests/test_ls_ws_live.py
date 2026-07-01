"""LS 실 WS 커넥터 테스트. 가짜 ws(주입형 connect) — 실 네트워크 호출 없음."""
from __future__ import annotations

import pytest
from websockets.exceptions import ConnectionClosed

from kp_arb.gateways.ls_ws_live import LSWebSocketConnector


class FakeWs:
    def __init__(self, frames: list[str | bytes], *, closed: bool = False) -> None:
        self.frames = frames
        self.closed_after = closed
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self) -> FakeWs:
        self._iter = iter(self.frames)
        return self

    async def __anext__(self) -> str | bytes:
        try:
            return next(self._iter)
        except StopIteration:
            if self.closed_after:
                raise ConnectionClosed(None, None) from None
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


async def test_connector_wraps_send_and_iterates() -> None:
    ws = FakeWs(["a", "b"])

    async def fake_connect(url: str) -> FakeWs:
        assert url == "wss://x/websocket"
        return ws

    conn = await LSWebSocketConnector("wss://x/websocket", connect=fake_connect).connect()
    await conn.send("subscribe")
    frames = [f async for f in conn]

    assert frames == ["a", "b"]
    assert ws.sent == ["subscribe"]


async def test_bytes_frames_decoded() -> None:
    ws = FakeWs([b"hello"])

    async def fake_connect(url: str) -> FakeWs:
        return ws

    conn = await LSWebSocketConnector(connect=fake_connect).connect()
    frames = [f async for f in conn]
    assert frames == ["hello"]


def test_ls_ws_url_by_mode() -> None:
    from kp_arb.config import RunMode
    from kp_arb.gateways.ls_ws_live import ls_ws_url

    assert ls_ws_url(RunMode.PAPER).endswith(":29443/websocket")  # 모의
    assert ls_ws_url(RunMode.LIVE).endswith(":9443/websocket")    # 실전


async def test_closed_becomes_connection_error() -> None:
    ws = FakeWs(["a"], closed=True)

    async def fake_connect(url: str) -> FakeWs:
        return ws

    conn = await LSWebSocketConnector(connect=fake_connect).connect()
    with pytest.raises(ConnectionError):
        _ = [f async for f in conn]
