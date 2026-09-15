"""자동M 코어 결선(auto_m_engine) — 가짜 시스템 + 실제 OrderBook으로 한 판 재생. 라이브 없음."""
import asyncio
from datetime import datetime
from typing import Any

from kp_arb.auto_m import LegStatus
from kp_arb.auto_m_engine import AutoMEngine
from kp_arb.core_server import _autom_command
from kp_arb.domain.enums import Block, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent, Quote
from kp_arb.gateways.ls_ws import Fill
from kp_arb.order_book import OrderBook
from kp_arb.strategy_core import CoreState, ScreenKind

U = Underlying.SK_HYNIX
SF = Instrument.KR_STOCK_FUTURE


class FakeSystem:
    """엔진이 쓰는 LiveSystem 조각만 흉내 — 시세 고정, 발주는 기록 + OrderBook 등록."""

    def __init__(self) -> None:
        self.order_book = OrderBook()
        self.error_seq = 0
        self.placed: list[OrderIntent] = []
        self.cancelled: list[str] = []
        self.halted = False
        self._ids = 0
        self.on_hl_identified: list[Any] = []  # (cloid, oid) — 응답 전 식별 훅(결정 27)
        self.cloid: str | None = None          # new_hl_cloid()가 돌려줄 값(None=cloid 없음)
        from kp_arb.domain.models import InstrumentInfo

        # HL szDecimals=1 → 가격 소수 최대 5자리, 유효숫자 5자리(1184.5 → 소수 1자리)
        self.instruments = {(U, Instrument.HL_PERP): InstrumentInfo(
            underlying=U, instrument=Instrument.HL_PERP, code="xyz:SKHX",
            multiplier=1.0, sz_decimals=1)}
        self.quotes = {
            (U, Instrument.HL_PERP, "hl"): Quote(
                underlying=U, instrument=Instrument.HL_PERP, bid=1184.0, ask=1184.5, ts=0,
                market="hl", bids=[(1184.0, 500.0)], asks=[(1184.5, 500.0)]),
            (U, SF, "krx"): Quote(
                underlying=U, instrument=SF, bid=1_598_000.0, ask=1_605_000.0, ts=0,
                bids=[(1_598_000.0, 4.0)], asks=[(1_605_000.0, 5.0)]),
        }
        # HL 1184 USD × 1356 = 1,605,504원 vs 주식 1,600,000 → HL est 괴리 0.344%
        # 역산가 = 이론가 1,605,000 × (1 + 0.00344 − 0.005) = 1,602,496 → 틱 3,000 내림 1,602,000
        self.sf_entry, self.s_entry, self.sf_exit = 0.01, 0.01, -0.01

    def pair_signal(self, u: Underlying, instrument: Instrument, entry_qty: int,
                    exit_qty: int) -> tuple[float | None, float | None]:
        if instrument is Instrument.KR_STOCK:
            return self.s_entry, getattr(self, "s_exit", None)  # 매도 쪽 S괴리(역방향 진입 G5)
        return self.sf_entry, self.sf_exit

    def stock_futures_theory(self, underlying: Underlying,
                             instrument: Instrument = SF) -> float | None:
        return 1_605_000.0

    def stock_last(self, underlying: Underlying) -> float | None:
        return 1_600_000.0

    def usdkrw_effective(self, now: datetime | None = None) -> tuple[float | None, str]:
        return 1356.0, "현물"

    def fx_entry_rate(self, side: Side) -> float | None:
        return 1355.9 if side is Side.SELL else 1356.1

    def futures_halted(self) -> bool:
        return self.halted

    def new_hl_cloid(self) -> str | None:
        return self.cloid

    async def place(self, intent: OrderIntent, *, cloid: str | None = None) -> str:
        delay = getattr(self, "place_delay", 0.0)  # LS 접수 응답 지연 흉내(실측 0.9초)
        if delay > 0:
            await asyncio.sleep(delay)
        fail_pre = getattr(self, "fail_pre", None)  # LS 선주문 거부 흉내(RestError 문구)
        if fail_pre and intent.instrument is SF:
            from kp_arb.gateways.ls_rest import RestError

            raise RestError(fail_pre)
        self._ids += 1
        oid = f"O{self._ids}"
        self.placed.append(intent)
        if getattr(self, "fail_post", False) and intent.instrument is Instrument.HL_PERP:
            self.last_cloid = cloid  # 응답 유실 흉내 — 주문은 들어갔을 수 있다
            raise ConnectionError("Connection reset by peer")
        self.order_book.track(oid, intent)
        if cloid and intent.instrument is Instrument.HL_PERP:
            # 코어가 응답 전 통보(cloid)로 oid를 식별한 경우 흉내 — 체결·취소보다 먼저 알린다
            for cb in self.on_hl_identified:
                cb(cloid, oid)
        # HL 발주 응답에 즉시 체결분이 실려 오는 경우 흉내 — place()가 끝나기 전에 체결 훅이 돈다
        imm = getattr(self, "immediate_fill", 0.0)
        if imm > 0 and intent.instrument is Instrument.HL_PERP:
            from kp_arb.gateways.ls_ws import Fill

            self.order_book.on_fill(Fill(fill_id=f"imm-{oid}", order_id=oid,
                                         qty=imm, price=intent.price or 0.0, ts=0))
            # 거래소가 잔량을 버리고 끝낸 통보(orderUpdates filled, sz>0)가 발주 응답보다 먼저
            # 와서 replay로 장부 상태가 place() 안에서 이미 취소로 바뀌는 경우 흉내(실측 09-11)
            if getattr(self, "drop_remainder_before_return", False):
                self.order_book.on_cancel(oid)
        return oid

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls = getattr(self, "cancel_calls", 0) + 1
        if getattr(self, "cancel_gone", False):  # 체결과 교차 — LS 03416/01433(OrderGoneError)
            from kp_arb.gateways.ls import OrderGoneError

            raise OrderGoneError("CFOAT00300 rejected (03416): 정정취소가능수량이 없습니다.")
        fails = getattr(self, "cancel_fail_times", 0)  # LS 초당 한도 흉내 — 처음 n번 실패
        if fails > 0:
            self.cancel_fail_times = fails - 1
            raise RuntimeError("per-second limit 10 for CFOAT00300 exceeded")
        self.cancelled.append(order_id)
        self.order_book.on_cancel(order_id)


