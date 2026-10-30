"""오전 NXT 개장 매매 엔진(DESIGN-nxt-open.md §3) — 첫 체결 T0·창·한 번 발주·늦은 시동 안전장치."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from kp_arb.core_server import _nxt_open_command
from kp_arb.domain.enums import Instrument, Side, Underlying, Venue
from kp_arb.domain.models import InstrumentInfo, OrderIntent, Quote
from kp_arb.gateways.base import PlaceResult
from kp_arb.gateways.ls_ws import Fill
from kp_arb.nxt_open import NxtOpenScreen, Tier
from kp_arb.nxt_open_engine import DIAG_EVAL_LOG, NxtOpenEngine
from kp_arb.strategy_core import CoreState

U = Underlying.SK_HYNIX


class FakeSystem:
    def __init__(self) -> None:
        self.on_trade: list[Any] = []
        self.on_quote: list[Any] = []
        self.on_fx: list[Any] = []
        self.on_fill: list[Any] = []
        # 꼬리표 → 발주 응답 전에 이만큼 체결 통보(HL 즉시 체결 흉내)
        self.fill_on_place: dict[str, float] = {}
        self.quotes: dict[tuple[Underlying, Instrument, str], Quote] = {
            (U, Instrument.HL_PERP, "hl"): Quote(
                underlying=U, instrument=Instrument.HL_PERP, bid=1310.0, ask=1310.5, ts=0,
                market="hl", bids=[(1310.0, 50.0), (1309.9, 80.0)],
                asks=[(1310.5, 40.0), (1310.6, 90.0)])}
        self.trades: dict[tuple[Underlying, Instrument, str], float] = {}
        self.instruments = {(U, Instrument.HL_PERP): InstrumentInfo(
            underlying=U, instrument=Instrument.HL_PERP, code="xyz:SKHX", sz_decimals=1)}
        self.usdkrw_spot: float | None = 1338.4
        self.usdkrw_spot_src: str | None = "하나고시"
        self.placed: list[list[OrderIntent]] = []
        self._n = 0

    def new_hl_cloid(self) -> str | None:
        self._n += 1
        return f"c{self._n}"

    async def place_many(self, intents: Any, cloids: Any = None) -> list[PlaceResult]:
        self.placed.append(list(intents))
        base = sum(len(batch) for batch in self.placed[:-1])
        outs = [PlaceResult(f"O{base + i + 1}") for i, _ in enumerate(intents)]
        for it, out in zip(intents, outs, strict=True):  # HL 즉시 체결 — 통보가 응답보다 먼저
            q = self.fill_on_place.get(it.tag)
            if q:
                for h in self.on_fill:
                    h(Fill(fill_id=f"early-{out.order_id}", order_id=str(out.order_id), qty=q,
                           price=float(it.price or 0.0), ts=0))
        return outs


def _wall(h: int, m: int, s: float) -> float:
    lt = time.localtime()
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return base + h * 3600 + m * 60 + s


class Clock:
    def __init__(self, wall: float) -> None:
        self.mono = 1000.0
        self.wall = wall

    def advance(self, s: float) -> None:
        self.mono += s
        self.wall += s


def _engine(wall: float) -> tuple[NxtOpenEngine, FakeSystem, Clock, NxtOpenScreen]:
    clk = Clock(wall)
    sys_ = FakeSystem()
    screen = NxtOpenScreen()
    st = screen.of(U)
    # NXT 1,729,000 vs HL 매수1 1310 × 1338.4 = 1,753,304 → 진입괴리 +1.406% → 1.2·1.0·0.8 세 단 다
    st.entries = [Tier(0.012, 100, 0.009), Tier(0.010, 100, 0.007), Tier(0.008, 100, 0.005)]
    st.exits = [Tier(-0.004, 100, -0.002), Tier(-0.008, 100, -0.006), Tier(-0.010, 100, -0.008)]
    st.run_entry = st.run_exit = True
    eng = NxtOpenEngine(screen, sys_, clock=lambda: clk.mono, wall=lambda: clk.wall)
    # §4b 기준가(2026-10-29): 없으면 전부 금지라 기존 시나리오엔 호가 중간값 1310.25를 잡아 둔다
    d = eng._day(U)
    d.hl_base, d.hl_base_wall = 1310.25, clk.wall
    return eng, sys_, clk, screen


def _nxt(price: float) -> Quote:
    """NXT 1호가(UH1의 NXT 쪽) — 매수1 = 매도1 = price, 잔량 1:1 → S = price(2026-10-28: 체결
    대신)."""
    return Quote(underlying=U, instrument=Instrument.KR_STOCK, bid=price, ask=price, ts=0.0,
                 bid_qty=1.0, ask_qty=1.0, market="nxt")


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_first_trade_opens_window_and_fires_once() -> None:
    eng, sys_, clk, _ = _engine(_wall(8, 0, 1.2))
    eng._on_quote(_nxt(1_729_000.0))                 # 첫 체결 → T0
    await _settle()
    snap = eng.live_snapshot()[U.value]
    assert snap["t0"] is not None and snap["window"] == "before" and not sys_.placed
    assert abs(snap["entry_disp"] - 0.014056) < 1e-5 and snap["hl_bid"] == 1310.0
    assert snap["hl_merge_ticks"] and snap["hl_merge_ticks"][0]["tick"]  # 호가단위 콤보 재료
    # 기준수량 100이면 매수호가창 1310×50 + 1309.9×50 → est 1309.95(체결쏴 모니터와 같은 계산)
    eng.settings(U).ref_qty = 100
    assert abs(eng.live_snapshot()[U.value]["hl_bid"] - 1309.95) < 1e-9
    eng.settings(U).ref_qty = 1
    clk.advance(0.3)                                 # +0.3초 — 아직 창 전
    eng._on_quote(sys_.quotes[(U, Instrument.HL_PERP, "hl")])
    await _settle()
    assert not sys_.placed
    clk.advance(0.7)                                 # +1.0초 — 창 안 → 세 단 한꺼번에 HL 매도
    eng._on_quote(_nxt(1_729_000.0))
    await _settle()
    assert len(sys_.placed) == 1 and len(sys_.placed[0]) == 3
    o = sys_.placed[0][0]
    assert (o.venue, o.instrument, o.side, o.qty, o.source, o.tag) == (
        Venue.HYPERLIQUID, Instrument.HL_PERP, Side.SELL, 100, "NXT개장", "진입1")
    # 주문가 = 1,729,000 × 1.009 / 1338.4 = 1303.47 → 매도는 바로 위 격자 1303.5
    assert o.price == 1303.5 and sys_.placed[0][2].price == 1298.4   # 0.5%: 1298.32 → 1298.4
    snap = eng.live_snapshot()[U.value]
    assert snap["fired"] and [r["order_id"] for r in snap["results"]] == ["O1", "O2", "O3"]
    # 나간 단(진입 1~3)은 다시 안 낸다 — 창 안에서 더 와도, 창이 닫혀도. 청산 단은 남아 있어 실행은
    # 아직 켜져 있다(사용자 2026-10-23 "주문 나간 세트만 멈추고 설정한 시간까지는 계속")
    clk.advance(1.0)
    eng._on_quote(_nxt(1_700_000.0))
    await _settle()
    assert len(sys_.placed) == 1 and eng.settings(U).run_entry and eng.settings(U).run_exit
    assert eng.live_snapshot()[U.value]["fired_tiers"] == [["entry", 0], ["entry", 1],
                                                            ["entry", 2]]
    clk.advance(10.0)
    eng._on_fx()
    await _settle()
    assert len(sys_.placed) == 1 and eng.live_snapshot()[U.value]["window"] == "closed"
    assert not eng.settings(U).run_entry  # 창이 끝나야 끈다


async def test_no_orders_when_disabled_or_outside_window() -> None:
    eng, sys_, clk, screen = _engine(_wall(8, 0, 0.5))
    screen.of(U).run_entry = screen.of(U).run_exit = False
    eng._on_quote(_nxt(1_729_000.0))
    clk.advance(1.0)
    eng._on_quote(_nxt(1_729_000.0))
    await _settle()
    assert not sys_.placed and not eng.live_snapshot()[U.value]["fired"]
    # 켜도 창이 끝났으면 안 낸다
    clk.advance(5.0)
    eng.set_enabled(U, "entry", True)
    eng._on_quote(_nxt(1_729_000.0))
    await _settle()
    assert not sys_.placed and eng.live_snapshot()[U.value]["window"] == "closed"


async def test_late_core_start_does_not_treat_first_tick_as_open() -> None:
    # 코어를 08:41에 켠 날 — 첫 NXT 수신을 개장으로 오인해 창을 열면 안 된다
    eng, sys_, clk, _ = _engine(_wall(8, 41, 0.0))
    eng._on_quote(_nxt(1_729_000.0))
    clk.advance(1.0)
    eng._on_quote(_nxt(1_729_000.0))
    await _settle()
    snap = eng.live_snapshot()[U.value]
    assert snap["t0"] is None and snap["t0_rejected"] and snap["window"] == "none"
    assert not sys_.placed
    # 미리 켜 둔 실행은 오늘 기회가 없으니 자동으로 꺼진다(사용자 2026-10-21)
    assert snap["note"] == "오늘 판정 없음(개장 밖) · 자동 끔"
    assert not snap["run_entry"] and not snap["run_exit"]


async def test_run_turns_off_by_itself_when_today_is_over() -> None:
    # 사용자 2026-10-21 "시간이 지나면 자동으로 자동주문 꺼졌으면" — 창이 끝났는데 조건이
    # 안 맞았으면 끔
    eng, sys_, clk, screen = _engine(_wall(8, 0, 0.5))
    st = screen.of(U)
    st.entries = [Tier(0.05, 100, 0.009)]  # 괴리 1.4%로는 안 맞는 단
    st.exits = []
    eng._on_quote(_nxt(1_729_000.0))  # T0
    clk.advance(3.0)
    eng._on_quote(_nxt(1_729_000.0))
    assert st.run_entry  # 창 안 — 아직 켜져 있음
    clk.advance(3.0)  # 창 끝(+6초)
    snap = eng.live_snapshot()[U.value]
    assert snap["window"] == "closed" and not sys_.placed
    assert not st.run_entry and not st.run_exit and snap["note"] == "창 끝 미충족 · 자동 끔"
    # 기회가 지난 뒤엔 켜기 거부 사유가 나온다(코어가 이걸로 거부) — 켜 둔 채 남지 않게
    assert eng.chance_over(U) == "오늘 창 끝"
    # 개장 시각을 바꾸면 오늘 상태를 새로 시작 — 다시 켤 수 있고 개장 전 체결은 무시, 개장 뒤 첫
    # 체결이 T0(사용자 2026-10-21 시험: 14:18 개장 밖 판정이 남아 14:45로 바꿔도 안 열리던 것)
    st.open_hms = "08:01:00"
    eng.reset_day(U)
    assert eng.chance_over(U) is None and eng.live_snapshot()[U.value]["window"] == "none"
    st.run_entry = True
    eng._on_quote(_nxt(1_729_000.0))  # 08:00:06.5 — 개장 전, 무시
    assert eng.live_snapshot()[U.value]["t0"] is None and not eng._day(U).t0_rejected
    clk.advance(54.0)  # 08:01:00.5
    eng._on_quote(_nxt(1_729_000.0))
    assert eng.live_snapshot()[U.value]["t0"] == "08:01:00.500"

    # 첫 체결이 아예 없는 날 — 개장 + 유예 10초 + 창 끝 5초가 지나면 끔
    eng2, _, clk2, screen2 = _engine(_wall(7, 59, 0.0))
    clk2.advance(60.0 + 15.0)
    assert screen2.of(U).run_entry and eng2.live_snapshot()[U.value]["window"] == "none"
    clk2.advance(0.2)
    snap2 = eng2.live_snapshot()[U.value]
    assert not screen2.of(U).run_entry and snap2["note"] == "첫 체결 없음 · 자동 끔"

    # 발주한 날은 발주 뒤 끔(상태줄 비고는 발주 결과가 보이므로 그대로)
    eng3, sys3, clk3, screen3 = _engine(_wall(8, 0, 0.5))
    eng3._on_quote(_nxt(1_729_000.0))  # T0
    clk3.advance(1.0)
    eng3._on_quote(_nxt(1_729_000.0))  # 창 안 — 진입 세 단 다 맞아 발주
    await _settle()
    assert sys3.placed and eng3.live_snapshot()[U.value]["fired"]
    # 진입 세 단이 나갔어도 청산 단이 남아 있으면 창이 끝날 때까지 켜져 있다(2026-10-23)
    assert screen3.of(U).run_entry and screen3.of(U).run_exit and eng3.chance_over(U) is None
    clk3.advance(10.0)
    eng3.live_snapshot()
    assert not screen3.of(U).run_entry and not screen3.of(U).run_exit
    assert eng3.chance_over(U) == "오늘 창 끝"
    # 발주한 뒤라도 개장 시각을 바꾸면 되돌린다(사용자 2026-10-22 결정 1) — 다시 켤 수 있음
    screen3.of(U).open_hms = "08:30:00"
    eng3.reset_day(U)
    assert eng3.chance_over(U) is None and not eng3.live_snapshot()[U.value]["fired"]


async def test_exit_tiers_buy_when_disparity_low() -> None:
    eng, sys_, clk, _ = _engine(_wall(8, 0, 2.0))
    # HL 매도1 1310.5 × 1338.4 = 1,753,973 vs NXT 1,770,000 → 청산괴리 −0.906% → −0.4·−0.8 두 단
    eng._on_quote(_nxt(1_770_000.0))
    clk.advance(1.0)
    eng._on_quote(sys_.quotes[(U, Instrument.HL_PERP, "hl")])
    await _settle()
    assert len(sys_.placed) == 1
    sides = [(o.side, o.tag) for o in sys_.placed[0]]
    assert sides == [(Side.BUY, "청산1"), (Side.BUY, "청산2")]


def test_core_command_saves_settings_and_guards_run() -> None:
    state = CoreState()
    body = {"cmd": "nxt_open_settings", "underlying": "sk_hynix",
            "start_s": 0.5, "end_s": 5.0,
            "entries": [{"spread": 0.012, "qty": 100, "price_spread": 0.009}]}
    assert _nxt_open_command(None, state, body)["ok"]
    st = state.nxt_open.of(U)
    assert st.entries[0] == Tier(0.012, 100, 0.009)
    assert not _nxt_open_command(None, state, {**body, "start_s": 9.0, "end_s": 1.0})["ok"]
    # 실행: 엔진 없으면 거부, 단이 비어 있으면 거부
    run = {"cmd": "nxt_open_run", "underlying": "samsung", "block": "entry", "value": True}
    assert "미접속" in _nxt_open_command(None, state, run)["errors"][0]
    clk = Clock(_wall(7, 50, 0.0))  # 개장(08:00) 전 — 기회가 남아 있음
    eng = NxtOpenEngine(state.nxt_open, FakeSystem(), clock=lambda: clk.mono,
                        wall=lambda: clk.wall)
    assert _nxt_open_command(eng, state, run)["errors"] == ["진입 단 없음"]
    assert "block은" in _nxt_open_command(eng, state, {**run, "block": "x"})["errors"][0]
    assert _nxt_open_command(eng, state, {**run, "underlying": "sk_hynix"})["ok"]
    assert st.run_entry is True and st.run_exit is False
    # 반만 채운 1단 + 완전한 2단 → 켜진다(사용자 2026-10-21: 반만 채운 단은 판정에서 빠질 뿐)
    st.entries[0], st.entries[1] = Tier(0.002, 0, -0.005), Tier(0.001, 1, -0.004)
    assert _nxt_open_command(eng, state, {**run, "underlying": "sk_hynix"})["ok"]
    assert _nxt_open_command(eng, state, {**run, "underlying": "sk_hynix", "value": False})["ok"]
    assert st.run_entry is False
    # 기회가 지난 뒤(개장+10초+창 끝 지남) 켜면 거부 — 사용자 2026-10-21 "시간이 지났는데 안 꺼짐"
    st.entries[0] = Tier(0.002, 1, -0.005)
    clk.advance(10 * 60 + 20.0)  # 08:00:20
    assert _nxt_open_command(eng, state, {**run, "underlying": "sk_hynix"})["errors"] == [
        "개장 08:00:00 지남 — 개장 시각을 고치거나 내일 켜세요"]
    # 개장 시각을 앞으로 옮겨 저장하면 오늘 상태를 새로 시작 → 다시 켤 수 있다
    assert _nxt_open_command(eng, state, {**body, "open_hms": "08:05:00"})["ok"]
    assert _nxt_open_command(eng, state, {**run, "underlying": "sk_hynix"})["ok"]
    assert st.run_entry is True
    assert _nxt_open_command(None, state, {"cmd": "nxt_open_x"})["errors"] == ["underlying 필요"]


async def test_each_tier_fires_once_and_rest_keep_running_until_window_end() -> None:
    # 사용자 2026-10-23: 나간 단만 멈추고 나머지는 창 끝까지 — 10-23 08:00 청산1만 나가고 둘 다
    # 꺼진 것 교정. NXT 1,729,000 / HL 매도1 1310.5: 청산괴리 +1.4% → 청산 단은 안 맞음.
    # S를 올려 청산괴리를 −0.5%, −0.9%로 만든다(환 1338.4 × 1310.5 = 1,753,173).
    eng, sys_, clk, screen = _engine(_wall(8, 0, 0.5))
    st = screen.of(U)
    st.entries = [Tier(0.05, 1, 0.009)]  # 안 맞는 진입 단 하나
    st.exits = [Tier(-0.004, 1, -0.002), Tier(-0.008, 1, -0.006), Tier(-0.010, 1, -0.008)]
    eng._on_quote(_nxt(1_762_000.0))  # T0 (청산괴리 −0.50%)
    clk.advance(1.0)
    eng._on_quote(_nxt(1_762_000.0))  # 창 안 — 청산1(−0.4%)만 충족
    await _settle()
    assert len(sys_.placed) == 1 and [o.tag for o in sys_.placed[0]] == ["청산1"]
    assert st.run_entry and st.run_exit  # 나머지 단이 남아 계속 실행
    clk.advance(1.0)
    eng._on_quote(_nxt(1_769_000.0))  # 청산괴리 −0.90% → 청산2 추가, 청산1은 다시 안 냄
    await _settle()
    assert len(sys_.placed) == 2 and [o.tag for o in sys_.placed[1]] == ["청산2"]
    snap = eng.live_snapshot()[U.value]
    assert [r["order_id"] for r in snap["results"]] == ["O1", "O2"] and snap["fired"]
    assert snap["fired_tiers"] == [["exit", 0], ["exit", 1]]
    clk.advance(0.5)
    eng._on_quote(_nxt(1_769_000.0))  # 같은 괴리 — 이미 나간 단은 다시 안 냄
    await _settle()
    assert len(sys_.placed) == 2 and st.run_exit
    clk.advance(5.0)                   # 창 끝 → 남은 단(진입1·청산3)이 있어도 끔
    eng.live_snapshot()
    assert not st.run_entry and not st.run_exit and eng.chance_over(U) == "오늘 창 끝"



async def test_auto_exit_places_opposite_order_when_strategy_order_fully_filled() -> None:
    # §4a(사용자 2026-10-29 확인 1~3): 체크한 단은 발주 시점 S·환으로 청산가 스프레드를 풀어 두고,
    # 그 주문이 전량 체결되면 곧바로 반대 방향 GTC 지정가. 부분 체결엔 안 냄, 같은 체결번호는 한 번.
    eng, sys_, clk, screen = _engine(_wall(8, 0, 1.2))
    st = screen.of(U)
    st.entries[0] = Tier(0.012, 100, 0.009, 0.002, True)   # 진입1 자동청산(청산가 +0.2%)
    st.entries[1] = Tier(0.010, 100, 0.007, 0.001, False)  # 체크 안 함
    eng._on_quote(_nxt(1_729_000.0))
    clk.advance(1.0)
    eng._on_quote(_nxt(1_729_000.0))
    await _settle()
    assert len(sys_.placed) == 1 and [o.tag for o in sys_.placed[0]] == ["진입1", "진입2", "진입3"]
    snap = eng.live_snapshot()[U.value]
    # 1,729,000 × 1.002 / 1338.4 = 1294.43 → 매수는 내림 1294.4
    assert snap["results"][0]["auto_price"] == 1294.4
    assert snap["results"][1]["auto_price"] is None
    assert [p["order_id"] for p in snap["auto_pending"]] == ["O1"]
    sys_.on_fill[0](Fill(fill_id="f1", order_id="O1", qty=40.0, price=1303.5, ts=0))
    sys_.on_fill[0](Fill(fill_id="f1", order_id="O1", qty=40.0, price=1303.5, ts=0))  # 중복 통보
    sys_.on_fill[0](Fill(fill_id="f2", order_id="O2", qty=100.0, price=1301.5, ts=0))  # 체크 없음
    await _settle()
    assert len(sys_.placed) == 1
    assert eng.live_snapshot()[U.value]["auto_pending"][0]["filled"] == 40.0
    sys_.on_fill[0](Fill(fill_id="f3", order_id="O1", qty=60.0, price=1303.5, ts=0))
    await _settle()
    assert len(sys_.placed) == 2
    o = sys_.placed[1][0]
    assert (o.side, o.qty, o.price, o.tag, o.source) == (
        Side.BUY, 100, 1294.4, "진입1청산", "NXT개장")
    snap = eng.live_snapshot()[U.value]
    assert snap["auto_pending"] == []
    last = snap["results"][-1]
    assert last["auto"] and last["order_id"] == "O4" and last["side"] == "buy"
    # 늦은 중복 통보는 아무 일도 안 한다
    sys_.on_fill[0](Fill(fill_id="f4", order_id="O1", qty=1.0, price=1303.5, ts=0))
    await _settle()
    assert len(sys_.placed) == 2


async def test_auto_exit_handles_fill_notified_before_place_response() -> None:
    # §4a-3 구현: HL 테이커 즉시 체결은 통보가 발주 응답보다 먼저 — 등록 전 체결을 보관했다 합친다
    eng, sys_, clk, screen = _engine(_wall(8, 0, 1.2))
    st = screen.of(U)
    st.entries = [Tier(0.012, 100, 0.009, 0.002, True)]
    sys_.fill_on_place["진입1"] = 100.0
    eng._on_quote(_nxt(1_729_000.0))
    clk.advance(1.0)
    eng._on_quote(_nxt(1_729_000.0))
    await _settle()
    assert [[o.tag for o in b] for b in sys_.placed] == [["진입1"], ["진입1청산"]]
    assert eng.live_snapshot()[U.value]["auto_pending"] == []


async def test_range_block_skips_tier_but_keeps_trying_until_window_end() -> None:
    # §4b(사용자 2026-10-29 확인 6~8): 기준가 없으면 금지, HL 현재가(마지막 체결가) ≤ 매도 한도면
    # 매도 금지 — 그 단은 발주된 단으로 치지 않고 창 끝까지 계속 시도, 사유는 단마다 한 번
    eng, sys_, clk, screen = _engine(_wall(8, 0, 1.2))
    d = eng._day(U)
    d.hl_base = None
    screen.of(U).hl_base_hms = "07:30:00"  # 기준시간 + 10초가 지나 이 날은 기준가를 못 잡는다
    screen.of(U).entries = [Tier(0.012, 100, 0.009)]  # 진입 한 단만(상태줄 사유는 마지막 단 것)
    eng._on_quote(_nxt(1_729_000.0))
    clk.advance(1.0)
    eng._on_quote(_nxt(1_729_000.0))
    await _settle()
    snap = eng.live_snapshot()[U.value]
    assert not sys_.placed and snap["fired_tiers"] == [] and snap["note"] == "진입1 HL 기준가 없음"
    assert snap["hl_base"] is None and snap["sell_limit"] is None
    # 기준가 1,360 → 매도 한도 1,319.2; HL 마지막 체결가 1,310 ≤ 한도 → 매도 금지
    d.hl_base, d.hl_base_wall = 1360.0, clk.wall
    sys_.trades[(U, Instrument.HL_PERP, "hl")] = 1310.0
    clk.advance(0.5)
    eng._on_quote(_nxt(1_729_000.0))
    await _settle()
    snap = eng.live_snapshot()[U.value]
    assert not sys_.placed and snap["fired_tiers"] == []
    assert snap["note"] == "진입1 매도 금지 — HL 1,310.0 ≤ 한도 1,319.2(기준 1,360.0 −3%)"
    assert (snap["sell_limit"], snap["buy_limit"], snap["hl_last"]) == (1319.2, 1400.8, 1310.0)
    # HL이 범위 안으로 돌아오면(1,320 > 1,319.2) 그제야 낸다 — 창 안이면 계속 시도
    sys_.trades[(U, Instrument.HL_PERP, "hl")] = 1320.0
    clk.advance(0.5)
    eng._on_quote(_nxt(1_729_000.0))
    await _settle()
    assert len(sys_.placed) == 1 and [o.tag for o in sys_.placed[0]] == ["진입1"]
    # 자동청산도 금지 검사(§4a-4): 청산1(HL 매수)의 자동청산은 매도 — HL 1,300 ≤ 매도 한도 1,319.2면
    # 안 내고 실패 사유만
    st = screen.of(U)
    st.exits = [Tier(-0.004, 10, -0.002, -0.001, True)]
    d.fired_tiers.clear()
    sys_.trades[(U, Instrument.HL_PERP, "hl")] = 1320.0
    eng._on_quote(_nxt(1_770_000.0))   # 청산괴리 −0.9% → 청산1(HL 매수) 발주
    await _settle()
    assert [o.tag for o in sys_.placed[-1]] == ["청산1"]
    oid = eng.live_snapshot()[U.value]["auto_pending"][0]["order_id"]
    sys_.trades[(U, Instrument.HL_PERP, "hl")] = 1300.0
    sys_.on_fill[0](Fill(fill_id="x", order_id=oid, qty=10.0, price=1300.0, ts=0))
    await _settle()
    last = eng.live_snapshot()[U.value]["results"][-1]
    assert last["auto"] and last["order_id"] is None and last["side"] == "sell"
    assert last["error"] == "매도 금지 — HL 1,300.0 ≤ 한도 1,319.2(기준 1,360.0 −3%)"
    assert [o.tag for o in sys_.placed[-1]] == ["청산1"]  # 자동청산 주문은 안 나감


async def test_hl_base_captured_at_base_time_only_within_grace() -> None:
    # §4b-1: HL기준시간(07:59:55)을 지난 첫 시세의 HL 현재가(마지막 체결가, 없으면 중간값)를
    # 기준가로, 10초 안에만. 늦게 켠 코어는 기준가 없음.
    eng, sys_, clk, _ = _engine(_wall(7, 59, 54.0))
    d = eng._day(U)
    d.hl_base = None
    eng._on_quote(sys_.quotes[(U, Instrument.HL_PERP, "hl")])
    assert eng.live_snapshot()[U.value]["hl_base"] is None     # 아직 기준시간 전
    clk.advance(2.0)                                            # 07:59:56
    eng._on_quote(sys_.quotes[(U, Instrument.HL_PERP, "hl")])
    snap = eng.live_snapshot()[U.value]
    assert snap["hl_base"] == 1310.25 and snap["hl_base_at"] == "07:59:56.000"  # 체결 없음 → 중간값
    assert (snap["sell_limit"], snap["buy_limit"]) == (1310.25 * 0.97, 1310.25 * 1.03)
    sys_.trades[(U, Instrument.HL_PERP, "hl")] = 1300.0
    clk.advance(1.0)
    eng._on_quote(sys_.quotes[(U, Instrument.HL_PERP, "hl")])
    assert eng.live_snapshot()[U.value]["hl_base"] == 1310.25  # 하루 한 번 — 안 바뀜
    eng2, sys2, _clk2, _ = _engine(_wall(8, 0, 6.0))             # 기준시간 + 11초에 켬
    eng2._day(U).hl_base = None
    sys2.trades[(U, Instrument.HL_PERP, "hl")] = 1300.0
    eng2._on_quote(sys2.quotes[(U, Instrument.HL_PERP, "hl")])
    assert eng2.live_snapshot()[U.value]["hl_base"] is None


async def test_diag_eval_log_once_per_change_and_decision_time(caplog: Any) -> None:
    # §6 임시 진단 로그(2026-10-30): 창 안 판정마다 한 줄 — 값·결과가 같으면 안 남김,
    # 발주 로그엔 판정 시각
    assert DIAG_EVAL_LOG  # 원인 확인 뒤 끄면 이 테스트도 같이 지운다
    caplog.set_level(logging.INFO, logger="kp_arb.nxt_open")
    eng, sys_, clk, _ = _engine(_wall(8, 0, 1.2))
    eng.settings(U).entries = [Tier(0.020, 100, 0.009)]  # 괴리 +1.406% < 2% → 미달
    q = _nxt(1_729_000.0)
    q = q.model_copy(update={"ts": 80001.0})
    eng._on_quote(q)                                  # T0 — 창 전이라 판정 로그 없음
    clk.advance(1.0)
    eng._on_quote(q)
    eng._on_quote(q)                                  # 같은 값 → 두 번째는 안 남김
    eng._on_quote(sys_.quotes[(U, Instrument.HL_PERP, "hl")])
    await _settle()
    lines = [r.getMessage() for r in caplog.records if "판정 " in r.getMessage()]
    assert len(lines) == 1 and "판정 08:00:02.200(NXT)" in lines[0]
    assert "NXT호가시각 08:00:01 S 1,729,000 HL매수est 1310.00" in lines[0]
    # 매도1 1310.5 × 1338.4 / 1,729,000 − 1 = +1.444%
    assert lines[0].endswith("HL매도est 1310.50 환 1338.40 진입괴리 +1.406% 청산괴리 +1.444% "
                             "→ 미달")
    # 스프레드를 낮추면 결과가 바뀌어 다시 남고, 발주 로그엔 판정 시각과 발주 시각이 같이
    eng.settings(U).entries = [Tier(0.010, 100, 0.009)]
    clk.advance(0.5)
    eng._on_quote(q)
    await _settle()
    lines = [r.getMessage() for r in caplog.records if "판정 " in r.getMessage()]
    assert len(lines) == 3 and lines[1].endswith("→ 발주 진입1")
    assert "판정 08:00:02.700 발주 08:00:02.700 —" in lines[2] and len(sys_.placed) == 1
