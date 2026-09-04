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
            return self.s_entry, None
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

    async def place(self, intent: OrderIntent) -> str:
        self._ids += 1
        oid = f"O{self._ids}"
        self.placed.append(intent)
        self.order_book.track(oid, intent)
        return oid

    async def cancel(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self.order_book.on_cancel(order_id)


def _engine() -> tuple[AutoMEngine, FakeSystem, CoreState]:
    state = CoreState()
    state.screens[ScreenKind.AUTO_M].underlying = U
    sys_ = FakeSystem()
    eng = AutoMEngine(state, sys_)  # type: ignore[arg-type]
    s = state.autom.sets[0]
    s.target_qty, s.per_qty, s.en_sf, s.en_s, s.ex_sf = 100, 10, 0.005, 0.005, -0.001
    state.autom.settings.windows = (("09:00:00", "15:20:00"),)
    return eng, sys_, state


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_engine_round_trip_pre_fill_post_fill() -> None:
    # 실행 → 선주문 LS SF 매수 10 @201,000 → 4계약 체결 → 후주문 HL 매도 40(IOC) → 체결 → RT 4
    eng, sys_, state = _engine()
    now, mono = datetime(2026, 9, 4, 10, 0, 0), 100.0
    res = await _autom_command(eng, state, {"cmd": "autom_run", "set": 0, "block": "entry",
                                            "value": True})
    assert res["ok"]
    eng.tick(now, mono)
    await _settle()
    assert len(sys_.placed) == 1
    pre = sys_.placed[0]
    assert (pre.venue, pre.instrument, pre.side, pre.qty, pre.price, pre.source) == (
        Venue.LS, SF, Side.BUY, 10, 1_602_000.0, "자동M")
    s = state.autom.sets[0]
    assert s.entry.status is LegStatus.PRE_RESTING and s.entry.pre_order_id == "O1"

    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=4, price=1_602_000.0, ts=0))
    await _settle()
    post = sys_.placed[1]
    assert (post.venue, post.instrument, post.side, post.qty, post.order_type) == (
        Venue.HYPERLIQUID, Instrument.HL_PERP, Side.SELL, 40, OrderType.MARKET)
    assert s.entry.status is LegStatus.PRE_PARTIAL and s.entry.post_pending == 40

    sys_.order_book.on_fill(Fill(fill_id="f2", order_id="O2", qty=40, price=1184.0, ts=0))
    await _settle()
    assert s.rt == 4 and s.entry.post_pending == 0 and s.entry.acc.fx_avg() == 1355.9
    snap = eng.live_snapshot()
    live = snap["sets"][0]
    assert live["rt"] == 4 and live["entry"]["status"] == "pre_partial"
    assert live["entry"]["sprd"] is not None
    # 상단 모니터 3칸 — 정방향 진입 = 진입 스프레드, 역방향 진입 = 청산 스프레드(반대 호가창)
    assert snap["monitor"]["fwd"] == {"en_sf": 0.01, "en_s": 0.01, "ex_sf": -0.01}
    assert snap["monitor"]["rev"]["en_sf"] == -0.01 and snap["monitor"]["rev"]["ex_sf"] == 0.01
    res = await _autom_command(eng, state, {"cmd": "autom_ref_qty", "qty": 5})
    assert res["ok"] and state.autom.ref_qty == 5
    # HL 호가단위 옵션 — 1184.5 USD → 기준틱 0.1, 그 배수(일반주문창과 같은 표)
    ticks = [t["tick"] for t in snap["hl_merge_ticks"]]
    assert ticks[:3] == ["0.1", "0.2", "0.5"] and snap["hl_merge_active"] is None


async def test_engine_cancel_on_signal_loss_and_halt_on_post_reject() -> None:
    eng, sys_, state = _engine()
    now = datetime(2026, 9, 4, 10, 0, 0)
    eng.set_running(0, Block.ENTRY, True)
    eng.tick(now, 100.0)
    await _settle()
    sys_.s_entry = 0.0  # S괴리 미달 → 걸어둔 선주문 취소
    eng.tick(now, 101.0)
    await _settle()
    assert sys_.cancelled == ["O1"]
    s = state.autom.sets[0]
    assert s.entry.status is LegStatus.ARMED and s.entry.pre_order_id is None

    # 다시 조건 충족 → 신규 → 전량 체결 → 후주문이 IOC 잔량 취소(미체결 100) → 체결차 → 중지
    sys_.s_entry = 0.01
    eng.tick(now, 102.0)
    await _settle()
    sys_.order_book.on_fill(Fill(fill_id="f3", order_id="O2", qty=10, price=1_602_000.0, ts=0))
    await _settle()
    assert sys_.placed[-1].instrument is Instrument.HL_PERP
    sys_.order_book.on_cancel("O3")  # HL IOC 전량 미체결
    await _settle()
    assert s.entry.status is LegStatus.HALTED and "체결차" in s.entry.halt_reason
    assert sys_.error_seq == 1  # 에러 알람
    res = await _autom_command(eng, state, {"cmd": "autom_run", "set": 0, "block": "entry",
                                            "value": True})
    assert not res["ok"] and "중지" in res["errors"][0]
    await _autom_command(eng, state, {"cmd": "autom_release", "set": 0, "block": "entry"})
    assert s.entry.status is LegStatus.IDLE


async def test_autom_commands_set_settings_and_validation() -> None:
    eng, _sys, state = _engine()
    res = await _autom_command(eng, state, {
        "cmd": "autom_set", "set": 1, "target_qty": 50, "per_qty": 5, "switch_delay_s": 20,
        "en_sf": 0.004, "en_s": "", "ex_sf": -0.002, "rt_manual": 7})
    assert res["ok"]
    s1 = state.autom.sets[1]
    assert (s1.target_qty, s1.per_qty, s1.switch_delay_s, s1.en_sf, s1.en_s, s1.ex_sf, s1.rt) == (
        50, 5, 20, 0.004, None, -0.002, 7)
    res = await _autom_command(eng, state, {"cmd": "autom_run", "set": 1, "block": "entry",
                                            "value": True})
    assert not res["ok"] and "진입SF·진입S" in res["errors"][0]  # en_s 없음
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
    none = await _autom_command(None, state, {"cmd": "autom_run", "set": 0, "block": "entry",
                                              "value": True})
    assert not none["ok"] and "미접속" in none["errors"][0]


def test_autom_state_persists_inputs_not_runtime() -> None:
    # core_state.json 왕복: 입력값·RT·누적은 복원, 실행 상태(running/status/주문번호)는 초기화
    import dataclasses
    import json

    from kp_arb.strategy_core import state_from_dict

    state = CoreState()
    s = state.autom.sets[2]
    s.target_qty, s.per_qty, s.en_sf, s.rt = 30, 3, 0.007, 5
    s.entry.running, s.entry.status, s.entry.pre_order_id = True, LegStatus.PRE_RESTING, "X"
    s.entry.acc.hl_qty, s.entry.acc.fx_sum, s.entry.acc.hl_px_sum = 40, 1356 * 40, 1184 * 40
    state.autom.settings.rel_buy = 3
    raw: dict[str, Any] = json.loads(json.dumps(dataclasses.asdict(state), default=str))
    restored = state_from_dict(raw)
    r = restored.autom.sets[2]
    assert (r.target_qty, r.per_qty, r.en_sf, r.rt) == (30, 3, 0.007, 5)
    assert r.entry.acc.fx_avg() == 1356 and restored.autom.settings.rel_buy == 3
    assert not r.entry.running and r.entry.status is LegStatus.IDLE and r.entry.pre_order_id is None