RUN = {"cmd": "autom_run", "underlying": U.value}  # 종목별 명령(2026-09-08) — 세트·진입/청산은 매번


def _engine(log_dir: Any = None) -> tuple[AutoMEngine, FakeSystem, CoreState]:
    state = CoreState()
    state.screens[ScreenKind.AUTO_M].underlying = U
    sys_ = FakeSystem()
    eng = AutoMEngine(state, sys_, log_dir=log_dir)  # type: ignore[arg-type]
    s = state.autom.book(U).sets[0]
    s.target_qty, s.per_qty, s.en_sf, s.en_s, s.ex_sf = 100, 10, 0.005, 0.005, -0.001
    state.autom.settings.windows = (("09:00:00", "15:20:00"),)
    return eng, sys_, state


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_immediate_partial_fill_is_applied_and_remainder_is_fractional() -> None:
    # 실측 2026-09-07: 후주문 10 중 0.588이 발주 응답에 즉시 체결로 실려 옴(등록 전 훅) →
    # 세트에 반영돼야 하고(hl_net −0.588, HL 대기 9.412), 잔량 취소 시 체결차 9.412(소수 그대로).
    eng, sys_, state = _engine()
    sys_.immediate_fill = 0.588
    now, mono = datetime(2026, 9, 4, 10, 0, 0), 100.0
    await _autom_command(eng, state, {**RUN, "set": 0, "block": "entry",
                                      "value": True})
    eng.tick(now, mono)
    await _settle()
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=1, price=1_602_000.0, ts=0))
    await _settle()
    s = state.autom.book(U).sets[0]
    assert s.hl_net == -0.588 and abs(s.entry.post_pending - 9.412) < 1e-9
    assert s.rt == 1  # RT는 선주문(SF) 체결 계약수 기준(2026-09-08) — HL 부분 체결과 무관
    sys_.order_book.on_cancel("O2")  # 잔량 취소 → 미체결 9.412 = 체결차 누적(한도 100 미만 → 계속)
    await _settle()
    assert s.entry.status is LegStatus.PRE_PARTIAL and s.entry.running  # 결정 25: 멈추지 않음
    assert abs(s.fill_diff - 9.412) < 1e-9 and s.entry.post_pending == 0


async def test_remainder_dropped_before_place_returns_finishes_round() -> None:
    # 실측 2026-09-11 10:18:35: HL이 후주문 10 중 5.963만 잡고 잔량 4.037을 버린 통보가 발주
    # 응답보다 49ms 먼저 와 replay로 장부는 취소가 됐는데, 그때 엔진은 주문번호를 몰라 지나쳤다
    # → 판이 'HL 4.037 대기'에서 영영 안 끝남. 등록 직후 상태를 한 번 더 훑어 판을 끝내야 한다.
    eng, sys_, state = _engine()
    sys_.immediate_fill = 5.963
    sys_.drop_remainder_before_return = True
    s = state.autom.book(U).sets[0]
    s.per_qty = 1  # 1회주문 1계약 → 후주문 10, 한도 10
    await _autom_command(eng, state, {**RUN, "set": 0, "block": "entry", "value": True})
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=1, price=1_602_000.0, ts=0))
    await _settle()
    assert abs(s.hl_net + 5.963) < 1e-9 and s.entry.post_pending == 0  # 대기 안 남음
    assert abs(s.fill_diff - 4.037) < 1e-9                              # 체결차 누적
    assert s.entry.status is not LegStatus.POST_PENDING                 # 판 종료
    assert s.entry.status is not LegStatus.HALTED and s.entry.running   # 4.037 < 한도 10 → 계속
    assert "O2" not in eng._orders                                      # 후주문 추적 정리


async def test_post_order_linked_by_cloid_before_place_returns() -> None:
    # 결정 27: 후주문 cloid를 발주 전에 세트에 묶어 두고, 코어가 통보로 oid를 식별하면 그 oid로
    # 등록 — 응답 전에 온 체결·잔량 버림 종료가 고아 보관 없이 바로 세트에 반영된다.
    eng, sys_, state = _engine()
    sys_.cloid = "0x" + "cd" * 16
    sys_.immediate_fill = 5.963
    sys_.drop_remainder_before_return = True
    s = state.autom.book(U).sets[0]
    s.per_qty = 1
    await _autom_command(eng, state, {**RUN, "set": 0, "block": "entry", "value": True})
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=1, price=1_602_000.0, ts=0))
    await _settle()
    assert abs(s.hl_net + 5.963) < 1e-9 and s.entry.post_pending == 0
    assert abs(s.fill_diff - 4.037) < 1e-9 and s.entry.running
    assert s.entry.status is not LegStatus.POST_PENDING
    assert eng._pending_refs == {} and "O2" not in eng._orders and eng._orphan_fills == {}


