"""xingAPI 실시간 클라이언트 — LSWebSocketClient의 전송 단만 XAReal로 바꾼다
(DESIGN-ls-xing.md §2.2).

구독 등록(``subscribe_*``·``_subs``)·프레임 파서·콜백(on_quote/on_fill/…)·WsStatus·on_reconnect는
부모 그대로. 다른 것은 ``run()``뿐:

1. ``ensure_login()``(코어가 넣어 줌 — 자격은 keyring, 실패하면 재시도)으로 로그인을 기다린다.
2. ``_subs``를 XAReal에 advise(키 = tr_key). 재로그인이면 부모의 ``on_reconnect``(재동기)를 울린다.
3. XAReal 이벤트(OutBlock 필드 dict)를 WS와 같은 모양의 프레임
   ``{"header": {"tr_cd", "tr_key"}, "body": {...}}``으로 만들어 부모의 ``_dispatch``에 넣는다.
4. 세션 끊김(OnDisconnect/OnLogout)이 오면 status.on_disconnect → 1로.

tr_key는 OutBlock의 종목코드 계열 필드(shcode/focode/futcode/expcode)에서 뽑는다 — 파서가 body를
먼저 보고 header는 폴백이므로 없어도 동작한다([OPEN] Res로 필드명 확인).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from ..domain.enums import Underlying
from ..ws_status import WsStatus
from .ls_ws import LSWebSocketClient, WSConnection
from .xing_com import XingSession

log = logging.getLogger("kp_arb.xing")

KEY_FIELDS: tuple[str, ...] = ("shcode", "focode", "futcode", "expcode")


def frame_from_real(tr: str, fields: dict[str, str],
                    key_fields: tuple[str, ...] = KEY_FIELDS) -> str:
    """XAReal OutBlock 필드 → WS 프레임 JSON(부모 _dispatch 입력). 순수.

    실측 2026-09-23(C:\\meme\\Res): 선물·원달러 실시간(JH0/JC0/YJC/FC9/DC0)의 코드 필드는
    ``futcode``뿐이고 ``shcode``가 없다 — WS 파서는 ``shcode``/``focode``를 보므로 body에
    ``shcode``를 같이 넣어 파서를 안 고친다(있으면 그대로).
    """
    key = next((str(fields[f]).strip() for f in key_fields if fields.get(f)), "")
    body = dict(fields)
    if key and not body.get("shcode"):
        body["shcode"] = key
    return json.dumps({"header": {"tr_cd": tr, "tr_key": key}, "body": body},
                      ensure_ascii=False)


class _NoConnection:
    """부모 생성자용 자리 — xing은 WS 연결을 쓰지 않는다."""

    async def send(self, text: str) -> None:  # pragma: no cover - 호출되지 않음
        raise RuntimeError("xing 실시간은 WS 전송을 쓰지 않는다")

    def __aiter__(self) -> AsyncIterator[str]:  # pragma: no cover
        raise RuntimeError("xing 실시간은 WS 수신을 쓰지 않는다")

    async def close(self) -> None:  # pragma: no cover
        return None


class _NoConnector:
    async def connect(self) -> WSConnection:  # pragma: no cover - 호출되지 않음
        raise RuntimeError("xing 실시간은 WS 연결을 쓰지 않는다")


def format_real_stats(delta: dict[str, int], top: int = 12) -> str:
    """1분 수신 통계 한 줄 — 총 건수 + 많은 TR부터 top개. 순수.
    (운영 PC 실측 2026-09-29: 메인창 수신 카운터가 멈춘 듯한데 로그에 시세는 안 찍혀 판별 불가 →
    TR별로 남긴다.)"""
    total = sum(delta.values())
    items = sorted(((tr, n) for tr, n in delta.items() if n), key=lambda x: -x[1])[:top]
    body = ", ".join(f"{tr} {n}" for tr, n in items) or "없음"
    return f"총 {total}건 — {body}"


def in_market_hours(t: time.struct_time) -> bool:
    """08:30~15:50(국내 정규장 전후) — 0건 경고를 낼 시간대. 순수."""
    hm = t.tm_hour * 60 + t.tm_min
    return 8 * 60 + 30 <= hm < 15 * 60 + 50


class XingRealClient(LSWebSocketClient):
    """XAReal 기반 실시간 — 콜백·파서·구독 등록은 LSWebSocketClient 그대로."""

    def __init__(
        self, session: XingSession, *,
        ensure_login: Callable[[], Awaitable[None]],
        etf_symbols: dict[Underlying, str] | None = None,
        status: WsStatus | None = None,
        clock: Callable[[], float] | None = None,
        reconnect_backoff_s: float = 2.0,
        stats_every_s: float = 60.0,
    ) -> None:
        super().__init__(_NoConnector(), etf_symbols=etf_symbols, clock=clock,
                         status=status or WsStatus(venue="LS", name="LS xing", kind="시세/주문",
                                                   expects_stream=True),
                         max_reconnects=1_000_000, reconnect_backoff_s=reconnect_backoff_s)
        self._session = session
        self._ensure_login = ensure_login
        self._down = asyncio.Event()  # 세션 끊김 신호
        self._stopped = False
        # TR별 수신 건수 — stats_every_s마다 그 사이 증가분을 로그(장중 0건이면 경고)
        self._real_counts: dict[str, int] = {}
        self._stats_every_s = stats_every_s
        self._running = False  # run()이 이미 도는 중 — 두 번째 run()은 대기만(아래)
        self._stop_event = asyncio.Event()
        session.on_real.append(self._on_real)
        session.on_session.append(self._on_session)

    # --- 실행 루프(부모 run 대체) ---

    async def run(self) -> None:
        """로그인 대기 → 전 구독 advise → 끊길 때까지 대기 → 반복. 데이터는 이벤트로 온다.

        xing은 한 세션이 주식·선물 계좌를 다 보므로 코어는 이 객체 하나를 stock_ws·deriv_ws 두
        자리에 넣는다(그래야 _wire가 선물 통보·FX를 구독한다). 그러면 run()이 두 번 불리는데,
        두 번째는 아무것도 하지 않고 정지까지 기다린다.
        """
        if self._running:
            await self._stop_event.wait()
            return
        self._running = True
        stats = asyncio.create_task(self._stats_loop()) if self._stats_every_s > 0 else None
        try:
            while not self._stopped:
                await self._ensure_login()
                self._down.clear()
                self.status.on_connect()
                await self._advise_all()
                if self.status.connects > 1:  # 재로그인(최초 아님) → 재동기 훅(부모와 같은 규칙)
                    for on_reconnect in self.on_reconnect:
                        on_reconnect()
                await self._down.wait()
                if self.status.connected:
                    self.status.on_disconnect()
                if self._reconnect_backoff_s > 0 and not self._stopped:
                    await asyncio.sleep(self._reconnect_backoff_s)
        finally:
            if stats is not None:
                stats.cancel()

    async def _stats_loop(self) -> None:
        """stats_every_s마다 그 사이 TR별 수신 건수를 INFO로, 장중(08:30~15:50) 0건이면 WARNING.
        (운영 PC 실측 2026-09-29: 카운터 정지 의심인데 로그로 판별 불가.)"""
        last: dict[str, int] = {}
        while not self._stopped:
            await asyncio.sleep(self._stats_every_s)
            now = dict(self._real_counts)
            delta = {tr: n - last.get(tr, 0) for tr, n in now.items()}
            last = now
            text = format_real_stats(delta)
            if sum(delta.values()) == 0 and in_market_hours(time.localtime()):
                log.warning("%s 실시간 %.0f초 수신 0건(장중) — 등록 %d건, 세션 %s",
                            self.status.name, self._stats_every_s, len(self._subs),
                            "연결" if self.status.connected else "끊김")
            else:
                log.info("%s 실시간 %.0f초 수신 %s", self.status.name, self._stats_every_s, text)

    def stop(self) -> None:
        self._stopped = True
        self._down.set()
        self._stop_event.set()

    async def _advise_all(self) -> None:
        for tr_cd, tr_key, _tr_type in self._subs:
            try:
                await self._session.advise(tr_cd, tr_key)
            except Exception as exc:  # noqa: BLE001 - 한 TR 실패(Res 없음 등)로 나머지를 막지 않음
                log.warning("%s 실시간 등록 실패 %s/%r — %s", self.status.name, tr_cd, tr_key, exc)
        log.info("%s 실시간 등록 %d건: %s", self.status.name, len(self._subs),
                 ", ".join(f"{tr}/{key!r}" if key else tr for tr, key, _t in self._subs))

    # --- 이벤트 → 부모 디스패치 ---

    def _on_real(self, tr: str, _key: str, fields: dict[str, str]) -> None:
        self.status.on_message(self._clock())
        self._real_counts[tr] = self._real_counts.get(tr, 0) + 1
        try:
            self._dispatch(frame_from_real(tr, fields))
        except Exception:  # noqa: BLE001 - 한 건 문제로 스트림을 죽이지 않음
            log.warning("xing 실시간 처리 실패 — 건너뜀 %s: %.300s", tr, fields, exc_info=True)

    def _on_session(self, kind: str, code: str, msg: str) -> None:
        if kind in ("disconnect", "logout"):
            log.warning("%s 세션 %s %s %s — 재로그인 대기", self.status.name, kind, code, msg)
            self._down.set()


def default_clock() -> float:
    return time.monotonic()
