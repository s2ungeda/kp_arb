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
        interval_s: float = 2.0, system_name: str = "kp-arb",
    ) -> None:
        self._system = system
        self._sink = SignalLinkSink(system_name=system_name)
        self._reporter = FXExposureReporter(
            self._sink, token="Meme", notional_fn=hl_coin_notional)
        self.interval_s = interval_s
        self.paused = False
        self._want_send_now = False
        self.last_signal: Signal | None = None
        self.last_sent_ok: bool | None = None
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
        try:
            positions = self._system.order_book.positions()
            fx, _ = self._system.usdkrw_effective()
            if force:
                sent: Signal | None = await self._reporter.report(positions, fx or 0.0)
            else:
                sent = await self._reporter.report_if_changed(positions, fx or 0.0)
            if sent is not None:
                self.last_signal = sent
                self.last_sent_ok = self._reporter.last_sent_ok
                self._note(f"{'수동' if force else '자동'} 송신 "
                           f"total_coin={sent.total_coin:.0f} fx={sent.fx:.2f} "
                           f"ok={self.last_sent_ok}")
        except Exception:  # noqa: BLE001 - 보고 실패가 코어를 멈추지 않게
            log.exception("FX 보고 실패 — 계속")

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
            "log": list(self.log),
        }
