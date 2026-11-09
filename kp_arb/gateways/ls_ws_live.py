"""websockets 기반 WS 커넥터 — HL 실시간(`hl_ws`)이 쓴다. LS OpenAPI WS 접속(URL 선택)은
2026-11-09 REST 경로 제거와 함께 지움(LS 실시간은 xing `XingRealClient`).

주어진 URL에 접속하고 ``ls_ws.LSWebSocketClient`` 의
``WSConnection``/``WSConnector`` Protocol을 구현한다. 구독 메시지 포맷·수신 프레임은
``LSWebSocketClient`` 가 처리(LS 실제 포맷과 일치). 끊김은 ``ConnectionError`` 로 변환해
클라이언트의 자동 재연결을 트리거한다.

주입형 ``connect`` 로 오프라인 테스트(실 네트워크 호출 금지).
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from websockets.exceptions import ConnectionClosed


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
        url: str = "",  # HL WS는 hl_ws가 URL을 넣는다(옛 LS 기본 URL은 2026-11-09 삭제)
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
