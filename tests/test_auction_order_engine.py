"""동시호가 주문 엔진(DESIGN-auction-order §3~§6) — 주문시각 바퀴·인터벌 재판정, 환 헤지 분할,
체결 → HL 4건, 정정(취소 확인 → 신규)·취소, 스냅샷. 라이브 없음(가짜 시스템)."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from kp_arb.auction_order import AuctionScreen, Row
from kp_arb.auction_order_engine import FILL_DEBOUNCE_S, AuctionOrderEngine
from kp_arb.domain.enums import (
    Account,
    Instrument,
    OrderType,
    SessionPhase,
    Side,
    Underlying,
    Venue,
)
from kp_arb.domain.models import InstrumentInfo, OrderIntent, Quote
from kp_arb.gateways.base import PlaceResult
from kp_arb.gateways.ls_ws import Fill
from kp_arb.order_book import OrderBook

U = Underlying.SK_HYNIX
SF = Instrument.KR_STOCK_FUTURE


class _Session:
    def __init__(self) -> None:
        self.phase = SessionPhase.PRE_OPEN

    def phase_for_market(self, market: str) -> SessionPhase:
        return self.phase


class FakeSystem:
    """엔진이 쓰는 조각 — 주문은 장부(OrderBook)에 실제로 올려 정정의 잔량·열림 판정을 흉내 낸다."""

    def __init__(self) -> None:
        self.on_fill: list[Any] = []
        self.quotes: dict[tuple[Underlying, Instrument, str], Quote] = {
            (U, Instrument.HL_PERP, "hl"): Quote(underlying=U, instrument=Instrument.HL_PERP,
                                                 bid=1332.4, ask=1332.6, ts=0, market="hl")}
        self.trades: dict[tuple[Underlying, Instrument, str], float] = {
            (U, Instrument.KR_STOCK, "nxt"): 1_766_000.0}
        self.instruments = {(U, Instrument.HL_PERP): InstrumentInfo(
            underlying=U, instrument=Instrument.HL_PERP, code="xyz:SKHX", sz_decimals=2)}
        self.expected_prices: dict[tuple[Underlying, Instrument], float] = {(U, SF): 1_766_000.0}
        self.fx_futures_expected: dict[str, float] = {"A756B000": 1327.30}
        self.fx_futures_price: dict[str, float] = {"A756B000": 1330.00}
        self.session = _Session()
        self.order_book = OrderBook()
        self.placed: list[OrderIntent] = []
        self.fx_placed: list[tuple[str, Side, int, float]] = []
        self.cancelled: list[str] = []
        self.fail_tags: set[str] = set()
        self._n = 0

    def fx_futures_code(self) -> str | None:
        return "A756B000"

    def stock_futures_theory(self, u: Underlying, instrument: Instrument) -> float | None:
        return 1_770_000.0

    def hl_merge_active(self, u: Underlying) -> tuple[int | None, int | None] | None:
        return None

    def new_hl_cloid(self) -> str | None:
        self._c = getattr(self, "_c", 0) + 1
        return f"c{self._c}"

    async def place(self, intent: OrderIntent, *, cloid: str | None = None) -> str:
        if intent.tag in self.fail_tags:
            raise RuntimeError("LS 거부 01433")
        self._n += 1
        oid = f"O{self._n}"
        self.placed.append(intent)
        acct = Account.KR_DERIV if intent.venue is Venue.LS else None
        self.order_book.track(oid, intent.model_copy(update={"account": acct}))
        return oid

    async def place_many(self, intents: Any, cloids: Any = None) -> list[PlaceResult]:
        outs = []
        for it in intents:
            outs.append(PlaceResult(await self.place(it)))
        return outs

    async def cancel(self, order_id: str, account: Account | None = None) -> None:
        self.cancelled.append(order_id)
        o = self.order_book.order(order_id)
        assert o is not None
        o.cancelled_qty = o.remaining_qty  # 취소 확정 — 장부가 닫힘(HL 통보 대신)
        from kp_arb.order_book import OrderStatus

        o.status = OrderStatus.CANCELLED

    async def place_fx_futures(self, code: str, side: Side, qty: int, price: float,
                               src: str = "") -> str:
        self.fx_placed.append((code, side, qty, price))
        return f"F{len(self.fx_placed)}"


def _wall(h: int, m: int, s: float) -> float:
    lt = time.localtime()
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return base + h * 3600 + m * 60 + s


class Clock:
    def __init__(self, wall: float) -> None:
        self.wall = wall


def _engine(wall: float) -> tuple[AuctionOrderEngine, FakeSystem, Clock, AuctionScreen]:
    clk = Clock(wall)
    sys_ = FakeSystem()
    screen = AuctionScreen()
    b = screen.book(U.value)
    b.settings.ls_itv_ms = 0
    b.settings.fx_itv_ms = 0
    saves: list[int] = []
    eng = AuctionOrderEngine(screen, sys_, save=lambda: saves.append(1), wall=lambda: clk.wall)
    return eng, sys_, clk, screen


async def _settle(n: int = 6) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def test_process1_goes_top_down_once_skipping_rows_that_do_not_qualify() -> None:
    # §3(10-26 오후, 순차): 주문시각부터 위→아래 한 행씩 — 차례에 조건 맞으면 발주 → 접수 → 인터벌
    # → 다음, 안 맞으면 건너뛰고 바로 다음(그 행은 그날 끝). 실패한 행도 재시도 없음. 한 번 내려가면
    # 끝.
    eng, sys_, clk, screen = _engine(_wall(15, 44, 55.0))
    b = screen.book(U.value)
    # 환 1327.30·HL 중간 1332.5·S 1,766,000·이론가 1,770,000 → s 0.003 → 매수 1,767,000
    # (> 예상 1,766,000 ✓)
    b.buy[0] = Row(0.003, 20, True)        # 나감
    b.buy[1] = Row(0.004, 10, True)        # 1,765,000 ≤ 1,766,000 → 건너뜀
    b.buy[2] = Row(0.003, 7, True)         # 1,767,000 — 차례엔 예상체결가가 올라 건너뜀
    b.sell[0] = Row(-0.001, 5, True)       # 매도 1,775,000 ≥ 예상 → 건너뜀
    b.sell[1] = Row(0.002, 5, True)        # 매도 1,770,000(올림) — 차례 전 예상체결가가 오르면 나감
    b.settings.ls_itv_ms = 50              # 낸 주문 뒤 인터벌 — 그 사이에 예상체결가가 바뀐다
    eng._tick(U)
    assert not eng._day(U).ls_started       # 15:44:55.000 < 주문시각 15:44:55.100
    # 주문시각 전: 지금 값으로 나갈 행만 표시(사용자 2026-10-26 색 표시) — 매수1·매수3(1,767,000 >
    # 1,766,000), 매도2는 1,770,000 ≥ 라 아님
    assert eng.live_snapshot()[U.value]["fires"] == {"buy:0": True, "buy:2": True}
    clk.wall = _wall(15, 44, 55.12)
    eng._tick(U)
    await _settle(4)
    d = eng._day(U)
    assert d.ls_started
    assert [(i.tag, i.qty, i.price) for i in sys_.placed] == [("매수1", 20, 1_767_000.0)]
    # 매수1 접수 뒤 인터벌 동안 예상체결가가 1,771,000으로 오르면 다음 행들은 그 값으로 판정한다
    sys_.expected_prices[(U, SF)] = 1_771_000.0
    await asyncio.sleep(0.15)
    assert [i.tag for i in sys_.placed] == ["매수1", "매도2"]   # 매도2 1,770,000 < 1,771,000
    assert d.rows[("buy", 1)].skipped.startswith("1,765,000 ≤")
    assert d.rows[("buy", 2)].skipped.startswith("1,767,000 ≤")
    assert d.rows[("sell", 0)].skipped.startswith("1,775,000 ≥")
    assert d.ls_done and d.note == "프로세스1 끝 — 발주 2, 실패 0, 건너뜀 3 → 예약 전부 끔"
    assert not any(r.on for r in b.buy + b.sell)   # 끝나면 예약 행 자동 끔(화면 잠금 해제)
    snap = eng.live_snapshot()[U.value]
    assert snap["rows"]["buy:0"]["order_id"] == "O1" and snap["prices"]["buy:0"] == 1_767_000.0
    assert snap["rows"]["buy:1"]["skipped"] and snap["ls_done"]
    assert snap["fires"] == {}                       # 이미 본 행은 "나갈 행" 표시에서 빠진다
    # 건너뛴 행은 뒤에 조건이 맞아져도 다시 안 낸다(한 번만)
    sys_.expected_prices[(U, SF)] = 1_700_000.0
    await _settle(8)
    assert len(sys_.placed) == 2


async def test_process1_fails_rows_rejected_after_market_close_without_retry() -> None:
    # §3-4: 뒤쪽 행이 동시호가가 끝난 뒤 차례가 와 거부되면 실패로 남기고 재시도 없음
    eng, sys_, clk, screen = _engine(_wall(15, 44, 55.2))
    b = screen.book(U.value)
    b.buy[0] = Row(0.003, 20, True)
    b.buy[1] = Row(0.003, 10, True)
    sys_.fail_tags.add("매수2")            # 장마감 뒤 거부를 흉내
    eng._tick(U)
    await _settle(8)
    d = eng._day(U)
    assert [i.tag for i in sys_.placed] == ["매수1"] and d.rows[("buy", 1)].error
    assert d.ls_done and d.note == "프로세스1 끝 — 발주 1, 실패 1, 건너뜀 0 → 예약 전부 끔"


async def test_process2_hedges_net_fillable_qty_in_splits() -> None:
    # §4: 환주문시각에 낸 주문 중 체결될 순 수량 → 반대 방향 원달러, 1회수량씩 인터벌로, 예약실행
    # 켜졌을 때만
    eng, sys_, clk, screen = _engine(_wall(15, 44, 55.2))
    b = screen.book(U.value)
    b.buy[0] = Row(0.003, 20, True)        # 매수 1,767,000 — 예상 1,766,000보다 높아 나감
    b.buy[1] = Row(0.0025, 5, True)        # 매수 1,768,000 — 나감
    b.sell[0] = Row(0.004, 5, True)        # 매도 1,766,000 — 예상과 같아 차례에 건너뜀
    b.sell[1] = Row(0.0045, 5, True)       # 매도 1,765,000 < 1,766,000 → 나감
    b.settings.fx_unit = 20
    eng._tick(U)
    await _settle(8)
    assert [i.tag for i in sys_.placed] == ["매수1", "매수2", "매도2"]
    eng.set_run(U, "fx", True)
    clk.wall = _wall(15, 44, 58.25)
    # 예상체결가 1,766,500: 매수 둘(25계약)·매도2(1,765,000 < 1,766,500)는 체결될 것 → 순 매수
    # 20계약만 헤지(자전 방지)
    sys_.expected_prices[(U, SF)] = 1_766_500.0
    eng._tick(U)
    await _settle(8)
    d = eng._day(U)
    assert d.fx_done
    # 20 × 10 × 1,766,500 / 1,327.30 / 10,000 = 26.6 → 26계약 → 20 + 6, 매도 = 1327.30 − 2.0
    assert sys_.fx_placed == [("A756B000", Side.SELL, 20, 1325.3),
                              ("A756B000", Side.SELL, 6, 1325.3)]
    assert [r["order_id"] for r in d.fx_sent] == ["F1", "F2"]
    assert "순 buy 20계약" in d.fx_note
    assert not d.fx_run                        # 헤지가 끝나면 환 예약실행 자동 끔(10-26)


async def test_process3_fills_to_four_hl_orders_then_amend_and_cancel() -> None:
    # §5: 체결 통보(들)를 모아 순 체결 × 10을 HL 반대 방향 4건(기준가 + 오프셋, 소수 2자리, 나머지
    # a), 행 a~d ↔ 주문번호 저장. 정정 = 취소 확인 → 잔량을 정정PT만큼 옮겨 신규. 취소 = 잔량 취소.
    eng, sys_, clk, screen = _engine(_wall(15, 44, 55.2))
    b = screen.book(U.value)
    b.buy[0] = Row(0.003, 3, True)
    b.settings.hl_off = {"a": 0.5, "b": 0.0, "c": -0.5, "d": -1.0}
    b.settings.hl_pt = {"a": 0.7, "b": 0.0, "c": 0.0, "d": 0.0}
    eng._tick(U)
    await _settle()
    assert [i.tag for i in sys_.placed] == ["매수1"]
    eng.set_run(U, "hl", True)
    # 15:45 체결 통보 두 건으로 나뉘어 옴(2 + 1 계약)
    for n, q in enumerate((2.0, 1.0), start=1):
        sys_.on_fill[0](Fill(fill_id=f"x{n}", order_id="O1", qty=q, price=1_767_000.0, ts=0,
                             account=Account.KR_DERIV))
    assert eng._day(U).fills == {"O1": ("buy", 3.0)}
    await asyncio.sleep(FILL_DEBOUNCE_S + 0.05)
    d = eng._day(U)
    hl = [i for i in sys_.placed if i.venue is Venue.HYPERLIQUID]
    # 선물 매수 3계약 → HL 매도 30 → 7.5씩, 기준가 1332.5 + 오프셋
    assert [(i.tag, i.side, i.qty, i.price) for i in hl] == [
        ("HLa", Side.SELL, 7.5, 1333.0), ("HLb", Side.SELL, 7.5, 1332.5),
        ("HLc", Side.SELL, 7.5, 1332.0), ("HLd", Side.SELL, 7.5, 1331.5)]
    assert all(i.order_type is OrderType.LIMIT and not i.reduce_only for i in hl)
    assert b.hl_orders == {"a": "O2", "b": "O3", "c": "O4", "d": "O5"} and d.hl_base == 1332.5
    assert not d.hl_run                        # 4건 나갔으니 HL 예약실행 자동 끔(10-26)
    # 정정 a: 잔량 7.5를 1333.0 − 0.7 = 1332.3으로(매도는 아래로), 취소 확인 뒤 신규
    assert await eng.amend(U, "a") is None
    assert sys_.cancelled == ["O2"] and b.hl_orders["a"] == "O6"
    new = sys_.placed[-1]
    assert (new.tag, new.side, new.qty, new.price) == ("HLa정정", Side.SELL, 7.5, 1332.3)
    assert await eng.amend(U, "b") == "b 행 정정PT가 없습니다"
    # 취소 c
    assert await eng.cancel(U, "c") is None and "O4" in sys_.cancelled and "c" not in b.hl_orders
    assert await eng.cancel(U, "c") == "c 행에 HL 주문이 없습니다"
    snap = eng.live_snapshot()[U.value]["hl"]
    assert snap["a"]["order_id"] == "O6" and snap["a"]["open"] and snap["a"]["remaining"] == 7.5
    assert snap["c"]["status"] == "취소" and snap["c"]["order_id"] is None
    # 늦게 온 체결은 다시 HL을 내지 않는다(하루 한 번)
    sys_.on_fill[0](Fill(fill_id="x3", order_id="O1", qty=1.0, price=1_767_000.0, ts=0,
                         account=Account.KR_DERIV))
    await asyncio.sleep(FILL_DEBOUNCE_S + 0.05)
    assert len([i for i in sys_.placed if i.venue is Venue.HYPERLIQUID]) == 5


async def test_hl_not_fired_when_run_off_and_net_zero() -> None:
    eng, sys_, clk, screen = _engine(_wall(15, 44, 55.2))
    b = screen.book(U.value)
    b.buy[0] = Row(0.003, 2, True)
    eng._tick(U)
    await _settle()
    sys_.on_fill[0](Fill(fill_id="x1", order_id="O1", qty=2.0, price=1_767_000.0, ts=0,
                         account=Account.KR_DERIV))
    await asyncio.sleep(FILL_DEBOUNCE_S + 0.05)
    assert len(sys_.placed) == 1              # 예약실행 꺼짐 → HL 없음
    assert eng._day(U).fills == {"O1": ("buy", 2.0)}


async def test_test_mode_uses_futures_mid_and_fx_last_outside_auction() -> None:
    # §7a(사용자 2026-10-26 "장중엔 예상체결가가 없으니 현재가로"): 시험 모드면 주식선물 1호가
    # 중간값·원달러선물 현재가를 예상체결가 자리에. 동시호가 단계(31)에 들어가면 시험 모드를 무시.
    eng, sys_, clk, screen = _engine(_wall(10, 30, 0.2))
    b = screen.book(U.value)
    b.settings.ls_hms, b.settings.ls_ms = "10:30:00", 100
    b.buy[0] = Row(0.003, 1, True)       # 매수 1,767,000
    sys_.session.phase = SessionPhase.REGULAR
    sys_.expected_prices.clear()          # 장중 — 예상체결가 없음
    sys_.quotes[(U, SF, "krx")] = Quote(underlying=U, instrument=SF, bid=1_765_000.0,
                                        ask=1_766_000.0, ts=0, market="krx")
    eng.set_run(U, "test", True)
    eng._tick(U)
    await _settle(8)
    snap = eng.live_snapshot()[U.value]
    assert snap["testing"] and snap["sf_expected"] == 1_765_500.0
    assert snap["fx_expected"] == 1330.0
    assert [i.tag for i in sys_.placed] == ["매수1"]   # 1,767,000 > 1,765,500 → 나감
    sys_.session.phase = SessionPhase.PRE_OPEN   # 동시호가에 들어가면 시험 모드 무시
    assert not eng._testing(eng._day(U)) and eng.live_snapshot()[U.value]["sf_expected"] is None


async def test_armed_rows_cleared_on_startup_and_shutdown() -> None:
    # 사용자 2026-10-26 "메인 닫을 때 자동주문은 꺼야지 — 모든 자동주문이 이 방식": 시동 때 저장된
    # 켬 상태를 복원하지 않고, 안전종료 때 예약·예약실행 전부 끔
    screen = AuctionScreen()
    b = screen.book(U.value)
    b.buy[0] = Row(0.003, 1, True)
    b.sell[2] = Row(-0.001, 1, True)
    saves: list[int] = []
    eng = AuctionOrderEngine(screen, FakeSystem(), save=lambda: saves.append(1))
    assert not any(r.on for r in b.buy + b.sell) and saves == [1]   # 시동: 끔 + 저장
    b.buy[0].on = True
    eng.set_run(U, "fx", True)
    await eng.shutdown()
    assert not b.buy[0].on and not eng._day(U).fx_run and saves == [1, 1]


async def test_moving_order_time_later_reruns_process1_same_day() -> None:
    # 사용자 2026-10-26 "시간 변경해서 다시 시도": 끝난 뒤 LS주문시각을 미래로 바꾸고 예약을 다시
    # 켜면 같은 날 다시 돈다(14:32 시험에서 "이미 돌았다"로 안 돌았던 문제). 낸 주문 행은 남긴다.
    eng, sys_, clk, screen = _engine(_wall(14, 30, 0.2))
    b = screen.book(U.value)
    b.settings.ls_hms, b.settings.ls_ms = "14:30:00", 100
    b.buy[0] = Row(0.003, 1, True)          # 1,767,000 > 1,766,000 → 나감
    b.buy[1] = Row(0.004, 1, True)          # 1,765,000 → 건너뜀
    eng._tick(U)
    await _settle(8)
    d = eng._day(U)
    assert d.ls_done and [i.tag for i in sys_.placed] == ["매수1"]
    assert not any(r.on for r in b.buy)     # 끝나면 자동 끔
    # 시각을 14:32:00으로 바꾸고(14:31에) 예약을 다시 켠다
    clk.wall = _wall(14, 31, 0.0)
    b.settings.ls_hms = "14:32:00"
    b.buy[1].on = True
    sys_.expected_prices[(U, SF)] = 1_764_000.0   # 이제 매수2(1,765,000)도 맞는다
    eng._tick(U)
    assert not d.ls_done and not d.ls_started and ("buy", 0) in d.rows and ("buy", 1) not in d.rows
    clk.wall = _wall(14, 32, 0.15)
    eng._tick(U)
    await _settle(8)
    assert [i.tag for i in sys_.placed] == ["매수1", "매수2"] and d.ls_done
    # 지난 시각으로 바꾸는 건 되돌리지 않는다
    b.settings.ls_hms = "14:00:00"
    eng._tick(U)
    assert d.ls_done
