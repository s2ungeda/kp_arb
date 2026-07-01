"""LS 실시간 WebSocket 실 커넥터 (websockets). 라이브 결선용.

``wss://openapi.ls-sec.co.kr:9443/websocket`` 에 접속하고, ``ls_ws.LSWebSocketClient`` 의
``WSConnection``/``WSConnector`` Protocol을 구현한다. 구독 메시지 포맷·수신 프레임은
``LSWebSocketClient`` 가 처리(LS 실제 포맷과 일치). 끊김은 ``ConnectionError`` 로 변환해
클라이언트의 자동 재연결을 트리거한다.

주입형 ``connect`` 로 오프라인 테스트(실 네트워크 호출 금지). 실제 접속은 ``ws_check`` 등 수동 실행.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from websockets.exceptions import ConnectionClosed

from ..config import RunMode

# WS는 모의/실전 포트가 다르다 (REST는 동일 8080, WS는 실전 9443 / 모의 29443).
LS_WS_URL_LIVE = "wss://openapi.ls-sec.co.kr:9443/websocket"
LS_WS_URL_PAPER = "wss://openapi.ls-sec.co.kr:29443/websocket"
LS_WS_URL = LS_WS_URL_LIVE  # 기본(실전)


def ls_ws_url(mode: RunMode) -> str:
    return LS_WS_URL_PAPER if mode is RunMode.PAPER else LS_WS_URL_LIVE


class LSWebSocketConnection:
    """단일 WS 세션 래퍼. ``WSConnection`` 구현."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, message: str) -> None:
        await self._ws.send(message)

    def __aiter__(self) -> AsyncIterator[str]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[str]:
        try:
            async for frame in self._ws:
                yield frame if isinstance(frame, str) else frame.decode("utf-8")
        except ConnectionClosed as exc:  # 끊김 → 재연결 트리거
            raise ConnectionError("LS WS closed") from exc

    async def close(self) -> None:
        await self._ws.close()


class LSWebSocketConnector:
    """WS 세션 팩토리. ``WSConnector`` 구현. 재연결 시마다 새 세션."""

    def __init__(
        self,
        url: str = LS_WS_URL,
        *,
        connect: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._url = url
        self._connect = connect

    async def connect(self) -> LSWebSocketConnection:
        connector = self._connect or _default_connect
        ws = await connector(self._url)
        return LSWebSocketConnection(ws)


async def _default_connect(url: str) -> Any:
    import websockets

    return await websockets.connect(url)
