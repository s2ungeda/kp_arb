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

from ..domain.enums import Instrument, Underlying
from ..domain.models import Quote
from .hl import Mark
from .hl_live import HL_SYMBOLS
from .ls_ws import Fill, TradeTick, WSConnection, WSConnector

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
        # 최근 호가창(l2Book) — bbo 프레임에 다단계를 붙일 때 사용.
        self._depth: dict[
            Underlying, tuple[list[tuple[float, float]], list[tuple[float, float]]]
        ] = {}
        self.on_mark: list[Callable[[Mark], None]] = []
        self.on_quote: list[Callable[[Quote], None]] = []          # 최우선호가(bbo)
        self.on_trade: list[Callable[[TradeTick], None]] = []      # 체결(현재가, ~0.2s)
        self.on_funding: list[Callable[[Underlying, float], None]] = []  # 예정 펀딩률
        self.on_fill: list[Callable[[Fill], None]] = []
        self.on_raw: list[Callable[[str], None]] = []

    # --- 구독 등록 ---

    def subscribe_marks(self) -> None:
        for coin in self._symbols.values():
            self._add({"type": "activeAssetCtx", "coin": coin})

    def subscribe_bbo(self) -> None:
        """최우선호가(매수/매도 1호가 + 잔량) 구독 → on_quote(Quote[HL_PERP])."""
        for coin in self._symbols.values():
            self._add({"type": "bbo", "coin": coin})

    def subscribe_l2book(self) -> None:
        """호가창(다단계) 구독 → on_quote(Quote.bids/asks 포함). bbo보다 깊고 약간 느림."""
        for coin in self._symbols.values():
            self._add({"type": "l2Book", "coin": coin})

    def subscribe_trades(self) -> None:
        """공개 체결 구독 → on_trade(현재가). 마크(1초 주기)보다 빠르다(실측 ~0.2초)."""
        for coin in self._symbols.values():
            self._add({"type": "trades", "coin": coin})

    def subscribe_user_fills(self, address: str) -> None:
        self._add({"type": "userFills", "user": address})

    def _add(self, subscription: dict[str, Any]) -> None:
        if subscription not in self._subs:
            self._subs.append(subscription)

    # --- 실행 루프 (LSWebSocketClient와 동일 패턴) ---

    async def run(self) -> None:
        """연결 → 구독 → 디스패치. 끊기면 재연결(데이터 흐르면 카운터 초기화).

        HL은 유지용 ping(50초 미만 간격 권장)을 보내지 않으면 서버가 유휴 연결을
        끊을 수 있어 45초마다 ping을 보낸다(응답 pong은 무시).
        """
        attempts = 0
        while True:
            ping_task: asyncio.Task[None] | None = None
            try:
                conn = await self._connector.connect()
                for sub in self._subs:
                    await conn.send(json.dumps({"method": "subscribe", "subscription": sub}))
                ping_task = asyncio.create_task(self._ping_loop(conn))
                async for raw in conn:
                    attempts = 0  # 데이터 수신 = 정상 연결
                    try:
                        self._dispatch(raw)
                    except Exception:  # noqa: BLE001 - 프레임 1건 문제로 스트림을 죽이지 않음
                        import logging

                        logging.getLogger("kp_arb.hl_ws").warning(
                            "프레임 처리 실패 — 건너뜀: %.300s", raw, exc_info=True
                        )
            except (ConnectionError, OSError):
                attempts += 1
                if attempts > self._max_reconnects:
                    raise
                if self._reconnect_backoff_s > 0:
                    await asyncio.sleep(self._reconnect_backoff_s)
                continue
            else:
                return
            finally:
                if ping_task is not None:
                    ping_task.cancel()

    @staticmethod
    async def _ping_loop(conn: WSConnection, interval_s: float = 45.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                await conn.send('{"method":"ping"}')
        except Exception:  # noqa: BLE001 - 연결 종료 시 조용히 끝 (본선이 재연결)
            return

    # --- 파싱/디스패치 ---

    def _dispatch(self, raw: str) -> None:
        for raw_handler in self.on_raw:
            raw_handler(raw)
        msg = json.loads(raw)
        channel = msg.get("channel")
        data = msg.get("data")
        if channel == "trades":
            # trades의 data는 체결 목록(list).
            if isinstance(data, list):
                for tick in self._parse_trades(data):
                    for trade_handler in self.on_trade:
                        trade_handler(tick)
            return
        if not isinstance(data, dict):
            return  # 구독 ACK("subscriptionResponse") 등
        if channel == "activeAssetCtx":
            mark = self._parse_mark(data)
            if mark is not None:
                for handler in self.on_mark:
                    handler(mark)
            self._emit_funding(data)
        elif channel == "bbo":
            quote = self._parse_bbo(data)
            if quote is not None:
                for quote_handler in self.on_quote:
                    quote_handler(quote)
        elif channel == "l2Book":
            quote = self._parse_l2book(data)
            if quote is not None:
                for quote_handler in self.on_quote:
                    quote_handler(quote)
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
        oracle_raw = ctx.get("oraclePx")
        return Mark(underlying=underlying, price=float(ctx["markPx"]),
                    ts=float(ctx.get("time", 0.0)),
                    oracle=float(oracle_raw) if oracle_raw not in (None, "") else None)

    def _emit_funding(self, data: dict[str, Any]) -> None:
        underlying = self._by_symbol.get(str(data.get("coin", "")))
        ctx = data.get("ctx", {})
        if underlying is None or "funding" not in ctx:
            return
        for handler in self.on_funding:
            handler(underlying, float(ctx["funding"]))

    def _parse_bbo(self, data: dict[str, Any]) -> Quote | None:
        # bbo 프레임: {coin, time, bbo: [매수1호가, 매도1호가]} — 각 호가 {px, sz, n}.
        underlying = self._by_symbol.get(str(data.get("coin", "")))
        levels = data.get("bbo")
        if underlying is None or not isinstance(levels, list) or len(levels) != 2:
            return None
        bid, ask = levels[0] or {}, levels[1] or {}
        if "px" not in bid or "px" not in ask:
            return None
        top_bid = (float(bid["px"]), float(bid.get("sz", 0) or 0))
        top_ask = (float(ask["px"]), float(ask.get("sz", 0) or 0))
        # 1호가는 bbo(빠름)로 갱신하고, 2호가 아래는 최근 l2Book 것을 붙인다.
        depth = self._depth.get(underlying)
        bids = [top_bid] + depth[0][1:] if depth else None
        asks = [top_ask] + depth[1][1:] if depth else None
        return Quote(
            underlying=underlying,
            instrument=Instrument.HL_PERP,
            bid=top_bid[0],
            ask=top_ask[0],
            ts=float(data.get("time", 0.0)),
            bid_qty=top_bid[1],
            ask_qty=top_ask[1],
            market="hl",
            bids=bids,
            asks=asks,
        )

    def _parse_l2book(self, data: dict[str, Any]) -> Quote | None:
        # l2Book 프레임: {coin, time, levels: [[매수단계...], [매도단계...]]} — 각 {px, sz, n}.
        underlying = self._by_symbol.get(str(data.get("coin", "")))
        levels = data.get("levels")
        if underlying is None or not isinstance(levels, list) or len(levels) != 2:
            return None
        # LS(10호가)와 맞춰 한쪽당 10단계까지만 보관.
        bids = [(float(x["px"]), float(x.get("sz", 0) or 0))
                for x in (levels[0] or [])[:10] if isinstance(x, dict) and "px" in x]
        asks = [(float(x["px"]), float(x.get("sz", 0) or 0))
                for x in (levels[1] or [])[:10] if isinstance(x, dict) and "px" in x]
        if not bids or not asks:
            return None
        self._depth[underlying] = (bids, asks)
        return Quote(
            underlying=underlying,
            instrument=Instrument.HL_PERP,
            bid=bids[0][0],
            ask=asks[0][0],
            ts=float(data.get("time", 0.0)),
            bid_qty=bids[0][1],
            ask_qty=asks[0][1],
            market="hl",
            bids=bids,
            asks=asks,
        )

    def _parse_trades(self, data: list[Any]) -> list[TradeTick]:
        # 공개 체결: [{coin, side, px, sz, time, tid, ...}]
        ticks: list[TradeTick] = []
        for t in data:
            if not isinstance(t, dict):
                continue
            underlying = self._by_symbol.get(str(t.get("coin", "")))
            if underlying is None or "px" not in t:
                continue
            ticks.append(TradeTick(
                underlying=underlying,
                instrument=Instrument.HL_PERP,
                price=float(t["px"]),
                ts=float(t.get("time", 0.0)),
                market="hl",
            ))
        return ticks

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
