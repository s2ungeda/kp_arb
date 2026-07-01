"""FXExposureReporter — 외부 #2 환헤지 프로세스로 USD 노출 보고 (DESIGN.md §5.7, §9).

- USD(USDC) 순노출은 ``fx.usd_exposure``로 계산(HL perp USD 명목 합).
- 발행 채널은 ``ExposureSink``(Protocol) 뒤로 격리. 실제 프로토콜(ZeroMQ/gRPC/TCP-JSON)은
  [OPEN §13 #2] → 여기선 mock sink로 두고 라이브에서 채운다.
- **이 시스템은 USD/KRW 선물 주문·계좌를 갖지 않는다** — 노출 계산·보고만 한다.
- 메시지: {source_id, exposure_usd, ts}. 변동 시에만 재발행하는 헬퍼 제공.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Protocol

from pydantic import BaseModel

from .domain.enums import Underlying
from .domain.models import Position
from .fx import usd_exposure


class ExposureReport(BaseModel):
    """외부 #2로 발행하는 노출 메시지."""

    source_id: str
    exposure_usd: float
    ts: float


class ExposureSink(Protocol):
    """노출 발행 채널 계약. 라이브는 IPC 구현, 테스트는 mock. 반환은 sent_ok."""

    async def publish(self, report: ExposureReport) -> bool: ...


class FXExposureReporter:
    def __init__(
        self,
        sink: ExposureSink,
        *,
        source_id: str,
        clock: Callable[[], float] = time.time,
        min_change: float = 0.0,
    ) -> None:
        self._sink = sink
        self._source_id = source_id
        self._clock = clock
        self._min_change = min_change
        self._last_exposure: float | None = None
        self.last_sent_ok: bool | None = None

    async def report(
        self,
        positions: Iterable[Position],
        marks: dict[Underlying, float] | None = None,
        *,
        ts: float | None = None,
    ) -> ExposureReport:
        """USD 순노출 계산 → 외부 #2로 발행. 발행 결과는 last_sent_ok에 기록."""
        exposure = usd_exposure(positions, marks)
        report = ExposureReport(
            source_id=self._source_id,
            exposure_usd=exposure,
            ts=ts if ts is not None else self._clock(),
        )
        self.last_sent_ok = await self._sink.publish(report)
        self._last_exposure = exposure
        return report

    async def report_if_changed(
        self,
        positions: Iterable[Position],
        marks: dict[Underlying, float] | None = None,
        *,
        ts: float | None = None,
    ) -> ExposureReport | None:
        """노출이 min_change 초과로 바뀐 경우에만 발행. 아니면 None(발행 안 함)."""
        exposure = usd_exposure(positions, marks)
        if self._last_exposure is not None:
            if abs(exposure - self._last_exposure) <= self._min_change:
                return None
        return await self.report(positions, marks, ts=ts)
