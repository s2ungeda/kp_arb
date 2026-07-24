"""FX 노출 보고 서비스 — SignalLink 전송 루프 + 감시 화면용 상태·제어 (7-4).

델파이 FChatMonitor 대응: 자동 송신(주기)·일시정지/재개·수동 송신·피어 목록·로그.
코어 안에서 돌고, 감시 화면(fx_monitor)은 코어 /state·/command로 읽고 제어한다.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

from .fx import hl_coin_notional
from .fx_reporter import FXExposureReporter, Signal
from .signallink import SignalLinkSink

if TYPE_CHECKING:
    from .bootstrap import LiveSystem

log = logging.getLogger("kp_arb.fx_service")
TICK_S = 0.5  # 루프 틱 — 주기·수동 송신 요청을 이 간격으로 확인


class FxReportService:
    """HL 명목·환율을 외부 #2로 보고 + 상태 노출·제어."""

    def __init__(
        self, system: LiveSystem, *,
        interval_s: float = 10.0, system_name: str = "kp-arb",
    ) -> None:
        self._system = system
        # 수신은 token="Meme"만 처리(sink가 필터) — 받은 메시지는 로그에 남긴다
        self._sink = SignalLinkSink(system_name=system_name, on_message=self._on_message)
        self._reporter = FXExposureReporter(
            self._sink, token="Meme", notional_fn=hl_coin_notional)
        self.interval_s = interval_s
        self.paused = False
        self._want_send_now = False
        self.last_signal: Signal | None = None
        self.last_sent_ok: bool | None = None
        self.last_hl: list[dict[str, object]] = []  # total_coin 구성(HL 종목별)
        self.log: deque[str] = deque(maxlen=200)

    # --- 제어 (감시 화면 명령 — 코어 이벤트 루프에서 호출, 동기 안전) ---

    def pause(self) -> None:
        self.paused = True
        self._note("자동 송신 일시정지")

    def resume(self) -> None:
        self.paused = False
        self._note("자동 송신 재개")

    def set_interval(self, seconds: float) -> None:
        self.interval_s = max(0.5, seconds)
        self._note(f"자동 송신 주기 {self.interval_s:g}초")

    def request_send_now(self) -> None:
        """수동 송신 요청 — 다음 틱(≤0.5초)에 값 변화 무관 강제 전송."""
        self._want_send_now = True

    def clear_log(self) -> None:
        self.log.clear()

    # --- 실행 루프 ---

    async def run(self) -> None:
        await self._sink.start()
        elapsed = 0.0
        try:
            while True:
                await asyncio.sleep(TICK_S)
                elapsed += TICK_S
                if self._want_send_now:
                    self._want_send_now = False
                    await self._send(force=True)
                    elapsed = 0.0
                elif not self.paused and elapsed >= self.interval_s:
                    elapsed = 0.0
                    await self._send(force=False)
        except asyncio.CancelledError:
            await self._sink.stop()
            raise

    async def _send(self, *, force: bool) -> None:
        # 델파이 원본과 동일 — 주기마다 무조건 전송(값 변화와 무관). force는 로그 라벨용.
        try:
            from .domain.enums import Instrument

            positions = await self._hl_positions()  # HL 계좌 직접 조회 (수동 매매 포함)
            fx, _ = self._system.usdkrw_effective()
            rate = fx or 0.0
            # total_coin 구성 — HL 종목별 (평균단가×수량×환율=원화 명목), 화면 표시용
            self.last_hl = [
                {"underlying": p.underlying.value, "qty": p.qty, "avg": p.avg_price,
                 "notional": p.avg_price * p.qty * rate}
                for p in positions if p.instrument is Instrument.HL_PERP
            ]
            sent = await self._reporter.report(positions, fx or 0.0)
            self.last_signal = sent
            self.last_sent_ok = self._reporter.last_sent_ok
            self._note(f"{'수동' if force else '자동'} 송신 "
                       f"total_coin={sent.total_coin:.0f} fx={sent.fx:.2f} "
                       f"ok={self.last_sent_ok}")
        except Exception:  # noqa: BLE001 - 보고 실패가 코어를 멈추지 않게
            log.exception("FX 보고 실패 — 계속")

    async def _hl_positions(self) -> list[Any]:
        """HL 보유 — 계좌 직접 조회(REST) 우선, 실패 시 OrderBook.

        웹사이트 등 우리 시스템 밖에서 연 포지션도 반영하려면 매번 실계좌를 읽어야 한다.
        """
        hl = getattr(self._system, "_hl", None)
        if hl is not None and hasattr(hl, "get_positions"):
            try:
                return list(await hl.get_positions())
            except Exception:  # noqa: BLE001 - 조회 실패 시 OrderBook으로 폴백
                log.warning("HL 포지션 조회 실패 — OrderBook 사용", exc_info=True)
        return self._system.order_book.positions()

    def _on_message(self, name: str, payload: dict[str, object]) -> None:
        """수신 메시지(token=Meme만 도달) → 로그. total_coin·fx만 요약."""
        self._note(f"수신 {name}: total_coin={payload.get('total_coin')} "
                   f"fx={payload.get('fx')}")

    def _note(self, message: str) -> None:
        import time

        line = f"{time.strftime('%H:%M:%S')} {message}"
        self.log.append(line)
        log.info("FX 보고: %s", message)

    # --- 감시 화면용 스냅샷 ---

    def snapshot(self) -> dict[str, Any]:
        last = None
        if self.last_signal is not None:
            last = {
                "id": self.last_signal.id,
                "total_coin": self.last_signal.total_coin,
                "fx": self.last_signal.fx,
                "datetime": self.last_signal.datetime,
                "ok": self.last_sent_ok,
            }
        return {
            "paused": self.paused,
            "interval_s": self.interval_s,
            "peers": self._sink.peer_list(),
            "last": last,
            "hl": self.last_hl,  # total_coin 구성 (HL 종목별) — 0일 때 원인 파악용
            "log": list(self.log)[-60:],  # /state 크기 억제 — 최근 60줄만
        }
