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
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..domain.enums import Instrument, Underlying, Venue
from ..domain.models import Quote
from ..order_log import ws_order_raw
from ..ws_status import WsStatus
from .hl import Mark
from .hl_live import HL_SYMBOLS
from .ls_ws import Fill, TradeTick, WSConnection, WSConnector

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


@dataclass(frozen=True)
class OrderUpdate:
    """HL orderUpdates 이벤트 — 주문 상태 변화(open/filled/canceled/rejected…).

    체결(userFills)과 달리 **취소·거부까지** 밀어준다 — 외부(홈페이지) 취소·자동취소
    (post-only/reduce-only/마진부족 등)를 실시간으로 알 수 있어, 조회 없이 OrderBook 정합.
    """

    oid: str
    coin: str
    status: str
    side: str                 # "B"(매수)|"A"(매도)
    sz: float                 # 남은 수량
    orig_sz: float            # 최초 수량
    limit_px: float | None

    @property
    def is_rejected(self) -> bool:
        return self.status == "rejected" or self.status.endswith("Rejected")

    @property
    def is_terminal_cancel(self) -> bool:
        """체결 제외 종료(취소·거부 계열) — 더 이상 미체결 아님 → OrderBook 제거 대상.

        상태값(공식): canceled·marginCanceled·reduceOnlyCanceled·selfTradeCanceled 등
        ``*Canceled`` 계열 + rejected·``*Rejected`` 계열. 'filled'은 userFills가 담당(제외),
        'open'·'triggered'은 살아있음(제외).
        """
        s = self.status
        return s == "canceled" or s.endswith("Canceled") or self.is_rejected


