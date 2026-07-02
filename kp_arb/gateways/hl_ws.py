"""HL 실시간 WebSocket 클라이언트 — 마크(activeAssetCtx) + 체결(userFills).

LS와 동일 패턴: asyncio 네이티브, 주입형 ``WSConnector``/``WSConnection``(ls_ws의 Protocol
재사용), 끊기면 자동 재연결·재구독. 라이브 커넥터는 ``HLWebSocketConnector``.

구독(공식 WS 프로토콜):
- ``{"method":"subscribe","subscription":{"type":"activeAssetCtx","coin":"xyz:SMSN"}}``
  → ``{"channel":"activeAssetCtx","data":{"coin":..,"ctx":{"markPx":..}}}``
- ``{"method":"subscribe","subscription":{"type":"userFills","user":"0x.."}}``
  → ``{"channel":"userFills","data":{"isSnapshot"?,"fills":[{oid,tid,px,sz,time,fee,..}]}}``
  isSnapshot(과거 체결 일괄)은 스킵 — 이벤트만 OrderBook으로.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from ..domain.enums import Underlying
from .hl import Mark
from .hl_live import HL_SYMBOLS
from .ls_ws import Fill, WSConnection, WSConnector

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


class HLWebSocketClient:
    """HL WS — 구독 상태 보존, 재연결·재구독, mark/fill 이벤트 방출."""

    def __init__(
        self,
        connector: WSConnector,
        *,
        symbols: dict[Underlying, str] | None = None,
        max_reconnects: int = 3,
        reconnect_backoff_s: float = 0.0,
    ) -> None:
        self._connector = connector
        self._symbols = dict(symbols or HL_SYMBOLS)
        self._by_symbol = {v: k for k, v in self._symbols.items()}
        self._max_reconnects = max_reconnects
        self._reconnect_backoff_s = reconnect_backoff_s
        self._subs: list[dict[str, Any]] = []  # subscription payload 희망 상태
        self.on_mark: list[Callable[[Mark], None]] = []
        self.on_fill: list[Callable[[Fill], None]] = []
        self.on_raw: list[Callable[[str], None]] = []

    # --- 구독 등록 ---

    def subscribe_marks(self) -> None:
        for coin in self._symbols.values():
            self._add({"type": "activeAssetCtx", "coin": coin})

    def subscribe_user_fills(self, address: str) -> None:
        self._add({"type": "userFills", "user": address})

    def _add(self, subscription: dict[str, Any]) -> None:
        if subscription not in self._subs:
            self._subs.append(subscription)

    # --- 실행 루프 (LSWebSocketClient와 동일 패턴) ---

    async def run(self) -> None:
        attempts = 0
        while True:
            conn = await self._connector.connect()
            for sub in self._subs:
                await conn.send(json.dumps({"method": "subscribe", "subscription": sub}))
            try:
                async for raw in conn:
                    self._dispatch(raw)
            except ConnectionError:
                attempts += 1
                if attempts > self._max_reconnects:
                    raise
                if self._reconnect_backoff_s > 0:
                    await asyncio.sleep(self._reconnect_backoff_s)
                continue
            else:
                return

    # --- 파싱/디스패치 ---

    def _dispatch(self, raw: str) -> None:
        for raw_handler in self.on_raw:
            raw_handler(raw)
        msg = json.loads(raw)
        channel = msg.get("channel")
        data = msg.get("data")
        if not isinstance(data, dict):
            return  # 구독 ACK("subscriptionResponse") 등
        if channel == "activeAssetCtx":
            mark = self._parse_mark(data)
            if mark is not None:
                for handler in self.on_mark:
                    handler(mark)
        elif channel == "userFills":
            if data.get("isSnapshot"):
                return  # 과거 체결 일괄 — 이벤트 아님
            for fill in self._parse_fills(data):
                for fill_handler in self.on_fill:
                    fill_handler(fill)

    def _parse_mark(self, data: dict[str, Any]) -> Mark | None:
        underlying = self._by_symbol.get(str(data.get("coin", "")))
        ctx = data.get("ctx", {})
        if underlying is None or "markPx" not in ctx:
            return None
        return Mark(underlying=underlying, price=float(ctx["markPx"]),
                    ts=float(ctx.get("time", 0.0)))

    def _parse_fills(self, data: dict[str, Any]) -> list[Fill]:
        fills: list[Fill] = []
        for f in data.get("fills", []):
            if str(f.get("coin", "")) not in self._by_symbol:
                continue  # 대상 외 코인
            fills.append(
                Fill(
                    fill_id=str(f.get("tid", "")),
                    order_id=str(f["oid"]),
                    qty=float(f["sz"]),
                    price=float(f["px"]),
                    fee=float(f.get("fee", 0.0) or 0.0),
                    ts=float(f.get("time", 0.0)),
                )
            )
        return fills


class HLWebSocketConnector:
    """라이브 커넥터(websockets). ``WSConnector`` 구현 — ls_ws_live와 동일 패턴."""

    def __init__(
        self,
        url: str = HL_WS_URL,
        *,
        connect: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._url = url
        self._connect = connect

    async def connect(self) -> WSConnection:
        from .ls_ws_live import LSWebSocketConnection, _default_connect

        connector = self._connect or _default_connect
        ws = await connector(self._url)
        return LSWebSocketConnection(ws)