async def test_failed_post_order_relinked_when_identified_late() -> None:
    # 결정 30(사용자 확정 2026-09-11): 후주문 발주가 통신 오류로 끝나 거부로 처리(체결차 +10)했는데
    # 유예 안에 코어가 살아 있는 주문으로 식별하면 세트에 재연결 + HL 대기 도로 +10 → 뒤따르는
    # 체결로 장부(HL 잔고·체결차)가 바로잡힌다.
    eng, sys_, state = _engine()
    sys_.cloid = "0x" + "ef" * 16
    sys_.fail_post = True
    s = state.autom.book(U).sets[0]
    s.per_qty = 2  # 한도 20 — 후주문 10 통째 실패는 한도 미만이라 계속(중지 경우는 stop 테스트)
    await _autom_command(eng, state, {**RUN, "set": 0, "block": "entry", "value": True})
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=1, price=1_602_000.0, ts=0))
    await _settle()
    assert s.fill_diff == 10 and s.entry.post_pending == 0          # 거부 처리 — 판 끝, 계속
    assert s.entry.status is not LegStatus.HALTED and sys_.last_cloid == sys_.cloid
    assert sys_.cloid in eng._failed_refs
    # 코어가 유예 안에 통보로 식별(oid O9) — 장부 등록 뒤 훅
    post_intent = sys_.placed[-1]
    sys_.order_book.track("O9", post_intent)
    for cb in sys_.on_hl_identified:
        cb(sys_.cloid, "O9")
    assert s.entry.post_pending == 10 and "O9" in eng._orders and eng._failed_refs == {}
    sys_.order_book.on_fill(Fill(fill_id="f2", order_id="O9", qty=10, price=1184.0, ts=0))
    await _settle()
    assert s.hl_net == -10 and s.fill_diff == 0 and s.entry.post_pending == 0  # 장부 보정
    assert s.entry.status is not LegStatus.HALTED and s.entry.running


async def test_engine_reverse_round_sells_sf_then_buys_hl() -> None:
    # exec §7A(2026-09-14): 역방향 세트는 rev_sets에 있고 명령·주문 참조에 reverse가 붙는다.
    # 진입 = SF 매도 선주문(올림·매수호가 기준 한계) → 체결 → HL 매수 후주문 → RT −10, 체결차 0.
    eng, sys_, state = _engine()
    sys_.s_exit = -0.01  # 매도 쪽 S괴리 < 진입S 0.5% → 역방향 G5 통과
    book = state.autom.book(U)
    r = book.rev_sets[0]
    r.target_qty, r.per_qty, r.en_sf, r.en_s, r.ex_sf = 100, 10, 0.005, 0.005, -0.001
    await _autom_command(eng, state, {**RUN, "set": 0, "block": "entry", "value": True,
                                      "direction": "rev"})
    assert r.entry.running and not book.sets[0].entry.running  # 정방향 세트는 그대로
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    assert len(sys_.placed) == 1 and sys_.placed[0].side is Side.SELL   # 선주문 SF 매도
    # HL 매도호가 1184.5×1356 = 1,606,182 → 괴리 +0.386% → P = 1,605,000×(1+0.00386−0.005)
    # = 1,603,170 → 3,000 올림 1,605,000 ≤ 한계 (1,598,000+1,000)×1.004 = 1,605,396
    assert sys_.placed[0].price == 1_605_000
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=10, price=1_605_000.0, ts=0))
    await _settle()
    assert len(sys_.placed) == 2 and sys_.placed[1].side is Side.BUY    # 후주문 HL 매수 100
    assert sys_.placed[1].qty == 100 and r.rt == -10 and r.sf_net == -10
    sys_.order_book.on_fill(Fill(fill_id="f2", order_id="O2", qty=100, price=1184.5, ts=0))
    await _settle()
    assert r.hl_net == 100 and r.fill_diff == 0 and r.entry.status is LegStatus.SETTLE_DELAY
    snap = eng.live_snapshot()[U.value]
    assert snap["rev_sets"][0]["rt"] == -10 and snap["rev_sets"][0]["reverse"] is True
    assert snap["sf_tick"] == 1000  # 세트설정 기준배수 검사용 SF 호가단위(2026-09-15, 160만 원대)
    assert snap["sets"][0]["rt"] == 0
    assert eng._tag(U, 0, Block.ENTRY, True) == "역방향 1세트 진입"


async def test_engine_saves_state_after_fills_and_halt() -> None:
    # RT·체결차·순잔고는 체결로 바뀐다 → 명령 때만 저장하면 재시동 때 잃는다(사용자 2026-09-07).
    state = CoreState()
    state.screens[ScreenKind.AUTO_M].underlying = U
    sys_ = FakeSystem()
    saves: list[int] = []
    eng = AutoMEngine(state, sys_, save=lambda: saves.append(1))  # type: ignore[arg-type]
    s = state.autom.book(U).sets[0]
    s.target_qty, s.per_qty, s.en_sf, s.en_s, s.ex_sf = 100, 10, 0.005, 0.005, -0.001
    state.autom.settings.windows = (("09:00:00", "15:20:00"),)
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    before = len(saves)
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=2, price=1_602_000.0, ts=0))
    await _settle()
    assert len(saves) > before  # 선주문 체결 → 저장
    before = len(saves)
    sys_.order_book.on_cancel("O2")  # 후주문이 밖에서 끝남 → 체결차 20 누적(한도 100 미만) → 저장
    await _settle()
    assert len(saves) > before and s.fill_diff == 20 and s.entry.status is LegStatus.PRE_PARTIAL


