"""FXExposureReporter — 노출 데이터를 외부 #2로 전송(보고) (DESIGN.md §5.7, §9).

- 전송 값(사용자 확정 2026-07-24): `total_coin` = **HL 보유종목 Σ(평균단가×수량)**,
  `total_domestic`=0, `token`="Meme", `fx`=환율. USD 환헤지는 #2가 수행.
- 본 시스템은 USD/KRW 선물 주문·계좌를 갖지 않는다(전송만).
- 채널: Dalin broadcast(UDP 8888 피어 발견 + TCP 전송) — `signallink.SignalLinkSink`.
  여기선 `ExposureSink`(Protocol) 뒤로 격리하고 순수 로직은 mock sink로 테스트.
- 메시지 = `Signal{id, fx, total_domestic=0, total_coin, token, datetime}` (기존 스키마).
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from typing import Protocol

from pydantic import BaseModel

from .domain.models import Position
from .fx import hl_coin_notional


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
        token: str = "Meme",
        notional_fn: Callable[[Iterable[Position]], float] = hl_coin_notional,
        min_change: float = 0.0,
    ) -> None:
        self._sink = sink
        self._token = token
        self._notional_fn = notional_fn  # total_coin 계산 (기본: HL 명목)
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
        """HL 명목(total_coin) + 환율을 #2로 전송.

        total_coin = Σ(HL 평균단가 × 수량) — USD 명목 그대로(환율 안 곱함,
        사용자 확정 2026-07-24). fx는 항상 1로 전송(#2가 환산 안 함).
        """
        total_coin = self._notional_fn(list(positions))
        signal = Signal(
            id=id if id is not None else uuid.uuid4().hex,
            fx=1.0,  # 항상 1 — #2가 환산 안 함 (사용자 확정 2026-07-24)
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
        pos_list = list(positions)
        total_coin = self._notional_fn(pos_list)
        if (self._last_total_coin is not None
                and abs(total_coin - self._last_total_coin) <= self._min_change):
            return None
        return await self.report(pos_list, fx, id=id, datetime=datetime)
