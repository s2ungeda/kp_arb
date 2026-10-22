"""오전 NXT 개장 매매 엔진(DESIGN-nxt-open.md §3) — 첫 체결 T0·창·한 번 발주·늦은 시동 안전장치."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from kp_arb.core_server import _nxt_open_command
from kp_arb.domain.enums import Instrument, Side, Underlying, Venue
from kp_arb.domain.models import InstrumentInfo, OrderIntent, Quote
from kp_arb.gateways.base import PlaceResult
from kp_arb.gateways.ls_ws import TradeTick
from kp_arb.nxt_open import NxtOpenScreen, Tier
from kp_arb.nxt_open_engine import NxtOpenEngine
from kp_arb.strategy_core import CoreState

U = Underlying.SK_HYNIX


class FakeSystem:
    def __init__(self) -> None:
        self.on_trade: list[Any] = []
        self.on_quote: list[Any] = []
        self.on_fx: list[Any] = []
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
        return [PlaceResult(f"O{i + 1}") for i, _ in enumerate(intents)]


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
    return eng, sys_, clk, screen


def _nxt(price: float) -> TradeTick:
    return TradeTick(underlying=U, instrument=Instrument.KR_STOCK, price=price, market="nxt")


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_first_trade_opens_window_and_fires_once() -> None:
    eng, sys_, clk, _ = _engine(_wall(8, 0, 1.2))
    eng._on_trade(_nxt(1_729_000.0))                 # 첫 체결 → T0
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
    eng._on_trade(_nxt(1_729_000.0))
    await _settle()
    assert len(sys_.placed) == 1 and len(sys_.placed[0]) == 3
    o = sys_.placed[0][0]
    assert (o.venue, o.instrument, o.side, o.qty, o.source, o.tag) == (
        Venue.HYPERLIQUID, Instrument.HL_PERP, Side.SELL, 100, "NXT개장", "진입1")
    # 주문가 = 1,729,000 × 1.009 / 1338.4 = 1303.47 → 매도는 바로 위 격자 1303.5
    assert o.price == 1303.5 and sys_.placed[0][2].price == 1298.4   # 0.5%: 1298.32 → 1298.4
    snap = eng.live_snapshot()[U.value]
    assert snap["fired"] and [r["order_id"] for r in snap["results"]] == ["O1", "O2", "O3"]
    # 딱 한 번 — 창 안에서 더 와도, 창이 닫혀도 다시 안 낸다
    clk.advance(1.0)
    eng._on_trade(_nxt(1_700_000.0))
    clk.advance(10.0)
    eng._on_fx()
    await _settle()
    assert len(sys_.placed) == 1 and eng.live_snapshot()[U.value]["window"] == "closed"


async def test_no_orders_when_disabled_or_outside_window() -> None:
    eng, sys_, clk, screen = _engine(_wall(8, 0, 0.5))
    screen.of(U).run_entry = screen.of(U).run_exit = False
    eng._on_trade(_nxt(1_729_000.0))
    clk.advance(1.0)
    eng._on_trade(_nxt(1_729_000.0))
    await _settle()
    assert not sys_.placed and not eng.live_snapshot()[U.value]["fired"]
    # 켜도 창이 끝났으면 안 낸다
    clk.advance(5.0)
    eng.set_enabled(U, "entry", True)
    eng._on_trade(_nxt(1_729_000.0))
    await _settle()
    assert not sys_.placed and eng.live_snapshot()[U.value]["window"] == "closed"


async def test_late_core_start_does_not_treat_first_tick_as_open() -> None:
    # 코어를 08:41에 켠 날 — 첫 NXT 수신을 개장으로 오인해 창을 열면 안 된다
    eng, sys_, clk, _ = _engine(_wall(8, 41, 0.0))
    eng._on_trade(_nxt(1_729_000.0))
    clk.advance(1.0)
    eng._on_trade(_nxt(1_729_000.0))
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
    eng._on_trade(_nxt(1_729_000.0))  # T0
    clk.advance(3.0)
    eng._on_trade(_nxt(1_729_000.0))
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
    eng._on_trade(_nxt(1_729_000.0))  # 08:00:06.5 — 개장 전, 무시
    assert eng.live_snapshot()[U.value]["t0"] is None and not eng._day(U).t0_rejected
    clk.advance(54.0)  # 08:01:00.5
    eng._on_trade(_nxt(1_729_000.0))
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
    eng3._on_trade(_nxt(1_729_000.0))  # T0
    clk3.advance(1.0)
    eng3._on_trade(_nxt(1_729_000.0))  # 창 안 — 진입 세 단 다 맞아 발주
    await _settle()
    assert sys3.placed and eng3.live_snapshot()[U.value]["fired"]
    assert not screen3.of(U).run_entry and not screen3.of(U).run_exit
    assert eng3.chance_over(U) == "오늘 발주 끝"
    # 발주한 뒤라도 개장 시각을 바꾸면 되돌린다(사용자 2026-10-22 결정 1) — 다시 켤 수 있음
    screen3.of(U).open_hms = "08:30:00"
    eng3.reset_day(U)
    assert eng3.chance_over(U) is None and not eng3.live_snapshot()[U.value]["fired"]


async def test_exit_tiers_buy_when_disparity_low() -> None:
    eng, sys_, clk, _ = _engine(_wall(8, 0, 2.0))
    # HL 매도1 1310.5 × 1338.4 = 1,753,973 vs NXT 1,770,000 → 청산괴리 −0.906% → −0.4·−0.8 두 단
    eng._on_trade(_nxt(1_770_000.0))
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