async def test_state_save_runs_after_post_order_is_sent() -> None:
    # 2026-09-14: 체결마다 하는 상태 저장(파일 읽기·비교·세대 회전·교체, 수 ms)이 체결 처리 안에서
    # 돌아 방금 예약한 후주문 태스크를 늦췄다 → 저장은 후주문 전송 뒤로(call_soon).
    state = CoreState()
    state.screens[ScreenKind.AUTO_M].underlying = U
    sys_ = FakeSystem()
    order: list[str] = []
    real_place = sys_.place

    async def place(intent: OrderIntent, *, cloid: str | None = None) -> str:
        order.append(f"place:{intent.instrument.value}")
        return await real_place(intent, cloid=cloid)

    sys_.place = place  # type: ignore[method-assign]
    eng = AutoMEngine(state, sys_, save=lambda: order.append("save"))  # type: ignore[arg-type]
    s = state.autom.book(U).sets[0]
    s.target_qty, s.per_qty, s.en_sf, s.en_s, s.ex_sf = 100, 10, 0.005, 0.005, -0.001
    state.autom.settings.windows = (("09:00:00", "15:20:00"),)
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    order.clear()
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=10, price=1_602_000.0, ts=0))
    await _settle()
    assert order[:2] == ["place:hl_perp", "save"]  # 후주문 전송이 저장보다 먼저


async def test_books_run_independently_per_underlying() -> None:
    # 사용자 확정 2026-09-08: 삼성이 도는 중에도 다른 창에서 하이닉스를 따로 돌린다.
    # 종목별 책 — 한 종목 실행/정지가 다른 종목에 영향 없음, 스냅샷은 종목 키로.
    eng, sys_, state = _engine()
    other = Underlying.SAMSUNG
    ob = state.autom.book(other)
    ob.sets[0].target_qty, ob.sets[0].per_qty = 100, 10
    ob.sets[0].en_sf, ob.sets[0].en_s, ob.sets[0].ex_sf = 0.005, 0.005, -0.001
    eng.set_running(U, 0, Block.ENTRY, True)
    assert state.autom.book(U).any_running() and not ob.any_running()
    assert state.autom.running_underlyings() == [U.value]
    snap = eng.live_snapshot()
    assert snap[U.value]["any_running"] is True and snap[other.value]["any_running"] is False
    res = await _autom_command(eng, state, {"cmd": "autom_run", "underlying": other.value,
                                            "set": 0, "block": "entry", "value": True})
    assert res["ok"] and ob.any_running()
    eng.stop_all(other)  # 창 닫기 = 그 창 종목만 정지
    assert not ob.any_running() and state.autom.book(U).any_running()
    eng.stop_all()       # 안전종료 = 전 종목
    assert not state.autom.any_running()
    # 월물·기준수량도 종목별
    await _autom_command(eng, state, {"cmd": "autom_month", "underlying": other.value,
                                      "month": "next"})
    assert ob.future_month == "next" and state.autom.book(U).future_month == "near"
    bad = await _autom_command(eng, state, {"cmd": "autom_month", "underlying": other.value,
                                            "month": "far"})
    assert not bad["ok"]


def test_legacy_single_autom_state_migrates_to_screen_underlying_book() -> None:
    # 2026-09-08 이전 저장 형식(단일 sets·ref_qty)은 그때 자동M 창이 가리키던 종목 책으로 이전
    from kp_arb.strategy_core import state_from_dict

    raw = {"screens": {"autoM": {"kind": "autoM", "underlying": "samsung"}},
           "autom": {"sets": [{"target_qty": 7, "per_qty": 2, "rt": 3, "sf_net": 1,
                               "hl_net": -10.0}], "ref_qty": 300}}
    st = state_from_dict(raw)
    b = st.autom.book(Underlying.SAMSUNG)
    assert (b.sets[0].target_qty, b.sets[0].per_qty, b.sets[0].rt, b.ref_qty) == (7, 2, 3, 300)
    assert st.autom.book(Underlying.SK_HYNIX).sets[0].target_qty == 0  # 다른 종목은 빈 책


async def test_shutdown_waits_for_cancel_and_retries_rate_limit() -> None:
    # 실측 2026-09-08: 종료 때 취소가 한도에 걸려 실패 → 0.3초 대기 뒤 닫혀 선주문이 LS에 남음.
    # 종료는 취소 요청을 재시도하고 끝날 때까지 기다린다.
    eng, sys_, state = _engine()
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    assert state.autom.book(U).sets[0].entry.pre_order_id == "O1"
    sys_.cancel_fail_times = 1  # 첫 취소는 한도 초과로 실패 → 0.6초 뒤 재시도 성공
    await eng.shutdown(timeout_s=3.0)
    assert sys_.cancelled == ["O1"]
    assert not state.autom.any_running()
    assert state.autom.book(U).sets[0].entry.pre_order_id is None  # 취소 확인까지 반영됨


async def test_stop_during_placement_cancels_on_ack() -> None:
    # 실측 2026-09-09 #13865: 발주 요청 중(응답 전) 실행 끔 → 응답이 오면 즉시 취소해야 한다.
    eng, sys_, state = _engine()
    sys_.place_delay = 0.05  # LS 응답이 늦게 온다
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()                               # 발주 요청은 나갔지만 응답 전
    s = state.autom.book(U).sets[0]
    assert s.entry.pre_order_id is None and sys_.placed == []
    eng.set_running(U, 0, Block.ENTRY, False)     # 응답 전에 실행 끔 → 취소할 번호 없음
    assert s.entry.status is LegStatus.IDLE
    await asyncio.sleep(0.1)                      # 응답 도착 → 접수 → 실행 꺼짐 → 즉시 취소
    await _settle()
    assert [p.instrument for p in sys_.placed] == [SF]
    assert sys_.cancelled == ["O1"] and s.entry.pre_order_id is None


