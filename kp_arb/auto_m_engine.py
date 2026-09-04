"""자동M 코어 결선 — 순수 상태기계(auto_m)를 실시세·발주·체결통보에 붙인다 (②단계).

- 100ms마다 정방향 3세트의 진입·청산 다리를 판정(``evaluate``)하고, 나온 Action을 실제
  주문(LiveSystem.place/cancel)으로 옮긴다. 선주문 = 국내 SF 지정가(maker, source "자동M"),
  후주문 = HL 즉시체결(IOC, 체결 계약 × 10).
- 체결·취소·거부는 OrderBook 통보(on_fill_applied·on_change)로 받아 ``on_*``에 넣는다.
- 중지(체결차·후주문 거부)는 에러 알람 카운터(error_seq)를 올려 메인창이 소리를 내고,
  화면은 상태(HALTED)를 보고 세트 행을 검게 칠한다(DESIGN-auto-m §9a).
- 시장 정지(exec §8)는 세션의 선물시장(5) 정지 오버레이로 본다. 풀리면 재개 딜레이.

시스템 의존은 ``_SystemLike``로 좁혀 두어 테스트는 가짜 시스템으로 돌린다(라이브 호출 없음).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from .auto_m import (
    HL_PER_SF,
    Action,
    AutoMScreen,
    AutoMSet,
    LegStatus,
    Signals,
    evaluate,
    on_post_fill,
    on_post_partial_reject,
    on_post_reject,
    on_pre_ack,
    on_pre_cancelled,
    on_pre_fill,
    on_pre_reject,
    order_qty,
    release_halt,
    set_running,
)
from .disparity import disp, est_price
from .domain.enums import Block, Instrument, OrderType, Side, Underlying, Venue
from .domain.models import OrderIntent, Quote
from .hl_merge import merge_tick_options

if TYPE_CHECKING:
    from .order_book import OrderBook, TrackedOrder
    from .strategy_core import CoreState

TICK_S = 0.1
SOURCE = "자동M"


class _SystemLike(Protocol):
    """엔진이 쓰는 LiveSystem의 일부 — 테스트용 가짜가 같은 모양을 흉내 낸다."""

    order_book: OrderBook
    quotes: dict[tuple[Underlying, Instrument, str], Quote]
    error_seq: int

    def pair_signal(self, u: Underlying, instrument: Instrument, entry_qty: int,
                    exit_qty: int) -> tuple[float | None, float | None]: ...
    def stock_futures_theory(self, underlying: Underlying,
                             instrument: Instrument = ...) -> float | None: ...
    def stock_last(self, underlying: Underlying) -> float | None: ...
    def usdkrw_effective(self, now: datetime | None = None) -> tuple[float | None, str]: ...
    def fx_entry_rate(self, side: Side) -> float | None: ...
    def futures_halted(self) -> bool: ...
    async def place(self, intent: OrderIntent) -> str: ...
    async def cancel(self, order_id: str) -> None: ...


@dataclass(frozen=True)
class _OrderRef:
    index: int
    block: Block
    leg: str  # "pre" | "post"


class AutoMEngine:
    """자동M 실행 엔진 — CoreState.autom(세트·설정)과 LiveSystem을 묶는다."""

    def __init__(self, state: CoreState, system: _SystemLike) -> None:
        self._state = state
        self._system = system
        self._log = logging.getLogger("kp_arb.autom")
        self._orders: dict[str, _OrderRef] = {}
        self._seen_status: dict[str, str] = {}
        self._halt_since: float | None = None
        self._resumed_mono: float | None = None
        self._bg: set[asyncio.Task[None]] = set()
        system.order_book.on_fill_applied.append(self._on_fill_applied)
        system.order_book.on_change.append(self._on_book_change)

    # ----------------------------------------------------------------- 편의 ---
    @property
    def screen(self) -> AutoMScreen:
        return self._state.autom

    def _underlying(self) -> Underlying:
        from .strategy_core import ScreenKind

        return self._state.screens[ScreenKind.AUTO_M].underlying

    def _counterpart(self) -> Instrument:
        from .strategy_core import ScreenKind

        return self._state.screens[ScreenKind.AUTO_M].counterpart

    # ------------------------------------------------------------ 판정 루프 ---
    async def run(self) -> None:
        while True:
            try:
                self.tick(datetime.now(), time.monotonic())
            except Exception:  # noqa: BLE001 - 한 틱의 오류로 죽지 않는다
                self._log.exception("자동M 판정 틱 오류 — 계속")
            await asyncio.sleep(TICK_S)

    def tick(self, now: datetime, mono: float) -> None:
        """전 세트·다리 1회 판정(now/mono 주입 — 테스트 가능)."""
        halted = self._system.futures_halted()
        if halted:
            self._halt_since = mono
            self._resumed_mono = None
        elif self._halt_since is not None:
            self._halt_since = None
            self._resumed_mono = mono
        for index, s in enumerate(self.screen.sets):
            for block in (Block.ENTRY, Block.EXIT):
                leg = s.leg(block)
                if not leg.running and leg.status is LegStatus.IDLE:
                    continue
                sig = self.build_signals(s, block, now, mono, halted)
                self._apply(index, block, evaluate(
                    s, block, sig, self.screen.settings, self._underlying()))

    def build_signals(self, s: AutoMSet, block: Block, now: datetime, mono: float,
                      halted: bool) -> Signals:
        """세트·다리 하나의 판정 입력 — 수량은 이번에 낼 계약수(없으면 걸어둔 수량)."""
        u, inst = self._underlying(), self._counterpart()
        qty = order_qty(block, s.per_qty, s.target_qty, s.rt) or s.leg(block).pre_qty or 1
        sf_entry, sf_exit = self._system.pair_signal(u, inst, qty, qty)
        s_entry, _ = self._system.pair_signal(u, Instrument.KR_STOCK, qty * HL_PER_SF, 0)
        hl = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        fx, _src = self._system.usdkrw_effective(now)
        stock = self._system.stock_last(u)
        hl_bid_d = hl_ask_d = None
        if hl is not None and fx is not None and stock:
            est_bid = est_price(hl.bids or [(hl.bid, hl.bid_qty or 1.0)], qty * HL_PER_SF)
            est_ask = est_price(hl.asks or [(hl.ask, hl.ask_qty or 1.0)], qty * HL_PER_SF)
            hl_bid_d = disp(est_bid * fx if est_bid else None, stock)
            hl_ask_d = disp(est_ask * fx if est_ask else None, stock)
        sf_quote = next((self._system.quotes.get((u, inst, m)) for m in ("uni", "krx", "nxt")
                         if self._system.quotes.get((u, inst, m)) is not None), None)
        asks: tuple[tuple[float, float], ...] = ()
        bids: tuple[tuple[float, float], ...] = ()
        if sf_quote is not None:
            asks = tuple(sf_quote.asks or [(sf_quote.ask, sf_quote.ask_qty or 1.0)])
            bids = tuple(sf_quote.bids or [(sf_quote.bid, sf_quote.bid_qty or 1.0)])
        return Signals(
            now=now, mono=mono,
            sf_spread_entry=sf_entry, s_spread_entry=s_entry, sf_spread_exit=sf_exit,
            hl_disp_bid=hl_bid_d, hl_disp_ask=hl_ask_d,
            sf_theory=self._system.stock_futures_theory(u, inst), stock_last=stock,
            sf_asks=asks, sf_bids=bids, market_halted=halted,
            resumed_mono=self._resumed_mono)

    # ------------------------------------------------------------ 행동 실행 ---
    def _apply(self, index: int, block: Block, actions: list[Action]) -> None:
        for act in actions:
            if act.kind == "place_pre":
                self._spawn(self._place_pre(index, block, act))
            elif act.kind == "cancel_pre" and act.order_id:
                self._spawn(self._cancel_pre(index, block, act.order_id, act.reason))
            elif act.kind == "place_post":
                self._spawn(self._place_post(index, block, act))
            elif act.kind == "halt":
                self._log.error("[자동M] %d세트 %s 중지 — %s", index + 1, block.value, act.reason)
                self._system.error_seq += 1  # 메인창 에러 알람 소리(공통설정)
            elif act.kind == "notify":
                self._log.warning("[자동M] %d세트 %s — %s", index + 1, block.value, act.reason)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _place_pre(self, index: int, block: Block, act: Action) -> None:
        s = self.screen.sets[index]
        u, inst = self._underlying(), self._counterpart()
        assert act.side is not None and act.price is not None
        intent = OrderIntent(venue=Venue.LS, underlying=u, instrument=inst, side=act.side,
                             qty=act.qty, order_type=OrderType.LIMIT, price=act.price,
                             source=SOURCE)
        try:
            oid = await self._system.place(intent)
        except Exception as exc:  # noqa: BLE001 - 거부/오류 → 딜레이 뒤 재시도(exec ㄴ5)
            self._log.warning("[자동M] 선주문 실패 %d세트 %s — %s", index + 1, block.value, exc)
            self._apply(index, block, on_pre_reject(
                s, block, time.monotonic(), self.screen.settings))
            return
        self._orders[oid] = _OrderRef(index, block, "pre")
        on_pre_ack(s, block, oid)
        self._log.info("[자동M] 선주문 %d세트 %s %s %d @ %g → #%s",
                       index + 1, block.value, act.side.value, act.qty, act.price, oid)

    async def _cancel_pre(self, index: int, block: Block, order_id: str, reason: str) -> None:
        self._log.info("[자동M] 선주문 취소 %d세트 %s #%s %s", index + 1, block.value,
                       order_id, reason)
        try:
            await self._system.cancel(order_id)
        except Exception as exc:  # noqa: BLE001 - 이미 체결/취소됐으면 통보로 정리된다
            self._log.warning("[자동M] 취소 실패 #%s — %s", order_id, exc)

    async def _place_post(self, index: int, block: Block, act: Action) -> None:
        s = self.screen.sets[index]
        u = self._underlying()
        assert act.side is not None
        intent = OrderIntent(venue=Venue.HYPERLIQUID, underlying=u, instrument=Instrument.HL_PERP,
                             side=act.side, qty=act.qty, order_type=OrderType.MARKET,
                             source=SOURCE)
        try:
            oid = await self._system.place(intent)
        except Exception as exc:  # noqa: BLE001 - 후주문 거부 → 체결차 → 중지(exec ㄹ2)
            self._log.error("[자동M] 후주문 실패 %d세트 %s — %s", index + 1, block.value, exc)
            self._apply(index, block, on_post_reject(s, block, str(exc)[:80]))
            return
        self._orders[oid] = _OrderRef(index, block, "post")
        self._log.info("[자동M] 후주문 %d세트 %s HL %s %d → #%s",
                       index + 1, block.value, act.side.value, act.qty, oid)

    # ------------------------------------------------------------ 주문 통보 ---
    def _on_fill_applied(self, order: TrackedOrder, qty: float, price: float,
                         _fill_id: str) -> None:
        ref = self._orders.get(order.order_id)
        if ref is None:
            return
        s = self.screen.sets[ref.index]
        mono = time.monotonic()
        if ref.leg == "pre":
            self._apply(ref.index, ref.block,
                        on_pre_fill(s, ref.block, int(round(qty)), price, mono))
        else:
            fx = self._system.fx_entry_rate(order.intent.side) or 0.0
            self._apply(ref.index, ref.block, on_post_fill(
                s, ref.block, qty, price, fx, mono, self.screen.settings))

    def _on_book_change(self) -> None:
        """취소·거부는 상태 변화로 온다 — 추적 주문의 상태 전이를 한 번씩 처리."""
        for oid, ref in list(self._orders.items()):
            order = self._system.order_book.order(oid)
            if order is None:
                continue
            status = order.status.value
            if self._seen_status.get(oid) == status:
                continue
            self._seen_status[oid] = status
            s = self.screen.sets[ref.index]
            mono = time.monotonic()
            if ref.leg == "pre":
                if status == "cancelled":
                    on_pre_cancelled(s, ref.block, mono, self.screen.settings)
                    self._forget(oid)
                elif status == "rejected":
                    self._apply(ref.index, ref.block,
                                on_pre_reject(s, ref.block, mono, self.screen.settings))
                    self._forget(oid)
                elif status == "filled":
                    self._forget(oid)
            elif status in ("cancelled", "rejected"):  # 후주문 IOC 잔량·거부 → 체결차
                unfilled = int(round(order.intent.qty - order.filled_qty))
                if unfilled > 0:
                    self._apply(ref.index, ref.block,
                                on_post_partial_reject(s, ref.block, unfilled))
                self._forget(oid)
            elif status == "filled":
                self._forget(oid)

    def _forget(self, oid: str) -> None:
        self._orders.pop(oid, None)
        self._seen_status.pop(oid, None)

    # ---------------------------------------------------------------- 명령 ---
    def set_running(self, index: int, block: Block, value: bool) -> None:
        self._apply(index, block, set_running(self.screen.sets[index], block, value))

    def release(self, index: int, block: Block) -> None:
        release_halt(self.screen.sets[index], block)

    def stop_all(self) -> None:
        """전 세트 실행 해제(창 닫기·안전종료) — 미체결 선주문 취소."""
        for index in range(len(self.screen.sets)):
            for block in (Block.ENTRY, Block.EXIT):
                self.set_running(index, block, False)

    # ------------------------------------------------------------- 스냅샷 ---
    def live_snapshot(self) -> dict[str, Any]:
        """화면용 — 세트별 상태·RT·체결차·누적(Sprd는 실시간 주식가·이론가로 계산)."""
        u, inst = self._underlying(), self._counterpart()
        stock = self._system.stock_last(u)
        theory = self._system.stock_futures_theory(u, inst)
        # 상단 모니터 3칸(§9): 기준수량 est 괴리. 정방향 진입 = HL 매수호가창 est,
        # 청산 = 매도호가창; 역방향은 반대(진입 = 매도호가창, 청산 = 매수호가창).
        q = max(1, self.screen.ref_qty)
        sf_en, sf_ex = self._system.pair_signal(u, inst, q, q)
        s_en, s_ex = self._system.pair_signal(u, Instrument.KR_STOCK, q * HL_PER_SF, q * HL_PER_SF)
        monitor = {"fwd": {"en_sf": sf_en, "en_s": s_en, "ex_sf": sf_ex},
                   "rev": {"en_sf": sf_ex, "en_s": s_ex, "ex_sf": sf_en}}
        out = []
        for s in self.screen.sets:
            row: dict[str, Any] = {"rt": s.rt, "fill_diff": s.fill_diff}
            for name, leg in (("entry", s.entry), ("exit", s.exit)):
                row[name] = {
                    "running": leg.running, "status": leg.status.value,
                    "halt_reason": leg.halt_reason, "pre_order_id": leg.pre_order_id,
                    "pre_price": leg.pre_price, "pre_qty": leg.pre_qty,
                    "pre_filled": leg.pre_filled, "post_pending": leg.post_pending,
                    "hl_qty": leg.acc.hl_qty, "sf_qty": leg.acc.sf_qty,
                    "fx_avg": leg.acc.fx_avg(), "sprd": leg.acc.sprd(stock, theory),
                }
            out.append(row)
        # HL 호가단위(틱) 옵션 — 일반주문창과 같은 표(가격 자릿수 기반, 코어 계산 §5.10)
        hl = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        ref = (hl.ask or hl.bid) if hl is not None else None
        merge_ticks = ([{"tick": s, "n_sig_figs": nsf, "mantissa": mant}
                        for s, nsf, mant in merge_tick_options(float(ref))] if ref else [])
        active_fn = getattr(self._system, "hl_merge_active", None)
        active = active_fn(u) if callable(active_fn) else None
        return {"sets": out, "any_running": self.screen.any_running(), "monitor": monitor,
                "hl_merge_ticks": merge_ticks,
                "hl_merge_active": ({"n_sig_figs": active[0], "mantissa": active[1]}
                                    if active is not None else None)}
