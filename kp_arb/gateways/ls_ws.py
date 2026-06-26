"""LS Open API WebSocket 클라이언트 (DESIGN.md §5.1).

블록 1-4 범위: 실시간 호가(H1_/NH1) + 체결(SC0~SC4) + 장운영(JIF) 구독,
연결 끊김 시 자동 재연결·재구독, on_quote / on_fill / on_market_status 이벤트 노출.

라이브 없음: 실제 WS는 주입된 ``WSConnector``/``WSConnection``(Protocol) 뒤로 격리.
테스트는 가짜 WS 서버(녹화 프레임)만 사용한다. 정확한 LS 프레임 필드명은 라이브 구현 시 확인.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from pydantic import BaseModel

from ..domain.enums import Instrument, Underlying
from ..domain.models import Quote

QUOTE_TRS: frozenset[str] = frozenset({"H1_", "NH1"})
FILL_TRS: frozenset[str] = frozenset({"SC0", "SC1", "SC2", "SC3", "SC4"})
STATUS_TR = "JIF"

_UNDERLYING_BY_CODE: dict[str, Underlying] = {u.krx_code: u for u in Underlying}


class Fill(BaseModel):
    """체결 이벤트(DESIGN.md §10 fills). 추후 StateStore에서 재사용 가능."""

    fill_id: str
    order_id: str
    qty: float
    price: float
    fee: float = 0.0
    ts: float


class MarketStatus(BaseModel):
    """장운영(JIF) 이벤트. SessionService(블록 2-1)가 SessionPhase로 해석."""

    tr_key: str
    body: dict[str, Any] = {}


class WSConnection(Protocol):
    """단일 WS 세션. 구독 메시지 전송 + 프레임 비동기 수신."""

    async def send(self, message: str) -> None: ...
    def __aiter__(self) -> AsyncIterator[str]: ...


class WSConnector(Protocol):
    """WS 세션 팩토리. 재연결 시마다 새 세션을 생성."""

    async def connect(self) -> WSConnection: ...


class LSWebSocketClient:
    """LS WS 클라이언트. 구독 상태를 보존하고 끊기면 재연결·재구독한다."""

    def __init__(
        self,
        connector: WSConnector,
        *,
        token: str = "",
        max_reconnects: int = 3,
        reconnect_backoff_s: float = 0.0,
    ) -> None:
        self._connector = connector
        self._token = token
        self._max_reconnects = max_reconnects
        self._reconnect_backoff_s = reconnect_backoff_s
        self._subs: list[tuple[str, str]] = []  # (tr_cd, tr_key) 희망 구독 상태
        self._conn: WSConnection | None = None
        self.on_quote: list[Callable[[Quote], None]] = []
        self.on_fill: list[Callable[[Fill], None]] = []
        self.on_market_status: list[Callable[[MarketStatus], None]] = []

    # --- 구독 등록(희망 상태). 실제 전송은 connect 시 _resubscribe ---

    def subscribe_quotes(self, underlying: Underlying) -> None:
        code = underlying.krx_code
        self._add("H1_", code)
        self._add("NH1", code)

    def subscribe_fills(self) -> None:
        for tr in sorted(FILL_TRS):
            self._add(tr, "")

    def subscribe_market_status(self, underlying: Underlying) -> None:
        self._add(STATUS_TR, underlying.krx_code)

    def _add(self, tr_cd: str, tr_key: str) -> None:
        if (tr_cd, tr_key) not in self._subs:
            self._subs.append((tr_cd, tr_key))

    # --- 실행 루프 ---

    async def run(self) -> None:
        """연결 → 재구독 → 프레임 디스패치. 끊기면 재연결, 깨끗이 끝나면 종료."""
        attempts = 0
        while True:
            conn = await self._connector.connect()
            self._conn = conn
            await self._resubscribe(conn)
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
                return  # 스트림이 정상 종료됨

    async def _resubscribe(self, conn: WSConnection) -> None:
        for tr_cd, tr_key in self._subs:
            await conn.send(self._register_msg(tr_cd, tr_key))

    def _register_msg(self, tr_cd: str, tr_key: str) -> str:
        return json.dumps(
            {
                "header": {"token": self._token, "tr_type": "3"},  # 3=등록
                "body": {"tr_cd": tr_cd, "tr_key": tr_key},
            }
        )

    # --- 프레임 파싱/디스패치 ---

    def _dispatch(self, raw: str) -> None:
        msg = json.loads(raw)
        tr_cd = msg.get("header", {}).get("tr_cd")
        if tr_cd in QUOTE_TRS:
            quote = self._parse_quote(msg)
            if quote is not None:
                for handler in self.on_quote:
                    handler(quote)
        elif tr_cd in FILL_TRS:
            fill = self._parse_fill(msg)
            for fill_handler in self.on_fill:
                fill_handler(fill)
        elif tr_cd == STATUS_TR:
            status = self._parse_status(msg)
            for status_handler in self.on_market_status:
                status_handler(status)
        # 알 수 없는 tr_cd는 무시

    def _parse_quote(self, msg: dict[str, Any]) -> Quote | None:
        code = msg.get("header", {}).get("tr_key", "")
        underlying = _UNDERLYING_BY_CODE.get(code)
        if underlying is None:
            return None
        body = msg["body"]
        return Quote(
            underlying=underlying,
            instrument=Instrument.KR_STOCK,  # H1_/NH1 모두 현물 호가
            bid=float(body["bid"]),
            ask=float(body["ask"]),
            ts=float(body["ts"]),
        )

    def _parse_fill(self, msg: dict[str, Any]) -> Fill:
        body = msg["body"]
        return Fill(
            fill_id=str(body["fill_id"]),
            order_id=str(body["order_id"]),
            qty=float(body["qty"]),
            price=float(body["price"]),
            fee=float(body.get("fee", 0.0)),
            ts=float(body["ts"]),
        )

    def _parse_status(self, msg: dict[str, Any]) -> MarketStatus:
        return MarketStatus(tr_key=msg.get("header", {}).get("tr_key", ""), body=msg["body"])