async def test_late_pre_fill_after_stop_still_hedges() -> None:
    # 사용자 확정 2026-09-09(결정 로그 19): 진입 중지 → 취소 실패 → 뒤늦게 선주문 체결이 오면
    # 주문번호 표로 우리 선주문임이 확실하므로 그대로 후주문(헤지)을 낸다.
    eng, sys_, state = _engine()
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    s = state.autom.book(U).sets[0]
    assert s.entry.pre_order_id == "O1"
    sys_.cancel_fail_times = 10                   # 취소가 계속 한도에 막힘 → 선주문이 남아 있음
    eng.set_running(U, 0, Block.ENTRY, False)
    await _settle()
    assert sys_.cancelled == [] and sys_.order_book.order("O1") is not None
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=10, price=1_602_000.0, ts=0))
    await _settle()
    assert [p.instrument for p in sys_.placed] == [SF, Instrument.HL_PERP]
    assert sys_.placed[1].qty == 100 and s.entry.post_pending == 100  # 10계약 × 10
    assert s.entry.status is LegStatus.POST_PENDING and not s.entry.running
    for task in list(eng._bg):                    # 남은 취소 재시도 정리
        task.cancel()
    await _settle()


def test_reject_reason_text_shortens_ls_rejection() -> None:
    from kp_arb.auto_m_engine import reject_reason_text

    assert reject_reason_text("CFOAT00100 rejected (02752): 증거금부족으로 주문이 불가합니다.") == \
        "LS 02752 증거금부족으로 주문이 불가합니다."
    assert reject_reason_text("daily cap 5000 exceeded") == "daily cap 5000 exceeded"
    assert reject_reason_text("x" * 200).endswith("…") and len(reject_reason_text("x" * 200)) == 90


async def test_pre_reject_reason_reaches_snapshot() -> None:
    # 거부 → 스냅샷 entry.reject/reject_at(상태줄 표시) — 재접수되면 비워진다.
    eng, sys_, state = _engine()
    sys_.fail_pre = "CFOAT00100 rejected (02752): 증거금부족으로 주문이 불가합니다."
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    row = eng.live_snapshot()[U.value]["sets"][0]["entry"]
    assert row["reject"] == "선주문 거부(1/3): LS 02752 증거금부족으로 주문이 불가합니다."
    assert len(row["reject_at"]) == 8  # HH:MM:SS
    sys_.fail_pre = None
    # 거부 뒤 딜레이는 실제 단조시계 기준(엔진의 time.monotonic) — 테스트 시계로는 안 지나가서 지움
    state.autom.book(U).sets[0].entry.delay_until = None
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 200.0)  # 딜레이 뒤 재발주 → 접수
    await _settle()
    row = eng.live_snapshot()[U.value]["sets"][0]["entry"]
    assert row["reject"] == "" and row["pre_order_id"] is not None


async def test_cancel_crossed_by_fill_is_not_retried() -> None:
    # 운영 실측 2026-09-14 #6548: 발주 55ms 뒤 체결, 그 사이 낸 취소는 LS 03416(잔량 없음).
    # 옛 코드는 3회 재시도(2·3회째는 잔량 0으로 보내 02897)로 경고 20줄. 잔량 없음은 경합이지
    # 실패가 아니다 — 한 번으로 끝내고 재시도·취소실패 표시 없이 체결 통보에 맡긴다.
    eng, sys_, state = _engine()
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    s = state.autom.book(U).sets[0]
    assert s.entry.pre_order_id == "O1"
    sys_.cancel_gone = True
    eng.set_running(U, 0, Block.ENTRY, False)     # 실행 끔 → 취소 → 03416
    await asyncio.sleep(1.5)                      # 재시도 간격(0.6초×2)보다 길게 기다려도
    assert sys_.cancel_calls == 1                 # 한 번만 보냈고 재시도 없음
    assert s.entry.cancel_tries == 1 and not s.entry.cancel_alarmed
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=10, price=1_602_000.0, ts=0))
    await _settle()                               # 체결 통보가 정리 → 후주문(헤지)
    assert [p.instrument for p in sys_.placed] == [SF, Instrument.HL_PERP]
    for task in list(eng._bg):
        task.cancel()
    await _settle()


async def test_cancel_alarm_raises_error_seq_and_snapshot_flags_it() -> None:
    # exec ㅂ3: 취소 재전송이 한도를 넘으면 에러 알람(error_seq) + 스냅샷 cancel_failed(상태줄).
    eng, sys_, state = _engine()
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    s = state.autom.book(U).sets[0]
    sys_.cancel_fail_times = 100                  # 취소 요청이 계속 실패(확인도 안 옴)
    eng.set_running(U, 0, Block.ENTRY, False)     # 1회
    before = sys_.error_seq
    mono = 100.0
    for _ in range(3):                            # 3초마다 재전송 → 4회째에 알람
        mono += 3.0
        eng.tick(datetime(2026, 9, 4, 10, 0, 0), mono)
    assert s.entry.cancel_tries == 4 and s.entry.cancel_alarmed
    assert sys_.error_seq == before + 1
    row = eng.live_snapshot()[U.value]["sets"][0]["entry"]
    assert row["cancel_failed"] is True and row["cancel_tries"] == 4
    for task in list(eng._bg):
        task.cancel()
    await _settle()


