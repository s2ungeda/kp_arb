"""HL 주문을 웹소켓으로 보내는 클라이언트(WS post) — DESIGN-hl-ws-post.md(사용자 2026-11-11).

공식 WS 프로토콜 "Post requests": HTTP /exchange에 보내던 서명 액션 본문을
``{"method":"post","id":N,"request":{"type":"action","payload":본문}}``로 보내면
``{"channel":"post","data":{"id":N,"response":{"type":"action"|"error","payload":…}}}``가 온다.

- 시세·체결 구독(hl_ws)과 **별도 연결**. 20초마다 ping, 끊기면 대기 요청 전부 실패 처리 뒤
  2초마다 재접속.
- 미접속이면 ``HLWsPostUnavailable`` → 게이트웨이가 HTTP(옛 길)로 보낸다. 전송 뒤 응답 없음은
  "결과 모름"(재전송 금지, CLAUDE.md §7).
- SDK 호출은 다른 스레드(asyncio.to_thread)에서 오므로 ``post_action_threadsafe``로 코어 루프에
  넘긴다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from ..ws_status import WsStatus
from .ls_ws import WSClosed, WSConnection, WSConnector

log = logging.getLogger("kp_arb.hl_post")

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


class HLWsPostUnavailable(RuntimeError):
    """지금 WS로 보낼 수 없음(미접속·재접속 중) — 호출부는 HTTP로 보낸다. 요청은 나가지 않았다."""


class HLWsPostError(RuntimeError):
    """서버가 post 응답으로 error를 돌려줌(payload는 HTTP 상태·설명 문자열). 요청은 서버에 닿음."""


class HLWsPostClient:
    """발주·취소 등 서명 액션을 WS 한 연결로 보내고 id로 응답을 맞춘다."""

    def __init__(
        self,
        connector: WSConnector,
        *,
        reconnect_backoff_s: float = 2.0,
        ping_interval_s: float = 20.0,
        status: WsStatus | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connector = connector
        self._backoff_s = reconnect_backoff_s
        self._ping_interval_s = ping_interval_s
        self._clock = clock
        self.status = status or WsStatus(
            venue="HL", name="HL 주문 WS", kind="주문", expects_stream=False)
        self._conn: WSConnection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self.sent_count = 0      # 보낸 post 수(로그·테스트)
        self.fallback_count = 0  # 미접속이라 HTTP로 돌린 수(게이트웨이가 올림)

    # --- 상태 ---

    @property
    def ready(self) -> bool:
        return self._conn is not None and self.status.connected

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """다른 스레드에서 post할 때 쓸 코어 루프를 기억한다(run()이 자동으로도 부른다)."""
        self._loop = loop or asyncio.get_running_loop()

    # --- 연결 루프 ---

    async def run(self) -> None:
        """연결 → 응답 읽기. 끊기면(예외·정상 종료 모두) 대기 요청을 실패시키고 재접속(무제한).
        커넥터가 ``WSClosed``를 던지면(테스트·셧다운) 재접속 없이 끝."""
        self.bind_loop()
        while True:
            ping_task: asyncio.Task[None] | None = None
            try:
                conn = await self._connector.connect()
                self._conn = conn
                self.status.on_connect()
                log.info("HL 주문 WS 연결(%d회째)", self.status.connects)
                ping_task = asyncio.create_task(self._ping_loop(conn))
                async for raw in conn:
                    self.status.on_message(self._clock())
                    try:
                        self._dispatch(raw)
                    except Exception:  # noqa: BLE001 - 프레임 1건 문제로 연결을 죽이지 않음
                        log.warning("HL 주문 WS 프레임 처리 실패 — 건너뜀: %.300s", raw,
                                    exc_info=True)
            except WSClosed:
                self._on_disconnect("종료")
                return
            except asyncio.CancelledError:
                self._on_disconnect("취소", final=True)  # 셧다운 — 재접속 문구 없이
                raise
            except (ConnectionError, OSError) as exc:
                self._on_disconnect(f"{type(exc).__name__}: {exc}")
            else:
                self._on_disconnect("서버가 닫음")  # 정상 종료 = 서버 쪽 순환·유휴 종료
            finally:
                if ping_task is not None:
                    ping_task.cancel()
            await asyncio.sleep(self._backoff_s)

    def _on_disconnect(self, why: str, *, final: bool = False) -> None:
        was = self.status.connected
        self._conn = None
        if was:
            self.status.on_disconnect()
        n = self._fail_pending(f"HL 주문 WS 끊김({why})")
        if final:
            log.info("HL 주문 WS 종료(%s) — 대기 요청 %d건 실패 처리", why, n)
        elif was or n:
            log.warning("HL 주문 WS 끊김(%s) — 대기 요청 %d건 실패 처리, %.0f초 뒤 재접속",
                        why, n, self._backoff_s)
        else:
            log.warning("HL 주문 WS 연결 실패(%s) — %.0f초 뒤 재접속", why, self._backoff_s)

    def _fail_pending(self, message: str) -> int:
        n = 0
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError(message))
                n += 1
        self._pending.clear()
        return n

    async def _ping_loop(self, conn: WSConnection) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_interval_s)
                await conn.send('{"method":"ping"}')
        except Exception:  # noqa: BLE001 - 연결 종료 시 조용히 끝(본선이 재접속)
            return

    # --- 응답 ---

    def _dispatch(self, raw: str) -> None:
        msg = json.loads(raw)
        channel = msg.get("channel")
        if channel == "pong":
            return
        if channel != "post":
            log.debug("HL 주문 WS 다른 채널 무시: %s", channel)
            return
        data = msg.get("data") or {}
        rid = data.get("id")
        fut = self._pending.pop(int(rid), None) if rid is not None else None
        if fut is None or fut.done():
            log.debug("HL 주문 WS 대기 없는 응답 id=%s 무시", rid)
            return
        response = data.get("response") or {}
        if response.get("type") == "error":
            fut.set_exception(HLWsPostError(str(response.get("payload"))))
        else:
            fut.set_result(response.get("payload"))

    # --- 전송 ---

    async def post_action(self, payload: dict[str, Any], timeout_s: float) -> Any:
        """서명 액션 본문 하나를 보내고 응답 payload(HTTP /exchange 응답과 같은 모양)를 돌려준다.

        미접속이면 ``HLWsPostUnavailable``(요청 안 나감). 보낸 뒤 끊기면 ConnectionError,
        응답이 늦으면 TimeoutError — 둘 다 "결과 모름"(호출부가 재전송하지 않는다).
        """
        conn = self._conn
        if conn is None or not self.status.connected:
            raise HLWsPostUnavailable("HL 주문 WS 미접속")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        frame = json.dumps({"method": "post", "id": rid,
                            "request": {"type": "action", "payload": payload}})
        kind = str((payload.get("action") or {}).get("type", "?"))
        t0 = time.perf_counter()
        try:
            await conn.send(frame)
        except Exception as exc:  # noqa: BLE001 - 보내다 끊김 = 나갔는지 모름
            self._pending.pop(rid, None)
            raise ConnectionError(f"HL 주문 WS 전송 실패: {exc}") from exc
        self.sent_count += 1
        try:
            result = await asyncio.wait_for(fut, timeout_s)
        except TimeoutError:
            self._pending.pop(rid, None)
            log.warning("HL WS post #%d %s 응답 없음 %.0f초 — 결과 모름", rid, kind, timeout_s)
            raise
        except HLWsPostError as exc:
            log.warning("HL WS post #%d %s 오류 응답 %.0f ms — %s", rid, kind,
                        (time.perf_counter() - t0) * 1000, exc)
            raise
        except ConnectionError:
            log.warning("HL WS post #%d %s 응답 전 끊김 — 결과 모름", rid, kind)
            raise
        status = result.get("status") if isinstance(result, dict) else None
        log.info("HL WS post #%d %s 왕복 %.0f ms — %s", rid, kind,
                 (time.perf_counter() - t0) * 1000, status)
        return result

    def post_action_threadsafe(self, payload: dict[str, Any], timeout_s: float) -> Any:
        """다른 스레드(SDK 호출이 도는 asyncio.to_thread)에서 — 코어 루프에 넘기고 결과를 기다림."""
        loop = self._loop
        if loop is None or loop.is_closed():
            raise HLWsPostUnavailable("HL 주문 WS 루프 없음")
        if not self.ready:
            raise HLWsPostUnavailable("HL 주문 WS 미접속")
        fut = asyncio.run_coroutine_threadsafe(self.post_action(payload, timeout_s), loop)
        # 루프 쪽 wait_for가 먼저 TimeoutError를 내므로 여기 여유는 그보다 조금 길게
        return fut.result(timeout_s + 2.0)


class HLWsPostConnector:
    """라이브 커넥터(websockets) — hl_ws의 HLWebSocketConnector와 같은 모양, URL만 같은 서버."""

    def __init__(self, url: str = HL_WS_URL) -> None:
        self._url = url

    async def connect(self) -> WSConnection:
        from .ls_ws_live import LSWebSocketConnection, _default_connect

        ws = await _default_connect(self._url)
        return LSWebSocketConnection(ws)
