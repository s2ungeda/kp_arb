"""동시호가 주문 엔진(DESIGN-auction-order.md §3~§6) — 코어가 하나 만든다. 종목별로 독립.

- 프로세스1(§3): LS주문시각부터 켜진 행을 **위에서 아래로 한 번만** — 차례마다 예상체결가를 다시
  읽어 판정, 맞으면 발주 → 접수 확인 → 인터벌 → 다음 행, 안 맞으면 건너뛰고 바로 다음 행(사용자
  2026-10-26 순차). 동시호가가 끝나 거부되면 그 행은 실패.
- 프로세스2(§4): 환주문시각에 낸 주문 중 그 순간 예상체결가로 체결될 순 수량을 세어 원달러선물을
  반대로, 1회주문수량씩 인터벌 간격(예약실행 켜져 있을 때만).
- 프로세스3(§5): 주식선물 체결 통보(들)를 잠깐 모아 순 체결 계약 × 10을 HL 반대 방향 4건(기준가 ±
  오프셋)으로(예약실행 켜져 있을 때만). 정정 = 취소 확인 → 잔량 신규, 취소 = 잔량 취소. 행 a~d ↔
  주문번호는 저장.
계산·판정은 전부 auction_order(순수). 시각은 PC 시계 그대로(보정 없음).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .auction_order import (
    HL_ROWS,
    SOURCE,
    AuctionBook,
    AuctionScreen,
    AuctionSettings,
    PlacedOrder,
    expected_order_price,
    fillable_qty,
    fire_order,
    fx_hedge_plan,
    hl_amend_price,
    hl_base_price,
    hl_mid,
    hl_order_price,
    hl_side,
    hl_split,
    monitor_spread,
    net_exposure,
    order_moment,
    row_fires,
    sec_of_day,
)
from .domain.enums import Account, Instrument, OrderType, SessionPhase, Side, Underlying, Venue
from .domain.models import OrderIntent, Quote
from .hl_merge import merge_tick_options
from .theory import quote_price

FUTURES_MARKET = "5"           # JIF 선물 시장(session_service.FUTURES_MARKET)
START_GRACE_S = 60.0           # 주문시각이 이보다 더 지난 뒤 켠 코어는 그날 그 프로세스를 안 돌림
TICK_S = 0.02                  # 타이머 틱(ms 단위 주문시각)
FILL_DEBOUNCE_S = 0.5          # 15:45 체결 통보가 여러 건으로 나뉘어 와도 한 번에 모아 HL 발주
CANCEL_WAIT_S = 3.0            # 정정: 취소 확인 대기 상한(넘으면 신규 안 냄 — 사람이 확인)
HL_PER_CONTRACT = 10           # 주식선물 1계약 = HL 10(§5 "계약 × 10")


class _System(Protocol):
    """엔진이 쓰는 LiveSystem 조각(테스트용 가짜도 같은 모양)."""

    quotes: dict[tuple[Underlying, Instrument, str], Quote]
    trades: dict[tuple[Underlying, Instrument, str], float]
    instruments: dict[tuple[Underlying, Instrument], Any]
    expected_prices: dict[tuple[Underlying, Instrument], float]
    fx_futures_expected: dict[str, float]
    fx_futures_price: dict[str, float]
    session: Any
    order_book: Any

    def fx_futures_code(self) -> str | None: ...
    def stock_futures_theory(self, u: Underlying, instrument: Instrument) -> float | None: ...
    def hl_merge_active(self, u: Underlying) -> tuple[int | None, int | None] | None: ...
    def new_hl_cloid(self) -> str | None: ...
    async def place(self, intent: OrderIntent, *, cloid: str | None = None) -> str: ...
    async def place_many(self, intents: Any, cloids: Any = None) -> Any: ...
    async def cancel(self, order_id: str, account: Account | None = None) -> None: ...
    async def place_fx_futures(self, code: str, side: Side, qty: int, price: float,
                               src: str = "") -> str: ...


@dataclass
class RowState:
    """행 하나의 오늘 결과 — 발주했으면 주문번호·가격·수량, 실패면 사유."""

    fired: bool = False
    side: str = ""       # "buy"|"sell" — 재실행 뒤 past_rows에서 체결 방향을 알기 위해
    order_id: str | None = None
    price: float | None = None
    qty: int = 0
    error: str | None = None
    skipped: str = ""    # 차례에 조건이 안 맞아 안 낸 사유(그날 끝 — §3 순차)
    at: str = ""


@dataclass
class HlRow:
    """HL 건 a~d의 오늘 상태(주문번호는 book.hl_orders에도 저장)."""

    order_id: str | None = None
    side: str = ""
    price: float | None = None
    qty: float = 0.0
    status: str = ""     # "접수" | "실패" | "정정중" | "취소" | "끝"
    error: str | None = None


@dataclass
class DayState:
    day: str = ""
    rows: dict[tuple[str, int], RowState] = field(default_factory=dict)
    # 재실행 전 바퀴에서 낸 행(체결 추적용 — 주문번호로 찾는다). 같은 날 다시 돌 때 rows는 비우고
    # 여기로 옮겨 모든 행이 다시 판정·발주된다(사용자 2026-10-27 "2번째 재실행부터 주문이 안 들어감"
    # — 전엔 낸 행을 rows에 남겨 두 번째부터 건너뛰었다)
    past_rows: list[RowState] = field(default_factory=list)
    px: dict[tuple[str, int], float | None] = field(default_factory=dict)  # 표시용 예상주문가
    ls_started: bool = False
    ls_done: bool = False
    ls_at: float | None = None       # 시작할 때 쓴 LS주문시각(초) — 바뀌면 같은 날 다시 돈다
    fx_started: bool = False
    fx_done: bool = False
    fx_at: float | None = None
    fx_sent: list[dict[str, Any]] = field(default_factory=list)
    fx_note: str = ""
    fills: dict[str, tuple[str, float]] = field(default_factory=dict)  # 주문번호 → (방향, 계약)
    hl_done: bool = False
    hl: dict[str, HlRow] = field(default_factory=dict)
    hl_base: float | None = None
    note: str = ""
    fx_run: bool = False   # 예약실행(2) — 저장하지 않는다(매일 켠다)
    hl_run: bool = False   # 예약실행(3)
    test: bool = False     # 시험 모드(§7a, 사용자 2026-10-26 "장중엔 현재가로") — 저장 안 함


def _hms(wall: float) -> str:
    lt = time.localtime(wall)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{int((wall % 1) * 1000):03d}"


class AuctionOrderEngine:
    """동시호가 주문 엔진 — run()이 20ms 틱으로 주문시각을 지키고, 체결 통보로 HL을 낸다."""

    def __init__(self, screen: AuctionScreen, system: _System, *, save: Any = None,
                 wall: Any = None) -> None:
        self._screen = screen
        self._system = system
        self._save = save
        self._wall = wall or time.time
        self._log = logging.getLogger("kp_arb.auction")
        self._days: dict[Underlying, DayState] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._hl_timers: dict[Underlying, asyncio.Task[None]] = {}
        hooks = getattr(system, "on_fill", None)
        if hooks is not None:
            hooks.append(self._on_fill)
        self.clear_armed("시동 — 예약 켬 상태는 복원하지 않음")

    # --- 켜짐 상태는 코어가 살아 있는 동안만(사용자 2026-10-26 "메인 닫을 때 자동주문은 꺼야지 —
    # 모든 자동주문이 이 방식") ---

    def clear_armed(self, why: str) -> int:
        """예약 행 전부 끔 + 예약실행(환·HL) 끔. 시동 때(복원 안 함)와 안전종료 때. 끈 행 수."""
        n = 0
        for key, book in self._screen.books.items():
            for row in book.buy + book.sell:
                if row.on:
                    row.on = False
                    n += 1
            d = self._days.get(Underlying(key)) if key in {u.value for u in Underlying} else None
            if d is not None:
                d.fx_run = d.hl_run = False
        if n and self._save:
            self._save()
        if n:
            self._log.info("[동시호가] %s — 예약 행 %d개 끔", why, n)
        return n

    async def shutdown(self) -> None:
        """안전종료 — 예약·예약실행 전부 끔(메인 종료 = 모든 자동주문 끔)."""
        self.clear_armed("안전종료")

    # --- 상태 접근 ---

    def book(self, u: Underlying) -> AuctionBook:
        return self._screen.book(u.value)

    def _day(self, u: Underlying) -> DayState:
        today = time.strftime("%Y-%m-%d", time.localtime(self._wall()))
        d = self._days.get(u)
        if d is None or d.day != today:
            d = DayState(day=today)
            self._days[u] = d
        return d

    def _instrument(self, u: Underlying) -> Instrument:
        return (Instrument.KR_STOCK_FUTURE_NEXT if self.book(u).month == "next"
                else Instrument.KR_STOCK_FUTURE)

    def sz_decimals(self, u: Underlying) -> int | None:
        info = self._system.instruments.get((u, Instrument.HL_PERP))
        dec = getattr(info, "sz_decimals", None) if info is not None else None
        return int(dec) if isinstance(dec, int) else None

    def set_run(self, u: Underlying, proc: str, value: bool) -> None:
        """예약실행 켜기/끄기 — proc "fx"(프로세스2) | "hl"(프로세스3)."""
        d = self._day(u)
        if proc == "test":  # 시험 모드(§7a): 장중 현재가를 예상체결가 자리에 — 동시호가 단계면 무시
            d.test = value
            self._log.warning("[동시호가] %s 시험 모드(현재가 대용) %s", u.value,
                              "켬" if value else "끔")
            return
        if proc == "fx":
            d.fx_run = value
        else:
            d.hl_run = value
        self._log.info("[동시호가] %s %s 예약실행 %s", u.value, "환" if proc == "fx" else "HL",
                       "켬" if value else "끔")

    def _testing(self, d: DayState) -> bool:
        """시험 모드가 켜져 있고 **동시호가 단계가 아닐 때만** 현재가 대용(진짜 동시호가엔 영향
        없음)."""
        return d.test and not self._in_auction()

    # --- 재료 ---

    def _inputs(self, u: Underlying) -> dict[str, float | None]:
        inst = self._instrument(u)
        hq = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        code = self._system.fx_futures_code()
        sf_exp = self._system.expected_prices.get((u, inst))
        fx_exp = self._system.fx_futures_expected.get(code) if code else None
        if self._testing(self._day(u)):  # §7a: 주식선물 1호가 중간값·원달러선물 현재가로 대신
            sq = self._system.quotes.get((u, inst, "krx"))
            sf_exp = hl_mid(sq.bid, sq.ask) if sq is not None else None
            fx_exp = self._system.fx_futures_price.get(code) if code else None
        # S = NXT 1호가 가중값(사용자 2026-10-27 — 체결 구독 없음, DESIGN-auction-order §1).
        # 통합 호가(UH1)에서 NXT 잔량이 있는 첫 단계로 만든 Quote(market "nxt") — 늘 들어온다.
        nq = self._system.quotes.get((u, Instrument.KR_STOCK, "nxt"))
        return {
            "sf_expected": sf_exp,
            "fx_expected": fx_exp,
            "hl_mid": hl_mid(hq.bid, hq.ask) if hq is not None else None,
            "s": (quote_price(nq.bid, nq.ask, nq.bid_qty, nq.ask_qty)
                  if nq is not None else None),
            "theory": self._system.stock_futures_theory(u, inst),
        }

    def _row_price(self, u: Underlying, inp: dict[str, float | None], side: Side,
                   spread: float | None) -> float | None:
        return expected_order_price(inp["theory"], inp["fx_expected"], inp["hl_mid"], inp["s"],
                                    spread, side)

    def _refresh_prices(self, u: Underlying, d: DayState) -> dict[str, float | None]:
        """표시용 예상주문가(두 예상체결가가 다 있어야 계산 — §2)."""
        inp = self._inputs(u)
        book = self.book(u)
        ready = inp["sf_expected"] is not None and inp["fx_expected"] is not None
        for side in (Side.BUY, Side.SELL):
            for i, row in enumerate(book.rows_of(side)):
                d.px[(side.value, i)] = (self._row_price(u, inp, side, row.spread)
                                         if ready and row.spread is not None else None)
        return inp

    def _in_auction(self) -> bool:
        """선물 시장(5)이 동시호가 단계인가 — **선물 장운영을 실제로 받았을 때만** 판정
        (received_phase). 주식 phase로 대신하면 15:20~15:30 주식 장마감 동시호가가 선물 동시호가로
        보여 시험 모드가 꺼진다(개발 PC·모의 실측 2026-10-27 — 모의는 선물 장운영을 안 보냄).
        옛 세션(메서드 없음)은 phase_for_market 그대로."""
        session = self._system.session
        fn = getattr(session, "received_phase", None)
        phase = fn(FUTURES_MARKET) if callable(fn) else session.phase_for_market(FUTURES_MARKET)
        return phase is SessionPhase.PRE_OPEN

    # --- 타이머 ---

    async def run(self) -> None:
        """20ms 틱 — 종목마다 주문시각이 되면 프로세스1·2 작업을 띄운다."""
        while True:
            try:
                for u in Underlying:
                    self._tick(u)
            except Exception:  # noqa: BLE001 - 틱 한 번의 오류로 엔진이 죽지 않게
                self._log.exception("[동시호가] 틱 오류")
            await asyncio.sleep(TICK_S)

    def _tick(self, u: Underlying) -> None:
        d = self._day(u)
        book = self.book(u)
        st = book.settings
        now = sec_of_day(self._wall())
        armed = any(r.on for r in book.buy + book.sell)
        if armed or d.ls_started:
            self._refresh_prices(u, d)
        ls_at = order_moment(st.ls_hms, st.ls_ms)
        # 주문시각을 **미래로 바꾸면** 그날 다시 돈다(사용자 2026-10-26 "시간 변경해서 다시 시도") —
        # 끝난 프로세스의 상태를 되돌린다. 이미 낸 주문(order_id 있는 행)은 체결 추적을 위해
        # past_rows로 옮기고, 행 자체는 전부 다시 판정·발주 대상(사용자 2026-10-27).
        if (d.ls_done and ls_at is not None and ls_at != d.ls_at and now < ls_at):
            d.ls_started = d.ls_done = False
            d.past_rows.extend(r for r in d.rows.values() if r.order_id)
            d.rows = {}
            d.note = f"LS주문시각 {st.ls_hms}.{st.ls_ms:03d}으로 바뀜 — 다시 대기"
            self._log.info("[동시호가] %s %s", u.value, d.note)
        if (armed and not d.ls_started and ls_at is not None
                and ls_at <= now < ls_at + START_GRACE_S):
            d.ls_started, d.ls_at = True, ls_at
            self._spawn(self._ls_loop(u, d, st))
        fx_at = order_moment(st.fx_hms, st.fx_ms)
        if d.fx_done and fx_at is not None and fx_at != d.fx_at and now < fx_at:
            d.fx_started = d.fx_done = False
            d.fx_sent, d.fx_note = [], ""
            self._log.info("[동시호가] %s 환주문시각 %s.%03d으로 바뀜 — 다시 대기", u.value,
                           st.fx_hms, st.fx_ms)
        if (d.fx_run and not d.fx_started and fx_at is not None
                and fx_at <= now < fx_at + START_GRACE_S):
            d.fx_started, d.fx_at = True, fx_at
            self._spawn(self._fx_hedge(u, d, st))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- 프로세스1 ---

    async def _ls_loop(self, u: Underlying, d: DayState, st: AuctionSettings) -> None:
        """프로세스1(§3, 사용자 2026-10-26 순차): 켜진 행을 **위에서 아래로 한 번만** — 차례가 온
        행을 그때 값(예상체결가·예상주문가)으로 판정해 맞으면 발주 → 접수 확인 → 인터벌 → 다음 행.
        안 맞는 행은 건너뛰고 바로 다음 행(그 행은 그날 끝). 동시호가가 끝나 거부되면 그 행은
        실패."""
        book = self.book(u)
        inst = self._instrument(u)
        self._log.info("[동시호가] %s 프로세스1 시작 %s (LS주문시각 %s.%03d, 인터벌 %dms)", u.value,
                       _hms(self._wall()), st.ls_hms, st.ls_ms, st.ls_itv_ms)
        for side, i in fire_order(book):
            key = (side.value, i)
            if key in d.rows:
                continue
            tag = f"{'매수' if side is Side.BUY else '매도'}{i + 1}"
            inp = self._refresh_prices(u, d)  # 차례마다 예상체결가·예상주문가를 다시 읽는다
            sf_exp = inp["sf_expected"]
            px = d.px.get(key)
            if sf_exp is None or inp["fx_expected"] is None or px is None:
                why = "예상체결가 없음" if sf_exp is None or inp["fx_expected"] is None \
                    else "주문가 계산 불가"
                d.rows[key] = RowState(skipped=why, at=_hms(self._wall()))
                self._log.info("[동시호가] %s %s 건너뜀 — %s", u.value, tag, why)
                continue
            if not row_fires(side, px, sf_exp):
                why = (f"{px:,.0f} {'≤' if side is Side.BUY else '≥'} 예상체결가 {sf_exp:,.0f}")
                d.rows[key] = RowState(price=px, qty=book.rows_of(side)[i].qty, skipped=why,
                                       at=_hms(self._wall()))
                self._log.info("[동시호가] %s %s 건너뜀 — %s (환 %s HL %s S %s 이론가 %s)", u.value,
                               tag, why, inp["fx_expected"], inp["hl_mid"], inp["s"],
                               inp["theory"])
                continue
            await self._place_row(u, d, inst, side, i, px, book.rows_of(side)[i].qty)
            await asyncio.sleep(max(0.0, st.ls_itv_ms / 1000.0))  # 접수 확인 뒤 인터벌
        placed = sum(1 for r in d.rows.values() if r.order_id)
        failed = sum(1 for r in d.rows.values() if r.error)
        skipped = sum(1 for r in d.rows.values() if r.skipped)
        d.note = f"프로세스1 끝 — 발주 {placed}, 실패 {failed}, 건너뜀 {skipped} → 예약 전부 끔"
        # 끝난 뒤 켜진 채 두면 다음 날까지 잠긴 화면만 남는다(사용자 2026-10-26 "잠김 상태로
        # 로드") — NXT 개장의 "발주 끝 → 자동 끔"과 같이 예약 행을 전부 끈다(매일 켠다는 원칙)
        for row in book.buy + book.sell:
            row.on = False
        if self._save:
            self._save()
        self._log.info("[동시호가] %s %s", u.value, d.note)
        d.ls_done = True

    async def _place_row(self, u: Underlying, d: DayState, inst: Instrument, side: Side,
                         i: int, px: float, qty: int) -> None:
        name = f"{'매수' if side is Side.BUY else '매도'}{i + 1}"
        rs = RowState(fired=True, side=side.value, price=px, qty=qty, at=_hms(self._wall()))
        d.rows[(side.value, i)] = rs  # 응답 전에 적어 두어 같은 행이 다시 나가지 않게
        intent = OrderIntent(venue=Venue.LS, underlying=u, instrument=inst, side=side, qty=qty,
                             order_type=OrderType.LIMIT, price=px, source=SOURCE, tag=name)
        try:
            rs.order_id = await self._system.place(intent)
        except Exception as exc:  # noqa: BLE001 - 거부·응답 없음 = 그 행 실패(재시도 없음, §3)
            rs.error = f"{type(exc).__name__}: {exc}"[:160]
            self._log.error("[동시호가] %s %s 발주 실패 %s@%s — %s", u.value, name, qty, px,
                            rs.error)
            return
        self._log.info("[동시호가] %s %s 발주 %s@%s → #%s", u.value, name, qty, f"{px:,.0f}",
                       rs.order_id)

    # --- 프로세스2 ---

    async def _fx_hedge(self, u: Underlying, d: DayState, st: AuctionSettings) -> None:
        try:
            await self._fx_hedge_run(u, d, st)
        finally:
            d.fx_done = True
            if d.fx_run:  # 끝나면 예약실행 자동 끔(예약 행과 같은 원칙 — 매일 켠다, 2026-10-26)
                d.fx_run = False
                self._log.info("[동시호가] %s 환 예약실행 자동 끔(헤지 끝)", u.value)

    async def _fx_hedge_run(self, u: Underlying, d: DayState, st: AuctionSettings) -> None:
        code = self._system.fx_futures_code()
        inp = self._inputs(u)
        sf_exp, fx_exp = inp["sf_expected"], inp["fx_expected"]
        placed = [PlacedOrder(Side(s), r.price, r.qty) for (s, _i), r in d.rows.items()
                  if r.order_id and r.price is not None]
        if code is None or sf_exp is None or fx_exp is None:
            d.fx_note = "환 헤지 안 냄 — 예상체결가·월물 없음"
            d.fx_done = True
            self._log.warning("[동시호가] %s %s", u.value, d.fx_note)
            return
        buy_q, sell_q = fillable_qty(placed, sf_exp)
        net = net_exposure(buy_q, sell_q)
        if net is None:
            d.fx_note = f"환 헤지 없음 — 체결될 수량 매수 {buy_q} 매도 {sell_q}(순 0)"
            d.fx_done = True
            self._log.info("[동시호가] %s %s", u.value, d.fx_note)
            return
        side, px, parts = fx_hedge_plan(net[0], net[1], sf_exp, fx_exp, st.tick, st.hedge_pct,
                                        st.fx_unit)
        d.fx_note = (f"환 {side.value} {sum(parts)}계약 @{px} ({len(parts)}건) — 순 "
                     f"{net[0].value} {net[1]}계약, 예상체결가 SF {sf_exp:,.0f} 환 {fx_exp}")
        self._log.info("[동시호가] %s 프로세스2 %s", u.value, d.fx_note)
        for n, qty in enumerate(parts):
            rec: dict[str, Any] = {"n": n + 1, "side": side.value, "qty": qty, "price": px,
                                   "at": _hms(self._wall())}
            try:
                rec["order_id"] = await self._system.place_fx_futures(code, side, qty, px,
                                                                      src=SOURCE)
            except Exception as exc:  # noqa: BLE001 - 실패 건은 재시도 없음(§4·CLAUDE.md §7)
                rec["error"] = f"{type(exc).__name__}: {exc}"[:160]
                self._log.error("[동시호가] %s 환 %d건 실패 — %s", u.value, n + 1, rec["error"])
            d.fx_sent.append(rec)
            if n + 1 < len(parts):
                await asyncio.sleep(max(0.0, st.fx_itv_ms / 1000.0))
        d.fx_done = True

    # --- 프로세스3 ---

    def _on_fill(self, fill: Any) -> None:
        """주식선물 체결 통보 — 우리 행의 주문이면 모아 두고, 잠깐 뒤 HL 발주(예약실행 켜져 있을
        때)."""
        oid = str(getattr(fill, "order_id", ""))
        account = getattr(fill, "account", None)
        if account not in (None, Account.KR_DERIV):
            return
        for u, d in self._days.items():
            for s, r in [*((s, r) for (s, _i), r in d.rows.items()),
                         *((r.side, r) for r in d.past_rows)]:
                if r.order_id == oid:
                    prev = d.fills.get(oid, (s, 0.0))
                    d.fills[oid] = (s, prev[1] + float(getattr(fill, "qty", 0.0)))
                    self._log.info("[동시호가] %s 체결 #%s %s %s", u.value, oid, s,
                                   getattr(fill, "qty", 0.0))
                    if d.hl_run and not d.hl_done and u not in self._hl_timers:
                        self._hl_timers[u] = asyncio.create_task(self._hl_after_fills(u, d))
                    return

    async def _hl_after_fills(self, u: Underlying, d: DayState) -> None:
        try:
            await asyncio.sleep(FILL_DEBOUNCE_S)
            await self._hl_fire(u, d)
        finally:
            self._hl_timers.pop(u, None)

    async def _hl_fire(self, u: Underlying, d: DayState) -> None:
        if d.hl_done:
            return
        d.hl_done = True  # 하루 한 번(늦게 오는 체결은 로그만 — 사람이 정리)
        buy = sum(q for s, q in d.fills.values() if s == Side.BUY.value)
        sell = sum(q for s, q in d.fills.values() if s == Side.SELL.value)
        net = net_exposure(int(round(buy)), int(round(sell)))
        if net is None:
            d.note = f"HL 헤지 없음 — 체결 매수 {buy:g} 매도 {sell:g}(순 0)"
            self._log.info("[동시호가] %s %s", u.value, d.note)
            return
        sf_side, contracts = net
        side = hl_side(sf_side)
        hq = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        merge = self._system.hl_merge_active(u) or (None, None)
        sz = self.sz_decimals(u)
        base = hl_base_price(hq.bid if hq else None, hq.ask if hq else None, merge[0], merge[1],
                             sz)
        if base is None:
            d.note = "HL 헤지 안 냄 — HL 호가 없음"
            self._log.error("[동시호가] %s %s", u.value, d.note)
            return
        d.hl_base = base
        book = self.book(u)
        parts = hl_split(contracts * HL_PER_CONTRACT, sz)
        intents: list[OrderIntent] = []
        names: list[str] = []
        for name, qty in zip(HL_ROWS, parts, strict=False):
            px = hl_order_price(base, book.settings.hl_off.get(name, 0.0), sz)
            d.hl[name] = HlRow(side=side.value, price=px, qty=qty)
            names.append(name)
            intents.append(OrderIntent(venue=Venue.HYPERLIQUID, underlying=u,
                                       instrument=Instrument.HL_PERP, side=side, qty=qty,
                                       order_type=OrderType.LIMIT, price=px, source=SOURCE,
                                       tag=f"HL{name}"))
        self._log.info("[동시호가] %s 프로세스3 HL %s 기준가 %s (순 체결 %s %d계약): %s",
                       u.value, side.value, base, sf_side.value, contracts,
                       [(n, d.hl[n].qty, d.hl[n].price) for n in names])
        try:
            outs = await self._system.place_many(intents, [self._system.new_hl_cloid()
                                                           for _ in intents])
        except Exception as exc:  # noqa: BLE001 - 묶음 실패 = 전부 실패(재시도 없음)
            for n in names:
                d.hl[n].status, d.hl[n].error = "실패", f"{type(exc).__name__}: {exc}"[:160]
            self._log.error("[동시호가] %s HL 묶음 발주 실패: %s", u.value, exc)
            return
        for name, out in zip(names, outs, strict=False):
            oid = getattr(out, "order_id", None)
            err = getattr(out, "error", None)
            row = d.hl[name]
            if oid:
                row.order_id, row.status = str(oid), "접수"
                book.hl_orders[name] = str(oid)
            else:
                row.status, row.error = "실패", err
            self._log.info("[동시호가] %s HL %s %s %s @ %s → %s", u.value, name, row.side,
                           row.qty, row.price, oid or f"실패 {err}")
        if self._save:
            self._save()
        if d.hl_run:  # 4건이 나갔으니 예약실행 자동 끔(정정·취소는 버튼으로 계속)
            d.hl_run = False
            self._log.info("[동시호가] %s HL 예약실행 자동 끔(발주 끝)", u.value)

    async def amend(self, u: Underlying, name: str) -> str | None:
        """정정(§5) — 잔량을 취소 확인 뒤 정정PT만큼 상대호가 쪽으로 옮긴 신규주문. 거부 사유 또는
        None."""
        d = self._day(u)
        book = self.book(u)
        oid = book.hl_orders.get(name)
        if not oid:
            return f"{name} 행에 HL 주문이 없습니다"
        tracked = self._system.order_book.order(oid)
        if tracked is None or not tracked.is_open:
            return f"{name} 행 주문 #{oid}이(가) 열려 있지 않습니다"
        pt = book.settings.hl_pt.get(name, 0.0)
        if pt <= 0:
            return f"{name} 행 정정PT가 없습니다"
        side = tracked.intent.side
        remaining = float(tracked.remaining_qty)
        new_px = hl_amend_price(float(tracked.intent.price or 0.0), pt, side, self.sz_decimals(u))
        row = d.hl.setdefault(name, HlRow(order_id=oid, side=side.value,
                                          price=tracked.intent.price, qty=tracked.intent.qty))
        row.status = "정정중"
        self._log.info("[동시호가] %s HL %s 정정 — #%s 잔량 %s %s → %s", u.value, name, oid,
                       remaining, tracked.intent.price, new_px)
        try:
            await self._system.cancel(oid)
        except Exception as exc:  # noqa: BLE001 - 취소 요청 실패 = 정정 안 함(원주문 유지)
            row.status = "접수"
            return f"취소 요청 실패 — {exc}"[:160]
        deadline = time.monotonic() + CANCEL_WAIT_S
        while tracked.is_open and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if tracked.is_open:
            row.status = "접수"
            return f"취소 확인이 {CANCEL_WAIT_S:.0f}초 안에 안 옴 — 신규 안 냄(HL에서 확인)"
        remaining = float(tracked.remaining_qty) or remaining  # 취소 전에 체결된 만큼은 빠짐
        if remaining <= 0:
            row.status = "끝"
            book.hl_orders.pop(name, None)
            return f"{name} 행은 취소 전에 전량 체결됐습니다"
        intent = OrderIntent(venue=Venue.HYPERLIQUID, underlying=u, instrument=Instrument.HL_PERP,
                             side=side, qty=remaining, order_type=OrderType.LIMIT, price=new_px,
                             source=SOURCE, tag=f"HL{name}정정")
        try:
            new_oid = await self._system.place(intent, cloid=self._system.new_hl_cloid())
        except Exception as exc:  # noqa: BLE001 - 신규 실패 = 취소만 된 채 끝(사람이 정리)
            row.status, row.error = "실패", f"{type(exc).__name__}: {exc}"[:160]
            book.hl_orders.pop(name, None)
            if self._save:
                self._save()
            return f"신규주문 실패 — {row.error}"
        row.order_id, row.price, row.qty, row.status = str(new_oid), new_px, remaining, "접수"
        book.hl_orders[name] = str(new_oid)
        if self._save:
            self._save()
        self._log.info("[동시호가] %s HL %s 정정 완료 → #%s %s @ %s", u.value, name, new_oid,
                       remaining, new_px)
        return None

    async def cancel(self, u: Underlying, name: str) -> str | None:
        """취소(§5) — 그 행 잔량 전부 취소, 그 행은 끝. 거부 사유 또는 None."""
        d = self._day(u)
        book = self.book(u)
        oid = book.hl_orders.get(name)
        if not oid:
            return f"{name} 행에 HL 주문이 없습니다"
        try:
            await self._system.cancel(oid)
        except Exception as exc:  # noqa: BLE001 - 응답 없음도 재전송 금지(CLAUDE.md §7)
            return f"취소 요청 실패 — {exc}"[:160]
        row = d.hl.setdefault(name, HlRow(order_id=oid))
        row.status = "취소"
        book.hl_orders.pop(name, None)
        if self._save:
            self._save()
        self._log.info("[동시호가] %s HL %s 취소 요청 #%s", u.value, name, oid)
        return None

    # --- 화면 ---

    def live_snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for u in Underlying:
            d = self._day(u)
            inp = self._inputs(u)
            if not (d.ls_started or any(r.on for r in self.book(u).buy + self.book(u).sell)):
                self._refresh_prices(u, d)  # 켜진 행이 없어도 표시용 예상주문가는 보여 준다
            hl_rows: dict[str, Any] = {}
            for name in HL_ROWS:
                row = d.hl.get(name)
                oid = self.book(u).hl_orders.get(name)
                tracked = self._system.order_book.order(oid) if oid else None
                hl_rows[name] = {
                    "order_id": oid, "side": row.side if row else "",
                    "price": row.price if row else None, "qty": row.qty if row else 0.0,
                    "remaining": float(tracked.remaining_qty) if tracked is not None else None,
                    "open": bool(tracked.is_open) if tracked is not None else False,
                    "status": row.status if row else "", "error": row.error if row else None}
            placed = [PlacedOrder(Side(s), r.price, r.qty) for (s, _i), r in d.rows.items()
                      if r.order_id and r.price is not None]
            net = (net_exposure(*fillable_qty(placed, inp["sf_expected"]))
                   if placed and inp["sf_expected"] is not None else None)
            hq = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
            ref = (hq.ask or hq.bid) if hq is not None else None
            merge = self._system.hl_merge_active(u)
            out[u.value] = {
                "sf_expected": inp["sf_expected"], "fx_expected": inp["fx_expected"],
                # 모니터(사용자 2026-10-26): 정방향매도 = HL 매수1호가, 역방향매수 = HL 매도1호가
                "mon_fwd": monitor_spread(inp["sf_expected"], inp["theory"], inp["fx_expected"],
                                          hq.bid if hq else None, inp["s"]),
                "mon_rev": monitor_spread(inp["sf_expected"], inp["theory"], inp["fx_expected"],
                                          hq.ask if hq else None, inp["s"]),
                "net": {"side": net[0].value, "qty": net[1]} if net else None,  # 진입계약수
                "hl_merge_ticks": ([{"tick": t, "n_sig_figs": nsf, "mantissa": mant}
                                    for t, nsf, mant in merge_tick_options(float(ref))]
                                   if ref else []),
                "hl_merge_active": ({"n_sig_figs": merge[0], "mantissa": merge[1]}
                                    if merge is not None else None),
                "hl_mid": inp["hl_mid"], "s": inp["s"], "theory": inp["theory"],
                "prices": {f"{s}:{i}": px for (s, i), px in d.px.items()},
                # 지금 값으로 차례가 오면 나갈 행(§2 표시, 사용자 2026-10-26) — 켜져 있고 아직 안 본
                # 행
                "fires": {f"{s}:{i}": True for (s, i), px in d.px.items()
                          if px is not None and inp["sf_expected"] is not None
                          and (s, i) not in d.rows
                          and self.book(u).rows_of(Side(s))[i].on
                          and row_fires(Side(s), px, inp["sf_expected"])},
                "rows": {f"{s}:{i}": {"order_id": r.order_id, "price": r.price, "qty": r.qty,
                                      "error": r.error, "skipped": r.skipped, "at": r.at}
                         for (s, i), r in d.rows.items()},
                "ls_started": d.ls_started, "ls_done": d.ls_done,
                "fx_run": d.fx_run, "fx_started": d.fx_started, "fx_done": d.fx_done,
                "fx_sent": list(d.fx_sent), "fx_note": d.fx_note,
                "fills": {oid: {"side": s, "qty": q} for oid, (s, q) in d.fills.items()},
                "hl_run": d.hl_run, "hl_done": d.hl_done, "hl_base": d.hl_base, "hl": hl_rows,
                "note": d.note, "auction": self._in_auction(), "test": d.test,
                "testing": self._testing(d),
            }
        return out