async def test_release_keeps_ledger_and_logs_fill_before_action(tmp_path: Any) -> None:
    # 결정 로그 22: 해제는 장부·후주문 추적 유지(0은 "체결차 Clear"로만) / 체결 줄이 행동 줄보다
    # 먼저 + 장부 원값.
    import logging

    logging.getLogger("kp_arb.autom.sk_hynix").handlers.clear()  # 다른 테스트의 조용한 핸들러 제거
    eng, sys_, state = _engine(log_dir=tmp_path)
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    s = state.autom.book(U).sets[0]
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=10, price=1_602_000.0, ts=0))
    await _settle()                                       # 후주문 O2(100) 발주됨
    assert s.entry.post_pending == 100 and "O2" in eng._orders
    sys_.order_book.on_fill(Fill(fill_id="f2", order_id="O2", qty=40, price=1184.0, ts=0))
    await _settle()
    assert s.fill_diff == 60                              # 칸 = 장부 실시간(SF 10, HL −40)
    s.per_qty = 5                                         # 한도 50 → 잔량 60은 한도 이상
    sys_.order_book.on_cancel("O2")                       # 잔량 60 밖에서 취소 → 60 ≥ 50 → 중지
    await _settle()
    assert s.entry.status is LegStatus.HALTED and "한도 50" in s.entry.halt_reason
    eng.release(U, 0, Block.ENTRY)                        # 해제 — 장부·후주문 추적 그대로
    assert (s.sf_net, s.hl_net, s.fill_diff) == (10, -40.0, 60.0)  # 장부 그대로(Clear는 사용자)
    for h in logging.getLogger("kp_arb.autom.sk_hynix").handlers:
        h.flush()
    text = "\n".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("autom_*.log"))
    fill_at = text.index("체결 정방향 1세트 진입: 후주문 #O2")
    assert "장부 SF 10 HL -40 체결차 60" in text
    assert text.index("행동 정방향 1세트 진입: halt") > fill_at
    assert "중지 해제(세트 단위) — 장부 SF 10 HL -40 체결차 60 (장부는 유지" in text


async def test_vanished_pre_order_is_cleared_and_vanished_post_order_halts() -> None:
    # 실측 2026-09-09 #20851: 재동기가 장부에서 선주문을 지워 상태는 '접수'인데 장부엔 없음 →
    # 'unknown order' 취소 되풀이. 장부에서 사라진 선주문은 취소로 정리, 후주문은 체결차 → 중지.
    eng, sys_, state = _engine()
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    s = state.autom.book(U).sets[0]
    assert s.entry.pre_order_id == "O1"
    sys_.order_book.load_snapshot(open_orders=(), reconcile_accounts=None)  # 유령 정리처럼 삭제
    for o in list(sys_.order_book._orders):  # 유예(15초) 안이라 남았으면 강제로 지움
        del sys_.order_book._orders[o]
    eng._on_book_change()
    assert s.entry.pre_order_id is None and s.entry.status is LegStatus.ARMED  # 다시 감시
    assert "O1" not in eng._orders
    # 후주문이 사라지면: 미체결분만큼 체결차 → 세트 중지
    eng2, sys2, state2 = _engine()
    eng2.set_running(U, 0, Block.ENTRY, True)
    eng2.tick(datetime(2026, 9, 4, 10, 0, 0), 100.0)
    await _settle()
    sys2.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=10, price=1_602_000.0, ts=0))
    await _settle()
    s2 = state2.autom.book(U).sets[0]
    assert s2.entry.status is LegStatus.POST_PENDING and s2.entry.post_pending == 100
    for o in list(sys2.order_book._orders):
        del sys2.order_book._orders[o]
    eng2._on_book_change()
    assert s2.entry.status is LegStatus.HALTED and "100" in s2.entry.halt_reason


async def test_engine_round_trip_pre_fill_post_fill() -> None:
    # 실행 → 선주문 LS SF 매수 10 @201,000 → 4계약 체결 → 후주문 HL 매도 40(IOC) → 체결 → RT 4
    eng, sys_, state = _engine()
    now, mono = datetime(2026, 9, 4, 10, 0, 0), 100.0
    res = await _autom_command(eng, state, {**RUN, "set": 0, "block": "entry",
                                            "value": True})
    assert res["ok"]
    eng.tick(now, mono)
    await _settle()
    assert len(sys_.placed) == 1
    pre = sys_.placed[0]
    assert (pre.venue, pre.instrument, pre.side, pre.qty, pre.price, pre.source) == (
        Venue.LS, SF, Side.BUY, 10, 1_602_000.0, "자동M")
    s = state.autom.book(U).sets[0]
    assert s.entry.status is LegStatus.PRE_RESTING and s.entry.pre_order_id == "O1"

    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=4, price=1_602_000.0, ts=0))
    await _settle()
    post = sys_.placed[1]
    # 후주문도 지정가(Gtc)만 — HL 매도 = 매수1호가 1184 × (1 − 1%) = 1172.16 (사용자 확정)
    assert (post.venue, post.instrument, post.side, post.qty, post.order_type) == (
        Venue.HYPERLIQUID, Instrument.HL_PERP, Side.SELL, 40, OrderType.LIMIT)
    # 1172.16은 유효숫자 6자리 → HL 거부(실측 09-07). 매도는 내림 → 1172.1(격자 맞춤)
    assert post.price == 1172.1
    assert s.entry.status is LegStatus.PRE_PARTIAL and s.entry.post_pending == 40

    sys_.order_book.on_fill(Fill(fill_id="f2", order_id="O2", qty=40, price=1184.0, ts=0))
    await _settle()
    assert s.rt == 4 and s.entry.post_pending == 0 and s.entry.acc.fx_avg() == 1355.9
    snap = eng.live_snapshot()[U.value]  # 종목별 스냅샷(2026-09-08)
    live = snap["sets"][0]
    assert live["rt"] == 4 and live["entry"]["status"] == "pre_partial"
    assert live["entry"]["sprd"] is not None
    # 상단 모니터 3칸 — 정방향 진입 = 진입 스프레드, 역방향 진입 = 청산 스프레드(반대 호가창)
    assert snap["monitor"]["fwd"] == {"en_sf": 0.01, "en_s": 0.01, "ex_sf": -0.01}
    assert snap["monitor"]["rev"]["en_sf"] == -0.01 and snap["monitor"]["rev"]["ex_sf"] == 0.01
    res = await _autom_command(eng, state, {"cmd": "autom_ref_qty", "underlying": U.value,
                                            "qty": 5})
    assert res["ok"] and state.autom.book(U).ref_qty == 5
    # HL 호가단위 옵션 — 1184.5 USD → 기준틱 0.1, 그 배수(일반주문창과 같은 표)
    ticks = [t["tick"] for t in snap["hl_merge_ticks"]]
    assert ticks[:3] == ["0.1", "0.2", "0.5"] and snap["hl_merge_active"] is None


