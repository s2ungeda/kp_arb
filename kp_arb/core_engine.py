"""리허설 판정 루프 (BUILD_PLAN 7-3a) — 판정·주문 계획·가격까지 계산·기록, 발주 없음.

실시세로 자동 로직을 검증하는 단계: 세트 조건(진입 = est 스프레드 ≥ 기준값 /
청산 = ≤ 기준값)이 맞으면 실제 주문과 똑같이 계획·가격을 만들고 **로그에만**
남긴다. 가상 포지션·발주누적(fired_qty)·딜레이도 실주문과 동일하게 관리 —
7-3b에서 로그 자리에 발주가 들어간다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from .domain.enums import Instrument, SessionPhase, Venue
from .strategy_core import (
    Block,
    CoreState,
    Leg,
    ScreenState,
    SpreadSet,
    in_screen_operating_window,
    plan_order,
)

if TYPE_CHECKING:
    from .bootstrap import LiveSystem

TICK_S = 0.5  # 판정 주기 — WS 이벤트 기반 즉시 판정은 7-3b에서 (리허설은 주기면 충분)


@dataclass
class ScreenRuntime:
    """화면별 리허설 런타임 — 저장하지 않음(재시동 시 초기화)."""

    virtual_position: int = 0  # 가상 포지션(국내 단위) — entry +, exit −
    last_fire: dict[tuple[Block, int], float] = field(default_factory=dict)  # 딜레이용


def should_fire(block: Block, signal: float, threshold: float) -> bool:
    """세트 조건 판정 (§6.2-2): entry = 신호 ≥ 기준값, exit = 신호 ≤ 기준값."""
    if block is Block.ENTRY:
        return signal >= threshold
    return signal <= threshold


class RehearsalEngine:
    """리허설 엔진 — CoreState(세트)와 LiveSystem(시세)을 묶어 주기 판정."""

    def __init__(self, state: CoreState, system: LiveSystem) -> None:
        self._state = state
        self._system = system
        self.runtime = {kind: ScreenRuntime() for kind in state.screens}
        self._log = logging.getLogger("kp_arb.rehearsal")

    async def run(self) -> None:
        """주기 판정 루프 — 한 틱의 오류로 죽지 않는다."""
        import time

        while True:
            try:
                self.tick(datetime.now(), time.monotonic())
            except Exception:  # noqa: BLE001 - 기록하고 다음 틱
                self._log.exception("판정 틱 오류 — 계속")
            await asyncio.sleep(TICK_S)

    def tick(self, now: datetime, mono: float) -> None:
        """전 화면·세트 1회 판정. now/mono 주입 — 단위 테스트 가능."""
        for kind, screen in self._state.screens.items():
            if not in_screen_operating_window(screen, now.time()):
                continue
            if self._session_blocked(screen):
                continue
            entry_sig, exit_sig = self._system.pair_signal(
                screen.underlying, kind.counterpart, screen.per_order_qty)
            for block, signal in ((Block.ENTRY, entry_sig), (Block.EXIT, exit_sig)):
                if signal is None:
                    continue
                for index, spread_set in enumerate(screen.sets_of(block)):
                    self._judge_set(screen, block, index, spread_set,
                                    signal, now, mono)

    def _session_blocked(self, screen: ScreenState) -> bool:
        """세션 가드 — 데드존이면 판정 안 함 (사이드카/CB 코드 매핑은 실측 후)."""
        try:
            phase = self._system.session.phase_for(screen.underlying)
        except Exception:  # noqa: BLE001 - 세션 미구성(테스트 등)은 통과
            return False
        return phase is SessionPhase.DEAD

    def _judge_set(  # noqa: PLR0913 - 판정 문맥 전달
        self,
        screen: ScreenState,
        block: Block,
        index: int,
        spread_set: SpreadSet,
        signal: float,
        now: datetime,
        mono: float,
    ) -> None:
        if not spread_set.running or spread_set.threshold is None:
            return
        if not should_fire(block, signal, spread_set.threshold):
            return
        rt = self.runtime[screen.kind]
        last = rt.last_fire.get((block, index))
        if last is not None and (mono - last) * 1000.0 < screen.settings.delay_ms:
            return  # 딜레이 대기 (§6.2-6)
        plan, errors = plan_order(screen, block, index,
                                  rt.virtual_position, now.time())
        if plan is None:
            return  # 목표 완료/한도 소진 등 — 로그 없이 조용히 (매 틱 반복 방지)
        kr_qty = self._kr_qty(screen, plan.legs)
        est_bid, est_ask, px_entry, px_exit = self._system.est_pair_prices(
            screen.underlying, screen.kind.counterpart, screen.per_order_qty,
            spread_set.threshold if block is Block.ENTRY else 0.0,
            spread_set.threshold if block is Block.EXIT else 0.0)
        ls_price = px_entry if block is Block.ENTRY else px_exit
        hl_est = est_bid if block is Block.ENTRY else est_ask
        legs = " + ".join(f"{leg.venue.value} {leg.side.value} {leg.qty}"
                          for leg in plan.legs)
        self._log.info(
            "[리허설 발주] %s %s %d세트 | 신호 %.4f%% %s 기준 %.4f%% | %s | "
            "LS주문가 %s | HL est %s | 가상포지션 %d→%d",
            screen.kind.value, block.value, index + 1,
            signal * 100, "≥" if block is Block.ENTRY else "≤",
            spread_set.threshold * 100, legs,
            f"{ls_price:,.0f}" if ls_price is not None else "-",
            f"{hl_est:.4f}" if hl_est is not None else "-",
            rt.virtual_position,
            rt.virtual_position + (kr_qty if block is Block.ENTRY else -kr_qty),
        )
        spread_set.fired_qty += kr_qty
        rt.virtual_position += kr_qty if block is Block.ENTRY else -kr_qty
        rt.last_fire[(block, index)] = mono

    @staticmethod
    def _kr_qty(screen: ScreenState, legs: tuple[Leg, ...]) -> int:
        """계획의 국내 수량 — LS 다리가 있으면 그 수량, HL만이면 역환산."""
        for leg in legs:
            if leg.venue is Venue.LS:
                return leg.qty
        ratio = 10 if screen.kind.counterpart is Instrument.KR_STOCK_FUTURE else 1
        return legs[0].qty // ratio
