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
        # HL 발주 응답에 즉시 체결분이 실려 오는 경우 흉내 — place()가 끝나기 전에 체결 훅이 돈다
        imm = getattr(self, "immediate_fill", 0.0)
        if imm > 0 and intent.instrument is Instrument.HL_PERP:
            from kp_arb.gateways.ls_ws import Fill

            self.order_book.on_fill(Fill(fill_id=f"imm-{oid}", order_id=oid,
                                         qty=imm, price=intent.price or 0.0, ts=0))
        return oid

    async def cancel(self, order_id: str) -> None:
        fails = getattr(self, "cancel_fail_times", 0)  # LS 초당 한도 흉내 — 처음 n번 실패
        if fails > 0:
            self.cancel_fail_times = fails - 1
            raise RuntimeError("per-second limit 10 for CFOAT00300 exceeded")
        self.cancelled.append(order_id)
        self.order_book.on_cancel(order_id)


RUN = {"cmd": "autom_run", "underlying": U.value}  # 종목별 명령(2026-09-08) — 세트·다리는 호출마다


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
    sys_.order_book.on_cancel("O2")  # 잔량 취소 확인 → 미체결 9.412 체결차 → 중지
    await _settle()
    assert s.entry.status is LegStatus.HALTED and not s.entry.running
    assert abs(s.fill_diff - 9.412) < 1e-9
    assert "9.412" in s.entry.halt_reason


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
    sys_.order_book.on_cancel("O2")  # 후주문이 밖에서 끝남 → 체결차 → 중지 → 저장
    await _settle()
    assert len(saves) > before and s.entry.status is LegStatus.HALTED


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
    eng.tick(now, 100.0)
    eng.tick(now, 100.1)  # 같은 판정 → 로그 추가 없음
    await _settle()
    sys_.order_book.on_fill(Fill(fill_id="f1", order_id="O1", qty=4, price=1_602_000.0, ts=0))
    await _settle()
    for h in logging.getLogger("kp_arb.autom.sk_hynix").handlers:
        h.flush()
    files = list(tmp_path.glob("autom_sk_hynix_*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    # 세트 표기 = "정방향 N세트 진입/청산" — 정/역 구분이 로그에 보이게(사용자 2026-09-08)
    assert "명령 정방향 1세트 진입 실행 켬" in text
    assert text.count("판정 정방향 1세트 진입: 통과") == 1  # 바뀔 때만
    assert ("상태 정방향 1세트 진입: - → armed" in text
            and "armed → pre_resting" in text)  # 전이 순서
    assert "행동 정방향 1세트 진입: place_pre buy 10 1602000" in text
    assert "체결 정방향 1세트 진입 선주문 #O1" in text and "누적 4/10, HL 대기 40" in text
    assert "행동 정방향 1세트 진입: place_post sell 40" in text


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
    await _autom_command(eng, state, {"cmd": "autom_release", "underlying": U.value, "set": 0,
                                      "block": "entry"})
    assert s.entry.status is LegStatus.IDLE


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