async def test_engine_writes_per_underlying_log(tmp_path: Any) -> None:
    # 종목별 상세 로그(logs/autom_<종목>_날짜.log): 명령·판정 근거(바뀔 때만)·상태 전이·행동·체결
    import logging

    from kp_arb.auto_m_engine import AutoMEngine as _E

    logging.getLogger("kp_arb.autom.sk_hynix").handlers.clear()  # 다른 테스트의 조용한 핸들러 제거
    eng, sys_, state = _engine(log_dir=tmp_path)
    assert isinstance(eng, _E)
    now = datetime(2026, 9, 4, 10, 0, 0)
    eng.set_running(U, 0, Block.ENTRY, True)
    sys_.s_entry = 0.0  # 처음엔 G5 미달 — 이 근거는 파일에 안 남긴다(사용자 2026-09-10, 로그 도배)
    eng.tick(now, 99.0)
    sys_.s_entry = 0.01
    eng.tick(now, 100.0)
    eng.tick(now, 100.1)  # 같은 판정 → 로그 추가 없음
    await _settle()
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=4, price=1_602_000.0, ts=0))
    await _settle()
    sys_.order_book.on_fill(Fill(fill_id="f2", order_id="O2", qty=40, price=1183.5, ts=0))
    await _settle()
    for h in logging.getLogger("kp_arb.autom.sk_hynix").handlers:
        h.flush()
    files = list(tmp_path.glob("autom_sk_hynix_*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "G5 미달" not in text
    # 세트 표기 = "정방향 N세트 진입/청산" — 정/역 구분이 로그에 보이게(사용자 2026-09-08)
    assert "명령 정방향 1세트 진입: 실행 켬" in text  # 종류 뒤 대상, ':' 뒤 내용 — 전 줄 통일
    assert text.count("판정 정방향 1세트 진입: 통과") == 1  # 바뀔 때만
    assert ("상태 정방향 1세트 진입: - → armed" in text
            and "armed → pre_resting" in text)  # 전이 순서
    assert "행동 정방향 1세트 진입: place_pre buy 10 1602000" in text
    assert "체결 정방향 1세트 진입: 선주문 #O1" in text and "누적 4/10, HL 대기 40" in text
    assert "행동 정방향 1세트 진입: place_post sell 40" in text
    # 발주 때 보관한 est와 체결 순간 다시 계산한 est를 나란히(사용자 2026-09-15) — 호가창이
    # 그대로면 둘이 같다(매수호가 1184 × 500 → 40계약 est 1184). 후주문 체결가 대비 차이는 유리 +.
    assert "선주문 #O1 4 @ 1.602e+06 기준est 1184 현est 1184 → 누적" in text
    assert "후주문 #O2 HL 40 @ 1183.5 기준est 1184 차이 -0.5(-0.042%) 현est 1184 " in text


async def test_engine_cancel_on_signal_loss_and_halt_on_post_reject() -> None:
    eng, sys_, state = _engine()
    now = datetime(2026, 9, 4, 10, 0, 0)
    eng.set_running(U, 0, Block.ENTRY, True)
    eng.tick(now, 100.0)
    await _settle()
    sys_.s_entry = 0.0  # S괴리 미달 → 걸어둔 선주문 취소
    eng.tick(now, 101.0)
    await _settle()
    assert sys_.cancelled == ["O1"]
    s = state.autom.book(U).sets[0]
    assert s.entry.status is LegStatus.ARMED and s.entry.pre_order_id is None

    # 다시 조건 충족 → 신규 → 전량 체결 → 후주문(지정가) 발주 → 잔량이 걸려 있어도 엔진은 취소 안 함
    sys_.s_entry = 0.01
    eng.tick(now, 102.0)
    await _settle()
    sys_.order_book.on_fill(Fill(fill_id="f3", order_id="O2", qty=10, price=1_602_000.0, ts=0))
    await _settle()
    assert sys_.placed[-1].instrument is Instrument.HL_PERP
    # 후주문은 취소하지 않는다(사용자 확정 2026-09-07) — 시간이 아무리 지나도 후주문대기 유지
    eng.tick(now, 102.5)
    eng.tick(now, 200.0)
    await _settle()
    assert "O3" not in sys_.cancelled
    assert s.entry.status is LegStatus.POST_PENDING and s.entry.post_pending == 100
    # 밖에서(사람·거래소) 취소되면 미체결분만큼 체결차 → 중지 + 에러 알람
    sys_.order_book.on_cancel("O3")
    await _settle()
    assert s.entry.status is LegStatus.HALTED and "체결차" in s.entry.halt_reason
    assert sys_.error_seq == 1  # 에러 알람
    res = await _autom_command(eng, state, {**RUN, "set": 0, "block": "entry",
                                            "value": True})
    assert not res["ok"] and "중지" in res["errors"][0]
    # 중지는 세트 단위 — 다른 쪽(청산)도 못 켠다. 한쪽만 중지로 남은 상태(실측 2026-09-15 역방향
    # 청산 중지 + 진입 실행)도 막는다.
    assert s.exit.status is LegStatus.HALTED
    res = await _autom_command(eng, state, {**RUN, "set": 0, "block": "exit", "value": True})
    assert not res["ok"] and "세트 중지 상태(진입·청산)" in res["errors"][0]
    s.entry.status = LegStatus.IDLE  # 진입만 풀린 비정상 상태를 흉내
    res = await _autom_command(eng, state, {**RUN, "set": 0, "block": "entry", "value": True})
    assert not res["ok"] and "세트 중지 상태(청산)" in res["errors"][0]
    s.entry.status = LegStatus.HALTED
    await _autom_command(eng, state, {"cmd": "autom_release", "underlying": U.value, "set": 0,
                                      "block": "entry"})
    assert s.entry.status is LegStatus.IDLE and s.exit.status is LegStatus.IDLE


async def test_autom_commands_set_settings_and_validation() -> None:
    eng, _sys, state = _engine()
    res = await _autom_command(eng, state, {
        "cmd": "autom_set", "underlying": U.value, "set": 1, "target_qty": 50, "per_qty": 5,
        "switch_delay_s": 20,
        "en_sf": 0.004, "en_s": "", "ex_sf": -0.002, "rt_manual": 7})
    assert res["ok"]
    s1 = state.autom.book(U).sets[1]
    assert (s1.target_qty, s1.per_qty, s1.switch_delay_s, s1.en_sf, s1.en_s, s1.ex_sf, s1.rt) == (
        50, 5, 20, 0.004, None, -0.002, 7)
    res = await _autom_command(eng, state, {**RUN, "set": 1, "block": "entry",
                                            "value": True})
    assert not res["ok"] and "진입SF·진입S" in res["errors"][0]  # en_s 없음
    # RT 수동 입력 부호(2026-09-15): 정방향은 음수 거부, 역방향은 0 또는 음수만(양수 거부)
    res = await _autom_command(eng, state, {"cmd": "autom_set", "underlying": U.value,
                                            "set": 1, "rt_manual": -2})
    assert not res["ok"] and "정방향 RT" in res["errors"][0] and s1.rt == 7
    res = await _autom_command(eng, state, {"cmd": "autom_set", "underlying": U.value,
                                            "direction": "rev", "set": 0, "rt_manual": -3})
    assert res["ok"] and state.autom.book(U).rev_sets[0].rt == -3
    res = await _autom_command(eng, state, {"cmd": "autom_set", "underlying": U.value,
                                            "direction": "rev", "set": 0, "rt_manual": 2})
    assert not res["ok"] and "역방향 RT" in res["errors"][0]
    assert state.autom.book(U).rev_sets[0].rt == -3
    # 주문가 기준배수(2026-09-15): 저장되고, 음수는 거부
    res = await _autom_command(eng, state, {"cmd": "autom_set", "underlying": U.value,
                                            "set": 1, "price_offset": 1000})
    assert res["ok"] and s1.price_offset == 1000
    res = await _autom_command(eng, state, {"cmd": "autom_set", "underlying": U.value,
                                            "set": 1, "price_offset": -500})
    assert not res["ok"] and s1.price_offset == 1000
    # 누적 clear는 set "all"로 그 방향 전 세트를 한 번에(화면 깜빡임 원인 제거, 2026-09-15)
    for st in state.autom.book(U).sets:
        st.entry.acc.hl_qty = 5.0
    res = await _autom_command(eng, state, {"cmd": "autom_clear_acc", "underlying": U.value,
                                            "set": "all", "block": "entry"})
    assert res["ok"] and all(st.entry.acc.hl_qty == 0 for st in state.autom.book(U).sets)
    res = await _autom_command(eng, state, {
        "cmd": "autom_settings", "windows": [["08:30:10", "08:46:20"], ["15:35:30", "15:46:55"]],
        "pre_tick": {"sk_hynix": 3000, "samsung": 500, "hyundai": 1000}, "pre_delay_ms": 1500,
        "resume_delay_s": 12, "pre_range": 0.005, "rel_buy": 2, "rel_sell": 3,
        "risk_fwd_en": 0.0, "risk_fwd_ex": 0.006, "risk_fwd_gap": 0.002})
    assert res["ok"]
    st = state.autom.settings
    assert (st.pre_delay_ms, st.resume_delay_s, st.pre_range, st.rel_buy, st.rel_sell) == (
        1500, 12, 0.005, 2, 3)
    assert st.pre_tick[Underlying.SAMSUNG] == 500 and state.autom.risk_fwd_ex == 0.006
    bad = await _autom_command(eng, state, {"cmd": "autom_settings", "windows": [["25:00", "x"]]})
    assert not bad["ok"]
    none = await _autom_command(None, state, {**RUN, "set": 0, "block": "entry",
                                              "value": True})
    assert not none["ok"] and "미접속" in none["errors"][0]


def test_autom_state_persists_inputs_not_runtime() -> None:
    # core_state.json 왕복: 입력값·RT·누적은 복원, 실행 상태(running/status/주문번호)는 초기화
    import dataclasses
    import json

    from kp_arb.strategy_core import state_from_dict

    state = CoreState()
    s = state.autom.book(U).sets[2]
    s.target_qty, s.per_qty, s.en_sf, s.rt = 30, 3, 0.007, 5
    s.entry.running, s.entry.status, s.entry.pre_order_id = True, LegStatus.PRE_RESTING, "X"
    s.entry.acc.hl_qty, s.entry.acc.fx_sum, s.entry.acc.hl_px_sum = 40, 1356 * 40, 1184 * 40
    state.autom.settings.rel_buy = 3
    raw: dict[str, Any] = json.loads(json.dumps(dataclasses.asdict(state), default=str))
    restored = state_from_dict(raw)
    r = restored.autom.book(U).sets[2]
    assert (r.target_qty, r.per_qty, r.en_sf, r.rt) == (30, 3, 0.007, 5)
    assert r.entry.acc.fx_avg() == 1356 and restored.autom.settings.rel_buy == 3
    assert not r.entry.running and r.entry.status is LegStatus.IDLE and r.entry.pre_order_id is None
