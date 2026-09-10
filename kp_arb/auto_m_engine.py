"""자동M 코어 결선 — 순수 상태변화(auto_m)를 실시세·발주·체결통보에 붙인다 (②단계).

- 100ms마다 **종목별로**(삼성·하이닉스… 각각 독립, 사용자 확정 2026-09-08) 정방향 3세트의
  진입·청산을 각각 판정(``evaluate``)하고, 나온 Action을 실제 주문(LiveSystem.place/cancel)으로
  옮긴다. 선주문 = 국내 SF 지정가(maker, source "자동M"), 후주문 = HL 지정가(Gtc, 상대 1호가
  ± HP 여유, 체결 계약 × 10 — IOC·FOK 안 씀).
- 체결·취소·거부는 OrderBook 통보(on_fill_applied·on_change)로 받아 ``on_*``에 넣는다.
- 중지(체결차·후주문 거부)는 에러 알람 카운터(error_seq)를 올려 메인창이 소리를 내고,
  화면은 상태(HALTED)를 보고 세트 행을 검게 칠한다(exec §10).
- 시장 정지(exec §8)는 세션의 선물시장(5) 정지 오버레이로 본다. 풀리면 재개 딜레이.

시스템 의존은 ``_SystemLike``로 좁혀 두어 테스트는 가짜 시스템으로 돌린다(라이브 호출 없음).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .auto_m import (
    HL_PER_SF,
    Action,
    AutoMBook,
    AutoMScreen,
    AutoMSet,
    Leg,
    LegStatus,
    Signals,
    evaluate,
    on_post_fill,
    on_post_partial_reject,
    on_post_reject,
    on_pre_ack,
    on_pre_cancel_failed,
    on_pre_cancelled,
    on_pre_fill,
    on_pre_reject,
    order_qty,
    release_halt,
    set_running,
)
from .disparity import disp, est_price
from .domain.enums import Block, Instrument, OrderType, Side, Underlying, Venue
from .domain.models import InstrumentInfo, OrderIntent, Quote
from .hl_merge import merge_tick_options
from .hl_price import hl_round_price
from .logs import attach_daily_file

if TYPE_CHECKING:
    from .order_book import OrderBook, TrackedOrder
    from .strategy_core import CoreState

TICK_S = 0.1
SOURCE = "자동M"


class _SystemLike(Protocol):
    """엔진이 쓰는 LiveSystem의 일부 — 테스트용 가짜가 같은 모양을 흉내 낸다."""

    order_book: OrderBook
    quotes: dict[tuple[Underlying, Instrument, str], Quote]
    instruments: dict[tuple[Underlying, Instrument], InstrumentInfo]  # HL szDecimals(가격 격자)
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
    underlying: Underlying
    index: int
    block: Block
    leg: str  # "pre" | "post"


class AutoMEngine:
    """자동M 실행 엔진 — CoreState.autom(종목별 세트·공통설정)과 LiveSystem을 묶는다."""

    def __init__(self, state: CoreState, system: _SystemLike,
                 log_dir: Path | None = None,
                 save: Callable[[], None] | None = None) -> None:
        self._state = state
        self._system = system
        # 상태 저장 훅(core_state.json) — RT·체결차·순잔고는 체결로 바뀌므로 명령 때만 저장하면
        # 재시동 때 잃는다. 체결·취소·중지 뒤마다 저장(사용자 2026-09-07: 재접속 때 물고 옴).
        self._save = save
        self._log = logging.getLogger("kp_arb.autom")  # 굵직한 줄 → 코어 로그
        self._log_dir = log_dir
        self._orders: dict[str, _OrderRef] = {}
        # 등록 전에 온 우리 주문 체결(발주 응답 즉시체결) — oid → [(주문, 수량, 가격)], 등록 때 반영
        self._orphan_fills: dict[str, list[tuple[TrackedOrder, float, float]]] = {}
        self._seen_status: dict[str, str] = {}
        self._mono = 0.0  # 마지막 tick의 단조 시계(테스트 주입 가능)
        # 종목별 상세 로그(logs/autom_<종목>_날짜.log) — 판정 근거·상태 전이·체결 반영
        # (사용자 2026-09-04). 판정·상태는 바뀔 때만 한 줄.
        self._logged_reason: dict[tuple[Underlying, int, Block], str] = {}
        self._logged_status: dict[tuple[Underlying, int, Block], str] = {}
        self._halt_since: float | None = None
        self._resumed_mono: float | None = None
        self._bg: set[asyncio.Task[None]] = set()
        system.order_book.on_fill_applied.append(self._on_fill_applied)
        system.order_book.on_change.append(self._on_book_change)
        self._log_restored_halts()

    def _log_restored_halts(self) -> None:
        """재시동 복원된 중지 세트를 로그에 남긴다(2026-09-10: 중지는 재시동 뒤에도 유지)."""
        for key, book in self.screen.books.items():
            u = Underlying(key)
            for index, s in enumerate(book.sets):
                for block in (Block.ENTRY, Block.EXIT):
                    leg = s.leg(block)
                    if leg.status is LegStatus.HALTED:
                        self.ulog(u).warning("복원 %s: 중지 유지(%s) | %s — 정리 뒤 화면에서 해제",
                                             self._tag(u, index, block), leg.halt_reason,
                                             self._ledger(s))

    # ----------------------------------------------------------------- 편의 ---
    @property
    def screen(self) -> AutoMScreen:
        return self._state.autom

    def _book(self, u: Underlying) -> AutoMBook:
        return self.screen.book(u)

    @staticmethod
    def _counterpart(book: AutoMBook) -> Instrument:
        return book.counterpart

    def ulog(self, u: Underlying) -> logging.Logger:
        """종목의 상세 로거 — logs/autom_<종목>_날짜.log (자정 롤오버, 코어 로그와 분리)."""
        return attach_daily_file(f"kp_arb.autom.{u.value}", f"autom_{u.value}", self._log_dir)

    @staticmethod
    def _tag(u: Underlying, index: int, block: Block) -> str:
        # 방향 표기(사용자 2026-09-08: 진입/청산만으론 정/역 구분이 안 됨). 지금은 정방향 세트만
        # 있어 고정이고, 역방향(§11 예정)이 붙으면 세트의 방향 값으로 바꾼다.
        return f"정방향 {index + 1}세트 {'진입' if block is Block.ENTRY else '청산'}"

    def _trace(self, u: Underlying, index: int, block: Block, leg: Leg) -> None:
        """판정 결과·상태가 바뀐 때만 한 줄 — 100ms마다 다 남기면 하루 수십만 줄."""
        key = (u, index, block)
        tag = self._tag(u, index, block)
        if leg.block_reason and leg.block_reason != self._logged_reason.get(key):
            self._logged_reason[key] = leg.block_reason
            self.ulog(u).info("판정 %s: %s", tag, leg.block_reason)
        status = leg.status.value
        if status != self._logged_status.get(key):
            prev = self._logged_status.get(key, "-")
            self._logged_status[key] = status
            self.ulog(u).info("상태 %s: %s → %s", tag, prev, status)

    # ------------------------------------------------------------ 판정 루프 ---
    async def run(self) -> None:
        while True:
            try:
                self.tick(datetime.now(), time.monotonic())
            except Exception:  # noqa: BLE001 - 한 틱의 오류로 죽지 않는다
                self._log.exception("자동M 판정 틱 오류 — 계속")
            await asyncio.sleep(TICK_S)

    def tick(self, now: datetime, mono: float) -> None:
        """전 종목·세트·진입/청산 1회 판정(now/mono 주입 — 테스트 가능)."""
        self._mono = mono
        halted = self._system.futures_halted()
        if halted:
            self._halt_since = mono
            self._resumed_mono = None
        elif self._halt_since is not None:
            self._halt_since = None
            self._resumed_mono = mono
        for key, book in self.screen.books.items():
            u = Underlying(key)
            for index, s in enumerate(book.sets):
                for block in (Block.ENTRY, Block.EXIT):
                    leg = s.leg(block)
                    if not leg.running and leg.status is LegStatus.IDLE:
                        continue
                    sig = self.build_signals(u, book, s, block, now, mono, halted)
                    self._apply(u, index, block, evaluate(s, block, sig, self.screen.settings, u))
                    self._trace(u, index, block, leg)

    def build_signals(self, u: Underlying, book: AutoMBook, s: AutoMSet, block: Block,
                      now: datetime, mono: float, halted: bool) -> Signals:
        """세트의 진입(또는 청산) 하나의 판정 입력 — 수량은 이번에 낼 계약수(없으면 걸어둔 수량)."""
        inst = self._counterpart(book)
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
    def _apply(self, u: Underlying, index: int, block: Block, actions: list[Action]) -> None:
        for act in actions:
            if act.kind != "notify":  # 행동 전부 종목 로그에(발주·취소·후주문·중지)
                self.ulog(u).info("행동 %s: %s %s %s %s %s", self._tag(u, index, block),
                                  act.kind, act.side.value if act.side else "",
                                  act.qty or "", act.price or "", act.reason or act.order_id or "")
            if act.kind == "place_pre":
                self._spawn(self._place_pre(u, index, block, act))
            elif act.kind == "cancel_pre" and act.order_id:
                self._spawn(self._cancel_pre(u, index, block, act.order_id, act.reason))
            elif act.kind == "place_post":
                self._spawn(self._place_post(u, index, block, act))
            elif act.kind == "halt":
                s = self._book(u).sets[index]
                self._log.error("[자동M] %s %s 중지 — %s | %s",
                                u.value, self._tag(u, index, block), act.reason, self._ledger(s))
                self._system.error_seq += 1  # 메인창 에러 알람 소리(공통설정)
            elif act.kind == "notify":
                self._log.warning("[자동M] %s %s — %s",
                                  u.value, self._tag(u, index, block), act.reason)
            elif act.kind == "alarm":  # 중지는 아니지만 사람이 봐야 함(취소실패, exec ㅂ3)
                self._log.error("[자동M] %s %s — %s",
                                u.value, self._tag(u, index, block), act.reason)
                self._system.error_seq += 1

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _place_pre(self, u: Underlying, index: int, block: Block, act: Action) -> None:
        book = self._book(u)
        s, inst = book.sets[index], self._counterpart(book)
        assert act.side is not None and act.price is not None
        intent = OrderIntent(venue=Venue.LS, underlying=u, instrument=inst, side=act.side,
                             qty=act.qty, order_type=OrderType.LIMIT, price=act.price,
                             source=SOURCE)
        try:
            oid = await self._system.place(intent)
        except Exception as exc:  # noqa: BLE001 - 거부/오류 → 딜레이 뒤 재시도(exec ㄴ5)
            self._log.warning("[자동M] %s 선주문 실패 %s — %s",
                              u.value, self._tag(u, index, block), exc)
            self._apply(u, index, block, on_pre_reject(
                s, block, time.monotonic(), self.screen.settings))
            return
        self._register(oid, _OrderRef(u, index, block, "pre"))
        late = on_pre_ack(s, block, oid, mono=self._mono)  # 발주 중 꺼졌/중지됐으면 취소 행동
        if late:
            self._log.warning("[자동M] %s 선주문 %s #%s — 발주 응답 전 실행 꺼짐/중지 → 즉시 취소",
                              u.value, self._tag(u, index, block), oid)
            self._apply(u, index, block, late)
        self._log.info("[자동M] %s 선주문 %s %s %d @ %g → #%s",
                       u.value, self._tag(u, index, block), act.side.value, act.qty, act.price,
                       oid)

    async def _cancel_pre(self, u: Underlying, index: int, block: Block,
                          order_id: str, reason: str) -> None:
        self._log.info("[자동M] %s 선주문 취소 %s #%s %s",
                       u.value, self._tag(u, index, block), order_id, reason)
        # LS 초당 한도(CFOAT00300 2회)에 걸리면 잠깐 뒤 다시 — 실측 2026-09-08: 한 번 실패한 채
        # 두면 "취소 대기" 표시만 남아 재시도도 종료 취소도 안 됐다. 끝내 실패하면 표시를 되돌려
        # 다음 판정이 다시 보낸다(이미 체결/취소된 주문의 거부도 통보로 정리된다).
        for attempt in range(3):
            try:
                await self._system.cancel(order_id)
                return
            except Exception as exc:  # noqa: BLE001 - 한도·통신 오류 등
                self._log.warning("[자동M] 취소 실패 #%s (%d/3) — %s", order_id, attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(0.6)
        s = self._book(u).sets[index]
        if s.leg(block).pre_order_id == order_id:
            on_pre_cancel_failed(s, block)

    async def _place_post(self, u: Underlying, index: int, block: Block, act: Action) -> None:
        s = self._book(u).sets[index]
        assert act.side is not None
        # 후주문도 지정가(Gtc)만(사용자 확정 2026-09-04) — 상대 1호가 ± HP 여유로 taker처럼 잡는다.
        hl = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        if hl is None or not hl.bid or not hl.ask:
            self._log.error("[자동M] %s 후주문 불가 %s — HL 호가 없음",
                            u.value, self._tag(u, index, block))
            self._apply(u, index, block, on_post_reject(s, block, "HL 호가 없음"))
            return
        raw_price = self.screen.settings.post_price(act.side, hl.bid, hl.ask)
        # HL 가격 격자(유효숫자 5·소수 6−szDecimals)에 맞춘다 — 안 맞으면 통째로 거부(실측 09-07)
        info = self._system.instruments.get((u, Instrument.HL_PERP))
        price = hl_round_price(raw_price, act.side, info.sz_decimals if info else None)
        intent = OrderIntent(venue=Venue.HYPERLIQUID, underlying=u, instrument=Instrument.HL_PERP,
                             side=act.side, qty=act.qty, order_type=OrderType.LIMIT,
                             price=price, source=SOURCE)
        try:
            oid = await self._system.place(intent)
        except Exception as exc:  # noqa: BLE001 - 후주문 거부 → 체결차 → 중지(exec ㄹ2)
            self._log.error("[자동M] %s 후주문 실패 %s — %s",
                            u.value, self._tag(u, index, block), exc)
            self._apply(u, index, block, on_post_reject(s, block, str(exc)[:80]))
            return
        self._register(oid, _OrderRef(u, index, block, "post"))
        # 후주문은 취소하지 않는다(사용자 확정 2026-09-07) — 잔량이 걸려 있어도 후주문대기로 둔다.
        self._log.info("[자동M] %s 후주문 %s HL %s %d @ %g → #%s",
                       u.value, self._tag(u, index, block), act.side.value, act.qty, price, oid)

    # ------------------------------------------------------------ 주문 통보 ---
    def _register(self, oid: str, ref: _OrderRef) -> None:
        """주문번호 ↔ 세트 연결. 발주 응답 안에 이미 실린 체결(HL 즉시체결)은 place()가 끝나기
        전에 훅으로 먼저 오므로 그때는 주인을 모른다 — 모아 뒀다가 여기서 되돌려 반영한다
        (실측 2026-09-07: 후주문 0.588 즉시체결이 세트에 안 잡혀 hl_net 0·HL 대기 1 남음)."""
        self._orders[oid] = ref
        for order, qty, price in self._orphan_fills.pop(oid, []):
            self._apply_fill(ref, order, qty, price)

    def _on_fill_applied(self, order: TrackedOrder, qty: float, price: float,
                         _fill_id: str) -> None:
        ref = self._orders.get(order.order_id)
        if ref is None:
            if order.intent.source == SOURCE:  # 우리 주문인데 아직 등록 전 — 등록 때 되돌려 반영
                self._orphan_fills.setdefault(order.order_id, []).append((order, qty, price))
            return
        self._apply_fill(ref, order, qty, price)

    def _apply_fill(self, ref: _OrderRef, order: TrackedOrder, qty: float,
                    price: float) -> None:
        u = ref.underlying
        s = self._book(u).sets[ref.index]
        mono = time.monotonic()
        leg = s.leg(ref.block)
        # 체결 줄을 먼저 찍고 행동(후주문 발주·중지)을 적용한다 — 행동 줄이 체결 줄보다 앞에 찍혀
        # "체결 전에 판단했다"로 읽힌 실측(2026-09-10 10:45:47.536/537)을 막는다.
        if ref.leg == "pre":
            acts = on_pre_fill(s, ref.block, int(round(qty)), price, mono)
            self.ulog(u).info("체결 %s 선주문 #%s %g @ %g → 누적 %d/%d, HL 대기 %g | %s",
                              self._tag(u, ref.index, ref.block), order.order_id, qty, price,
                              leg.pre_filled, leg.pre_qty, leg.post_pending, self._ledger(s))
        else:
            fx = self._system.fx_entry_rate(order.intent.side) or 0.0
            # Sprd 기준값(S현재가·SF이론가)은 **이 체결 시점** 값을 판 버퍼에 넣는다(사용자 확정
            # 2026-09-10 — 실시간을 쓰면 매매결과가 시세 따라 계속 바뀜). 로그에도 같은 값.
            stock = self._system.stock_last(u)
            theory = self._system.stock_futures_theory(u, self._counterpart(self._book(u)))
            acts = on_post_fill(s, ref.block, qty, price, fx, mono, self.screen.settings,
                                stock_last=stock, sf_theory=theory)
            acc = leg.acc
            sprd = acc.sprd()
            self.ulog(u).info(
                "체결 %s 후주문 #%s HL %g @ %g 환진입가 %g S현재가 %s SF이론가 %s → RT %d "
                "HL대기 %g | %s | 누적 HL %g SF %g 환평균 %s HL평균 %s SF평균 %s Sprd %s",
                self._tag(u, ref.index, ref.block), order.order_id, qty, price, fx,
                stock, f"{theory:,.0f}" if theory else None, s.rt, leg.post_pending,
                self._ledger(s), acc.hl_qty, acc.sf_qty, acc.fx_avg(), acc.hl_avg(),
                acc.sf_avg(), f"{sprd * 100:.3f}%" if sprd is not None else "-(판 미완)")
        self._apply(u, ref.index, ref.block, acts)
        self._trace(u, ref.index, ref.block, leg)
        self._persist()  # RT·체결차·순잔고 바뀜 → core_state.json

    @staticmethod
    def _ledger(s: AutoMSet) -> str:
        """세트 장부 원값 — 체결차의 출처를 로그에서 따라갈 수 있게(실측 2026-09-10)."""
        return f"장부 SF {s.sf_net} HL {s.hl_net:g} 체결차 {s.fill_diff:g}"

    def _persist(self) -> None:
        """코어 상태 저장(세대 백업 포함) — 실패해도 판정을 멈추지 않는다."""
        if self._save is None:
            return
        try:
            self._save()
        except Exception as exc:  # noqa: BLE001 - 저장 실패는 로그만
            self._log.warning("[자동M] 상태 저장 실패 — %s", exc)

    def _on_book_change(self) -> None:
        """취소·거부는 상태 변화로 온다 — 추적 주문의 상태 전이를 한 번씩 처리."""
        for oid, ref in list(self._orders.items()):
            order = self._system.order_book.order(oid)
            if order is None:
                # 장부에서 사라진 추적 주문(재동기 유령 정리 등) — 그대로 두면 "걸려 있다"고
                # 믿고 없는 주문 취소를 되풀이한다(실측 2026-09-09 #20851). 선주문은 취소된 것으로
                # 정리해 다음 판으로, 후주문은 밖에서 끝난 것으로 보고 체결차 → 중지(사람 확인).
                self._recover_vanished(oid, ref)
                continue
            status = order.status.value
            if self._seen_status.get(oid) == status:
                continue
            self._seen_status[oid] = status
            u = ref.underlying
            s = self._book(u).sets[ref.index]
            mono = time.monotonic()
            self.ulog(u).info("통보 %s %s주문 #%s 상태 %s (체결 %g/%g)",
                              self._tag(u, ref.index, ref.block),
                              "선" if ref.leg == "pre" else "후", oid, status,
                              order.filled_qty, order.intent.qty)
            if ref.leg == "pre":
                if status == "cancelled":
                    on_pre_cancelled(s, ref.block, mono, self.screen.settings)
                    self._forget(oid)
                elif status == "rejected":
                    self._apply(u, ref.index, ref.block,
                                on_pre_reject(s, ref.block, mono, self.screen.settings))
                    self._forget(oid)
                elif status == "filled":
                    self._forget(oid)
            elif status in ("cancelled", "rejected"):
                # 후주문이 밖에서 취소(사람·거래소)되거나 거부됨 — 엔진은 후주문을 취소하지 않는다
                # (사용자 확정 2026-09-07). 미체결분(소수 그대로)만큼 체결차 → 중지.
                unfilled = round(order.intent.qty - order.filled_qty, 6)
                if unfilled > 1e-9:
                    self._apply(u, ref.index, ref.block,
                                on_post_partial_reject(s, ref.block, unfilled))
                self._forget(oid)
            elif status == "filled":
                self._forget(oid)
            self._trace(u, ref.index, ref.block, s.leg(ref.block))
            self._persist()  # 취소·거부로 바뀐 상태(체결차·중지) 저장

    def _recover_vanished(self, oid: str, ref: _OrderRef) -> None:
        """장부에서 사라진 추적 주문 정리(실측 2026-09-09 — 재동기가 선물 선주문 #20851을 유령으로
        지워 체결 통보가 미아가 되고 자동M은 'unknown order' 취소를 되풀이). 선주문은 취소 확인과
        같게 다음 판으로, 후주문은 밖에서 끝난 것으로 보고 미체결분 체결차 → 중지(사람이 확인)."""
        u = ref.underlying
        s = self._book(u).sets[ref.index]
        leg = s.leg(ref.block)
        tag = self._tag(u, ref.index, ref.block)
        kind = "선" if ref.leg == "pre" else "후"
        self._log.warning("[자동M] %s %s %s주문 #%s 장부에서 사라짐(재동기 등) — 정리",
                          u.value, tag, kind, oid)
        self.ulog(u).warning("통보 %s %s주문 #%s 장부에서 사라짐 — %s", tag, kind, oid,
                             "취소로 정리" if ref.leg == "pre" else "미체결분 체결차 → 중지")
        if ref.leg == "pre":
            if leg.pre_order_id == oid:
                on_pre_cancelled(s, ref.block, time.monotonic(), self.screen.settings)
        elif leg.post_pending > 1e-9:
            self._apply(u, ref.index, ref.block,
                        on_post_partial_reject(s, ref.block, leg.post_pending))
        self._forget(oid)
        self._trace(u, ref.index, ref.block, s.leg(ref.block))
        self._persist()

    def _forget(self, oid: str) -> None:
        self._orders.pop(oid, None)
        self._seen_status.pop(oid, None)

    # ---------------------------------------------------------------- 명령 ---
    def set_running(self, u: Underlying, index: int, block: Block, value: bool) -> None:
        s = self._book(u).sets[index]
        self.ulog(u).info(
            "명령 %s 실행 %s | 목표 %d 1회 %d 전환 %ds 진입SF %s 진입S %s 청산 %s RT %d",
            self._tag(u, index, block), "켬" if value else "끔", s.target_qty,
            s.per_qty, s.switch_delay_s, s.en_sf, s.en_s, s.ex_sf, s.rt)
        self._apply(u, index, block, set_running(s, block, value, mono=self._mono))
        self._trace(u, index, block, s.leg(block))

    def release(self, u: Underlying, index: int, block: Block) -> None:
        """중지 해제 = 사람이 헤지 정리를 마침(사용자 확정 2026-09-10) — 세트 장부 0에서 재시작.

        중지 시점에 HL에 남아 있던 후주문의 추적도 끊는다: 해제 뒤 그 주문이 체결돼도 새 장부에
        섞이지 않는다(그 주문은 사람이 정리, 후주문은 엔진이 취소하지 않는 규칙 그대로).
        """
        s = self._book(u).sets[index]
        self.ulog(u).info("명령 %s 중지 해제(세트 단위) — 초기화 전 %s",
                          self._tag(u, index, block), self._ledger(s))
        release_halt(s, block)
        for oid, ref in list(self._orders.items()):
            if ref.underlying is u and ref.index == index and ref.leg == "post":
                self.ulog(u).info("통보 %s 후주문 #%s 추적 해제(중지 해제) — 이후 체결은 장부 밖",
                                  self._tag(u, index, ref.block), oid)
                self._forget(oid)
        for b in (Block.ENTRY, Block.EXIT):
            self._trace(u, index, b, s.leg(b))
        self._persist()

    def stop_all(self, u: Underlying | None = None) -> None:
        """실행 해제(창 닫기·안전종료) — 미체결 선주문 취소. u=None이면 전 종목."""
        targets = [u] if u is not None else [Underlying(k) for k in self.screen.books]
        for tu in targets:
            for index in range(len(self._book(tu).sets)):
                for block in (Block.ENTRY, Block.EXIT):
                    if self._book(tu).sets[index].leg(block).running:
                        self.set_running(tu, index, block, False)

    async def shutdown(self, timeout_s: float = 3.0) -> None:
        """안전종료 — 전 종목 정지(미체결 선주문 취소 요청)하고 그 취소 요청이 끝날 때까지 기다린다.

        실측 2026-09-08: 0.3초만 기다리고 닫아 취소가 LS 한도에 걸린 선주문이 그대로 남았다.
        """
        self.stop_all()
        if self._bg:
            await asyncio.wait(list(self._bg), timeout=timeout_s)
        left = [oid for oid, ref in self._orders.items() if ref.leg == "pre"]
        if left:
            self._log.warning("[자동M] 종료 — 취소 확인 못 한 선주문 %s (LS에서 확인 필요)", left)

    # ------------------------------------------------------------- 스냅샷 ---
    def live_snapshot(self) -> dict[str, Any]:
        """화면용 — 종목별 {세트 상태·RT·체결차·누적, 모니터 3칸, HL 호가단위}. 키 = 종목."""
        return {key: self._snapshot_for(Underlying(key), book)
                for key, book in self.screen.books.items()}

    def _snapshot_for(self, u: Underlying, book: AutoMBook) -> dict[str, Any]:
        inst = self._counterpart(book)
        # 상단 모니터 3칸(exec §11.9): 기준수량 est 괴리. 정방향 진입 = HL 매수호가창 est,
        # 청산 = 매도호가창; 역방향은 반대(진입 = 매도호가창, 청산 = 매수호가창).
        q = max(1, book.ref_qty)
        sf_en, sf_ex = self._system.pair_signal(u, inst, q, q)
        s_en, s_ex = self._system.pair_signal(u, Instrument.KR_STOCK, q * HL_PER_SF, q * HL_PER_SF)
        monitor = {"fwd": {"en_sf": sf_en, "en_s": s_en, "ex_sf": sf_ex},
                   "rev": {"en_sf": sf_ex, "en_s": s_ex, "ex_sf": sf_en}}
        out = []
        for s in book.sets:
            row: dict[str, Any] = {"rt": s.rt, "fill_diff": s.fill_diff}
            for name, leg in (("entry", s.entry), ("exit", s.exit)):
                row[name] = {
                    "running": leg.running, "status": leg.status.value,
                    "halt_reason": leg.halt_reason, "pre_order_id": leg.pre_order_id,
                    "pre_price": leg.pre_price, "pre_qty": leg.pre_qty,
                    "pre_filled": leg.pre_filled, "post_pending": leg.post_pending,
                    # 취소 재전송 한도 초과 → 상태줄 "취소실패 #번호 n회"(exec ㅂ3)
                    "cancel_failed": leg.cancel_alarmed, "cancel_tries": leg.cancel_tries,
                    "hl_qty": leg.acc.hl_qty, "sf_qty": leg.acc.sf_qty,
                    # 매매결과 표시는 짝이 맞은(적은 쪽) 체결량 기준(사용자 확정 2026-09-08)
                    "matched_hl": leg.acc.matched_hl(), "matched_sf": leg.acc.matched_sf(),
                    # Sprd는 체결 시점 값들의 가중평균이라 판이 끝나면 고정(2026-09-10)
                    "fx_avg": leg.acc.fx_avg(), "sprd": leg.acc.sprd(),
                }
            out.append(row)
        # HL 호가단위(틱) 옵션 — 일반주문창과 같은 표(가격 자릿수 기반, 코어 계산 §5.10)
        hl = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        ref = (hl.ask or hl.bid) if hl is not None else None
        merge_ticks = ([{"tick": s, "n_sig_figs": nsf, "mantissa": mant}
                        for s, nsf, mant in merge_tick_options(float(ref))] if ref else [])
        active_fn = getattr(self._system, "hl_merge_active", None)
        active = active_fn(u) if callable(active_fn) else None
        return {"sets": out, "any_running": book.any_running(), "monitor": monitor,
                "ref_qty": book.ref_qty, "future_month": book.future_month,
                "hl_merge_ticks": merge_ticks,
                "hl_merge_active": ({"n_sig_figs": active[0], "mantissa": active[1]}
                                    if active is not None else None)}
