"""FXExposureReporter — 국내 노출 데이터를 외부 #2로 전송(보고) (DESIGN.md §5.7, §9).

- 전송 값: 국내 롱 다리 KRW 명목(`total_coin`) + 환율(`fx`). USD 환산·환헤지는 #2가 수행.
- 본 시스템은 USD/KRW 선물 주문·계좌를 갖지 않는다(전송만).
- 채널: 기존 `SignalLink`(UDP 8888 발견 + TCP) 재사용 — 실제 소켓 결선은 라이브(Phase 6).
  여기선 `ExposureSink`(Protocol) 뒤로 격리하고 테스트는 mock sink만 사용.
- 메시지 = `Signal{id, fx, total_domestic=0, total_coin, token, datetime}` (기존 스키마).
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Protocol

from pydantic import BaseModel

from .domain.enums import Instrument
from .domain.models import Position
from .fx import domestic_krw_notional


class Signal(BaseModel):
    """외부 #2로 전송하는 노출 메시지(기존 SignalLink 스키마)."""

    id: str
    fx: float
    total_domestic: float = 0.0
    total_coin: float = 0.0
    token: str = ""
    datetime: str = ""


class ExposureSink(Protocol):
    """노출 전송 채널 계약(라이브=SignalLink, 테스트=mock). 반환은 sent_ok."""

    async def send(self, signal: Signal) -> bool: ...


class FXExposureReporter:
    def __init__(
        self,
        sink: ExposureSink,
        *,
        token: str = "",
        multipliers: Mapping[Instrument, float] | None = None,
        min_change: float = 0.0,
    ) -> None:
        self._sink = sink
        self._token = token
        self._multipliers = multipliers
        self._min_change = min_change
        self._last_total_coin: float | None = None
        self.last_sent_ok: bool | None = None

    async def report(
        self,
        positions: Iterable[Position],
        fx: float,
        *,
        id: str | None = None,
        datetime: str = "",
    ) -> Signal:
        """국내 KRW 명목(total_coin) + 환율을 계산해 #2로 전송."""
        total_coin = domestic_krw_notional(positions, self._multipliers)
        signal = Signal(
            id=id if id is not None else uuid.uuid4().hex,
            fx=fx,
            total_domestic=0.0,
            total_coin=total_coin,
            token=self._token,
            datetime=datetime,
        )
        self.last_sent_ok = await self._sink.send(signal)
        self._last_total_coin = total_coin
        return signal

    async def report_if_changed(
        self,
        positions: Iterable[Position],
        fx: float,
        *,
        id: str | None = None,
        datetime: str = "",
    ) -> Signal | None:
        """total_coin이 min_change 초과로 바뀐 경우에만 전송. 아니면 None."""
        total_coin = domestic_krw_notional(positions, self._multipliers)
        if self._last_total_coin is not None:
            if abs(total_coin - self._last_total_coin) <= self._min_change:
                return None
        return await self.report(positions, fx, id=id, datetime=datetime)