class HLWebSocketClient:
    """HL WS — 구독 상태 보존, 재연결·재구독, mark/fill 이벤트 방출."""

    def __init__(
        self,
        connector: WSConnector,
        *,
        symbols: dict[Underlying, str] | None = None,
        max_reconnects: int = 3,
        reconnect_backoff_s: float = 0.0,
        status: WsStatus | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._connector = connector
        self._symbols = dict(symbols or HL_SYMBOLS)
        self._by_symbol = {v: k for k, v in self._symbols.items()}
        self._max_reconnects = max_reconnects
        self._reconnect_backoff_s = reconnect_backoff_s
        # WS 세션 현황(Phase 8-3) — 연결/끊김·수신카운트. 미주입 시 자체 생성.
        self.status = status or WsStatus(
            venue="HL", name="HL", kind="시세/주문", expects_stream=True)
        self._clock = clock or time.monotonic
        self._subs: list[dict[str, Any]] = []  # subscription payload 희망 상태
        # 최근 호가창(l2Book) — bbo 프레임에 다단계를 붙일 때 사용.
        self._depth: dict[
            Underlying, tuple[list[tuple[float, float]], list[tuple[float, float]]]
        ] = {}
        # l2Book 호가단위 머지 상태(coin별 nSigFigs/mantissa — 비면 원시) + 제어 큐
        self._l2_extra: dict[str, dict[str, int]] = {}
        self._control: deque[dict[str, Any]] = deque()  # 활성 연결로 보낼 (un)subscribe
        self.on_mark: list[Callable[[Mark], None]] = []
        self.on_quote: list[Callable[[Quote], None]] = []          # 최우선호가(bbo)
        self.on_trade: list[Callable[[TradeTick], None]] = []      # 체결(현재가, ~0.2s)
        self.on_funding: list[Callable[[Underlying, float], None]] = []  # 예정 펀딩률
        self.on_fill: list[Callable[[Fill], None]] = []
        self.on_order_update: list[Callable[[OrderUpdate], None]] = []  # 주문상태(취소 등)
        self.on_raw: list[Callable[[str], None]] = []
        # 재연결(최초 연결 제외) 후 재구독까지 끝나면 발화 — OrderBook 재스냅샷용(Phase 8-4).
        self.on_reconnect: list[Callable[[], None]] = []

    # --- 구독 등록 ---

    def subscribe_marks(self) -> None:
        for coin in self._symbols.values():
            self._add({"type": "activeAssetCtx", "coin": coin})

    def subscribe_bbo(self) -> None:
        """최우선호가(매수/매도 1호가 + 잔량) 구독 → on_quote(Quote[HL_PERP])."""
        for coin in self._symbols.values():
            self._add({"type": "bbo", "coin": coin})

    def subscribe_l2book(self) -> None:
        """호가창 구독 → on_quote(Quote.bids/asks). **fast=true: 5단계·빠름**(문서:
        '5 levels if fast, 20 levels if slow'). 실측상 slow(20단계)는 ~0.4/s로 느려
        화면이 뚝뚝 끊겨(HL 홈페이지와 비교 불가) fast로 간다 — 5단계면 표시엔 충분."""
        for coin in self._symbols.values():
            self._add({"type": "l2Book", "coin": coin, "fast": True})

    def subscribe_trades(self) -> None:
        """공개 체결 구독 → on_trade(현재가). 마크(1초 주기)보다 빠르다(실측 ~0.2초)."""
        for coin in self._symbols.values():
            self._add({"type": "trades", "coin": coin})

    def subscribe_user_fills(self, address: str) -> None:
        self._add({"type": "userFills", "user": address})

    def subscribe_order_updates(self, address: str) -> None:
        """주문 상태 변화 구독 → on_order_update. 외부(홈페이지) 취소·자동취소도 실시간 반영."""
        self._add({"type": "orderUpdates", "user": address})

    def _add(self, subscription: dict[str, Any]) -> None:
        if subscription not in self._subs:
            self._subs.append(subscription)

    def set_l2_aggregation(
        self,
        underlying: Underlying,
        n_sig_figs: int | None,
        mantissa: int | None = None,
    ) -> None:
        """l2Book 호가단위 머지 변경 — 구독 취소 후 재구독 (사용자 확정 2026-07-23).

        서버 옵션: nSigFigs 2~5 또는 None(원시), mantissa 1·2·5(nSigFigs=5일 때만).
        저장된 구독 희망 상태도 갱신해 재연결 시 같은 단위로 다시 붙는다.
        UI 스레드에서 호출해도 안전 — 큐에만 넣고 전송은 WS 루프가 한다.
        """
        coin = self._symbols[underlying]
        target = next((s for s in self._subs
                       if s.get("type") == "l2Book" and s.get("coin") == coin), None)
        if target is None:
            return  # l2Book 미구독
        self._control.append({"method": "unsubscribe", "subscription": dict(target)})
        target.pop("nSigFigs", None)
        target.pop("mantissa", None)
        extra: dict[str, int] = {}
        if n_sig_figs is not None:
            extra["nSigFigs"] = n_sig_figs
            if mantissa is not None:
                extra["mantissa"] = mantissa
        target.update(extra)
        self._l2_extra[coin] = extra
        self._depth.pop(underlying, None)  # 옛 단위 호가창 폐기
        self._control.append({"method": "subscribe", "subscription": dict(target)})

    def l2_aggregation(
        self, underlying: Underlying
    ) -> tuple[int | None, int | None]:
        """현재 적용 중인 l2Book 머지(nSigFigs, mantissa) — 원시(미설정)면 (None, None).

        모든 창이 같은 값을 읽어 콤보를 맞추도록(단일 진실=코어) 스냅샷에 실어 보낸다.
        """
        coin = self._symbols.get(underlying)
        extra = self._l2_extra.get(coin, {}) if coin is not None else {}
        return (extra.get("nSigFigs"), extra.get("mantissa"))

    # --- 실행 루프 (LSWebSocketClient와 동일 패턴) ---

    async def run(self) -> None:
        """연결 → 구독 → 디스패치. 끊기면 재연결(데이터 흐르면 카운터 초기화).

        HL은 유지용 ping(50초 미만 간격 권장)을 보내지 않으면 서버가 유휴 연결을
        끊을 수 있어 45초마다 ping을 보낸다(응답 pong은 무시).
        """
        attempts = 0
        while True:
            ping_task: asyncio.Task[None] | None = None
            control_task: asyncio.Task[None] | None = None
            try:
                conn = await self._connector.connect()
                self.status.on_connect()
                self._control.clear()  # 재연결이면 희망 상태로 전부 재구독 — 옛 제어 폐기
                for sub in self._subs:
                    await conn.send(json.dumps({"method": "subscribe", "subscription": sub}))
                if self.status.connects > 1:  # 재연결(최초 아님) → 재동기 훅
                    for on_reconnect in self.on_reconnect:
                        on_reconnect()
                ping_task = asyncio.create_task(self._ping_loop(conn))
                control_task = asyncio.create_task(self._control_loop(conn))
                async for raw in conn:
                    attempts = 0  # 데이터 수신 = 정상 연결
                    self.status.on_message(self._clock())
                    try:
                        self._dispatch(raw)
                    except Exception:  # noqa: BLE001 - 프레임 1건 문제로 스트림을 죽이지 않음
                        import logging

                        logging.getLogger("kp_arb.hl_ws").warning(
                            "프레임 처리 실패 — 건너뜀: %.300s", raw, exc_info=True
                        )
            except (ConnectionError, OSError):
                if self.status.connected:
                    self.status.on_disconnect()
                attempts += 1
                if attempts > self._max_reconnects:
                    raise
                if self._reconnect_backoff_s > 0:
                    await asyncio.sleep(self._reconnect_backoff_s)
                continue
            else:
                self.status.on_disconnect()  # 스트림 정상 종료 = 서버가 닫음 = 끊김
                return
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                if control_task is not None:
                    control_task.cancel()

    @staticmethod
    async def _ping_loop(conn: WSConnection, interval_s: float = 45.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                await conn.send('{"method":"ping"}')
        except Exception:  # noqa: BLE001 - 연결 종료 시 조용히 끝 (본선이 재연결)
            return

    async def _control_loop(self, conn: WSConnection, interval_s: float = 0.3) -> None:
        """머지 변경 등 제어 메시지를 활성 연결로 전송 (UI 스레드 → 큐 → 여기)."""
        try:
            while True:
                while self._control:
                    await conn.send(json.dumps(self._control.popleft()))
                await asyncio.sleep(interval_s)
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
        if channel == "orderUpdates":
            ws_order_raw(Venue.HYPERLIQUID, raw)  # 주문상태 원본(가공 전) 상시 기록
            # data는 주문상태 목록(list) — 초기 스냅샷(모두 open)도 같은 형식(open은 무시됨).
            if isinstance(data, list):
                for upd in self._parse_order_updates(data):
                    for upd_handler in self.on_order_update:
                        upd_handler(upd)
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
            ws_order_raw(Venue.HYPERLIQUID, raw)  # 체결통보 원본(스냅샷 포함) 상시 기록
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
        # 스칼라 bid/ask(위)는 bbo(빠름). **호가창(bids/asks)은 자체 정합적인 l2Book만**
        # 쓴다 — 신선한 bbo 1호가를 스테일 l2에 섞으면 크로스(매도<매수)나 한쪽 단계 소실이
        # 생긴다(실측). 호가창은 l2Book 프레임 단위로 갱신(대칭·정합, 갱신율은 l2 피드에 의존).
        depth = self._depth.get(underlying)
        bids = depth[0] if depth is not None else None
        asks = depth[1] if depth is not None else None
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
        # 서버 최대인 한쪽당 20단계까지 보관 (est-pr·머지 표시용).
        bids = [(float(x["px"]), float(x.get("sz", 0) or 0))
                for x in (levels[0] or [])[:20] if isinstance(x, dict) and "px" in x]
        asks = [(float(x["px"]), float(x.get("sz", 0) or 0))
                for x in (levels[1] or [])[:20] if isinstance(x, dict) and "px" in x]
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

    def _parse_order_updates(self, data: list[Any]) -> list[OrderUpdate]:
        out: list[OrderUpdate] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            order = row.get("order") or {}
            oid, status = order.get("oid"), row.get("status")
            if oid is None or status is None:
                continue
            if str(order.get("coin", "")) not in self._by_symbol:
                continue  # 대상 외 코인
            px = order.get("limitPx")
            out.append(OrderUpdate(
                oid=str(oid), coin=str(order.get("coin", "")), status=str(status),
                side=str(order.get("side", "")),
                sz=float(order.get("sz", 0) or 0),
                orig_sz=float(order.get("origSz", 0) or 0),
                limit_px=float(px) if px is not None and px != "" else None))
        return out


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
