"""오전 NXT 개장 매매 실행 엔진 — DESIGN-nxt-open.md §3·§6.

CoreState.nxt_open(종목별 설정)과 LiveSystem을 묶는다. 이벤트 주도(별도 루프 없음):
- NXT 주식 체결(on_trade, market "nxt") → 그날 첫 체결이면 T0(개장 시각 + 유예 안일 때만) + S 갱신
- HL 호가(on_quote, 선택한 호가단위 적용 1호가)·환율(on_fx) → 괴리 재계산
- 창 안(T0 + start ~ + end)이고 실행이 켜져 있으면 판정 → 맞는 단을 한꺼번에 HL 묶음 발주, 딱 한 번
발주 뒤 미체결·포지션은 건드리지 않는다(정리는 사용자). 날짜가 바뀌면 그날 상태를 비운다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .disparity import est_price
from .domain.enums import Instrument, OrderType, Side, Underlying, Venue
from .domain.models import OrderIntent, Quote
from .hl_merge import merge_tick_options
from .nxt_open import (
    SOURCE,
    NxtOpenScreen,
    NxtOpenSettings,
    Plan,
    Tier,
    day_over,
    disparity,
    judge,
    level_price,
    order_price,
    remaining_tiers,
    t0_verdict,
    window_state,
)


class _System(Protocol):
    """엔진이 쓰는 LiveSystem 조각(테스트용 가짜도 같은 모양)."""

    quotes: dict[tuple[Underlying, Instrument, str], Quote]
    trades: dict[tuple[Underlying, Instrument, str], float]
    instruments: dict[tuple[Underlying, Instrument], Any]
    usdkrw_spot: float | None
    usdkrw_spot_src: str | None

    async def place_many(self, intents: Any, cloids: Any = None) -> Any: ...
    def new_hl_cloid(self) -> str | None: ...


@dataclass
class TierResult:
    """낸 주문 한 건의 결과(화면 표시)."""

    block: str
    index: int
    side: str
    qty: float
    price: float
    order_id: str | None = None
    error: str | None = None


@dataclass
class DayState:
    """종목 하나의 그날 상태 — 저장하지 않는다."""

    day: str = ""
    t0: float | None = None          # monotonic
    t0_wall: float | None = None     # epoch(표시용)
    t0_rejected: bool = False        # 첫 체결이 개장 시각 밖 → 오늘 창 없음
    fired: bool = False              # 오늘 한 단이라도 발주함(표시용)
    fired_wall: float | None = None  # 마지막 발주 시각
    fired_tiers: set[tuple[str, int]] = field(default_factory=set)  # 발주된 단 — 단마다 하루 한 번
    results: list[TierResult] = field(default_factory=list)
    s: float | None = None
    hl_bid_n: float | None = None
    hl_ask_n: float | None = None
    entry_disp: float | None = None
    exit_disp: float | None = None
    note: str = ""                    # 마지막 판정 설명(미충족 사유 등)
    over_seen: bool = False           # 오늘 기회 끝남을 한 번 처리함(실행 자동 끔은 하루 한 번)


class NxtOpenEngine:
    """오전 NXT 개장 매매 엔진 — 코어가 하나 만든다."""

    def __init__(self, screen: NxtOpenScreen, system: _System, *,
                 save: Any = None, clock: Any = None, wall: Any = None) -> None:
        self._screen = screen
        self._system = system
        self._save = save
        self._clock = clock or time.monotonic
        self._wall = wall or time.time
        self._log = logging.getLogger("kp_arb.nxt_open")
        self._days: dict[Underlying, DayState] = {u: DayState() for u in Underlying}
        self._tasks: set[asyncio.Task[None]] = set()
        for name, handler in (("on_trade", self._on_trade), ("on_quote", self._on_quote),
                              ("on_fx", self._on_fx)):
            hooks = getattr(system, name, None)
            if hooks is not None:
                hooks.append(handler)

    # --- 설정·명령 ---

    def settings(self, u: Underlying) -> NxtOpenSettings:
        return self._screen.of(u)

    def sz_decimals(self, u: Underlying) -> int | None:
        """그 종목 HL 수량 소수 자리(종목 메타) — 설정 저장 때 단 수량 검사용. 메타 없으면 None."""
        info = self._system.instruments.get((u, Instrument.HL_PERP))
        dec = getattr(info, "sz_decimals", None) if info is not None else None
        return int(dec) if isinstance(dec, int) else None

    def set_enabled(self, u: Underlying, block: str, value: bool) -> None:
        """실행 켜기/끄기 — block "entry"(HL매도) | "exit"(HL매수), 화면 표의 병합 버튼."""
        st = self.settings(u)
        if block == "entry":
            st.run_entry = value
        else:
            st.run_exit = value
        self._log.info("[NXT개장] %s %s 실행 %s", u.value, "진입" if block == "entry" else "청산",
                       "켬" if value else "끔")

    def chance_over(self, u: Underlying) -> str | None:
        """오늘 기회가 끝났으면 그 사유(실행 켜기 거부 문구용), 아니면 None — 사용자 2026-10-21
        "시간이 지났는데 안 꺼짐": 지난 뒤에 켜는 것도 막아야 켜 둔 채 남지 않는다."""
        d, st = self._day(u), self.settings(u)
        if not self._is_over(d, st):
            return None
        if remaining_tiers(st, d.fired_tiers) == 0 and d.fired:
            return "오늘 발주 끝(전 단)"
        if d.t0_rejected:
            return "오늘 판정 없음(개장 밖)"
        if d.t0 is not None:
            return "오늘 창 끝"
        return f"개장 {st.open_hms} 지남"

    def reset_day(self, u: Underlying) -> None:
        """개장 시각이 바뀌면 그 종목의 오늘 상태(첫 체결·개장 밖 판정·발주 끝·자동 끔 처리)를 새로
        시작(사용자 2026-10-21 시험: 14:18 개장 밖 판정이 남아 14:45로 바꿔도 창이 안 열리고 자동
        끔도 안 걸림). 발주한 날도 되돌린다(사용자 2026-10-22 결정 1) — 개장 시각 변경은 사람이
        일부러 하는 일이고 켜기도 다시 눌러야 하므로 "하루 한 번"은 '한 개장 시각에 한 번'으로."""
        d = self._day(u)
        if d.fired:
            self._log.warning("[NXT개장] %s 개장 시각 변경 — 오늘 이미 발주(%s)했지만 상태를 "
                              "되돌려 다시 켤 수 있게 함", u.value, _hms(d.fired_wall))
        else:
            self._log.info("[NXT개장] %s 개장 시각 변경 → 오늘 상태 초기화", u.value)
        self._days[u] = DayState(day=d.day)

    def _is_over(self, d: DayState, st: NxtOpenSettings) -> bool:
        window = window_state(self._clock(), d.t0, st.start_s, st.end_s)
        all_fired = d.fired and remaining_tiers(st, d.fired_tiers) == 0
        return day_over(self._wall(), st.open_hms, window, d.t0_rejected, all_fired, st.end_s)

    def _auto_off(self, u: Underlying, d: DayState, st: NxtOpenSettings) -> None:
        """오늘 기회가 끝나면 진입·청산 실행을 자동으로 끈다(사용자 2026-10-21) — 발주 뒤·창 끝
        미충족·개장 밖·첫 체결 없이 시각 지남. 하루 한 번(over_seen). 기회가 끝난 뒤에 켜는 것은
        코어가 chance_over로 거부하고, 개장 시각을 바꾸면 reset_day로 다시 시작한다."""
        if d.over_seen or not self._is_over(d, st):
            return
        d.over_seen = True
        if not (st.run_entry or st.run_exit):
            return
        st.run_entry = st.run_exit = False
        if d.fired and remaining_tiers(st, d.fired_tiers) == 0:
            why = "전 단 발주 끝"
        elif d.t0_rejected:
            why = "오늘 판정 없음(개장 밖)"
        elif d.t0 is not None:
            why = "창 끝" if d.fired else "창 끝 미충족"
        else:
            why = "첫 체결 없음"
        if not d.fired:  # 발주한 날은 상태줄이 발주 결과를 보여 주므로 비고를 안 바꾼다
            d.note = f"{why} · 자동 끔"
        self._log.info("[NXT개장] %s 오늘 기회 끝(%s) — 진입·청산 실행 자동 끔", u.value, why)

    # --- 이벤트 ---

    def _day(self, u: Underlying) -> DayState:
        today = time.strftime("%Y-%m-%d", time.localtime(self._wall()))
        d = self._days[u]
        if d.day != today:
            d = self._days[u] = DayState(day=today)
        return d

    def _on_trade(self, tick: Any) -> None:
        if getattr(tick, "instrument", None) is not Instrument.KR_STOCK:
            return
        if getattr(tick, "market", "") != "nxt":
            return
        u: Underlying = tick.underlying
        d = self._day(u)
        d.s = float(tick.price)
        if d.t0 is None and not d.t0_rejected:
            now_wall = self._wall()
            st = self.settings(u)
            verdict = t0_verdict(now_wall, st.open_hms)
            if verdict == "early":
                pass  # 개장 전 체결 — T0 후보 아님(무시), 개장 뒤 첫 체결을 기다린다
            elif verdict == "ok":
                d.t0, d.t0_wall = self._clock(), now_wall
                self._log.info("[NXT개장] %s 첫 체결 %s @ %s — 창 +%.1f~+%.1f초", u.value,
                               _hms(now_wall), f"{d.s:,.0f}", st.start_s, st.end_s)
            else:
                d.t0_rejected = True
                d.note = "오늘 판정 없음(개장 밖)"
                self._log.warning("[NXT개장] %s 첫 NXT 체결이 %s(개장 %s 기준 유예 밖) — 오늘 창 "
                                  "없음", u.value, _hms(now_wall), st.open_hms)
        self._evaluate(u)

    def _on_quote(self, quote: Quote) -> None:
        if quote.instrument is Instrument.HL_PERP:
            self._evaluate(quote.underlying)

    def _on_fx(self) -> None:
        for u in Underlying:
            self._evaluate(u)

    # --- 계산·판정 ---

    def _compute(self, u: Underlying, d: DayState) -> None:
        st = self.settings(u)
        hl = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        bids = (hl.bids if hl.bids else [(hl.bid, hl.bid_qty or 1.0)]) if hl else None
        asks = (hl.asks if hl.asks else [(hl.ask, hl.ask_qty or 1.0)]) if hl else None
        # 선택한 호가단위(틱 머지, 코어 공용 hl_merge)를 적용한 호가창을 기준수량만큼 쓸어담은
        # est 가격(체결쏴 모니터와 같음) — 진입은 매수호가창, 청산은 매도호가창.
        # 기준수량 0이면 1호가
        if st.ref_qty > 0:
            d.hl_bid_n = est_price(bids or [], st.ref_qty) if bids else None
            d.hl_ask_n = est_price(asks or [], st.ref_qty) if asks else None
        else:
            d.hl_bid_n = level_price(bids, 1)
            d.hl_ask_n = level_price(asks, 1)
        if d.s is None:
            d.s = self._system.trades.get((u, Instrument.KR_STOCK, "nxt"))
        fx = self._system.usdkrw_spot
        d.entry_disp = disparity(fx, d.hl_bid_n, d.s)
        d.exit_disp = disparity(fx, d.hl_ask_n, d.s)

    def _evaluate(self, u: Underlying) -> None:
        d = self._day(u)
        self._compute(u, d)
        st = self.settings(u)
        self._auto_off(u, d, st)
        if not (st.run_entry or st.run_exit):
            return
        if window_state(self._clock(), d.t0, st.start_s, st.end_s) != "open":
            return
        # 발주된 단은 빼고 판정 — 나머지 단은 창이 끝날 때까지 계속(사용자 2026-10-23)
        plans = judge(d.entry_disp, d.exit_disp, st, d.fired_tiers)
        if not plans:
            return
        d.fired_tiers.update((p.block, p.index) for p in plans)  # 단마다 한 번 — 발주 전에 잠근다
        d.fired, d.fired_wall = True, self._wall()
        task = asyncio.create_task(self._fire(u, d, plans))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fire(self, u: Underlying, d: DayState, plans: list[Plan]) -> None:
        fx, s = self._system.usdkrw_spot, d.s
        info = self._system.instruments.get((u, Instrument.HL_PERP))
        sz_dec = getattr(info, "sz_decimals", None) if info is not None else None
        intents: list[OrderIntent] = []
        results: list[TierResult] = []
        for p in plans:
            px = (order_price(s, p.price_spread, fx, p.side, sz_dec)
                  if s is not None and fx is not None else None)
            res = TierResult(p.block, p.index, p.side.value, p.qty, px or 0.0)
            results.append(res)
            if px is None:
                res.error = "주문가 계산 불가(S·환율 없음)"
                continue
            tag = f"{'진입' if p.block == 'entry' else '청산'}{p.index + 1}"
            intents.append(OrderIntent(
                venue=Venue.HYPERLIQUID, underlying=u, instrument=Instrument.HL_PERP,
                side=p.side, qty=p.qty, order_type=OrderType.LIMIT, price=px,
                source=SOURCE, tag=tag))
        d.results.extend(results)  # 같은 창에서 여러 번 발주될 수 있어 누적
        self._log.info("[NXT개장] %s 발주 %s — 진입괴리 %s 청산괴리 %s S %s 환 %s: %s", u.value,
                       _hms(self._wall()), _pct(d.entry_disp), _pct(d.exit_disp),
                       f"{s:,.0f}" if s else "-", f"{fx:.2f}" if fx else "-",
                       [(r.block, r.index + 1, r.side, r.qty, r.price) for r in results])
        if not intents:
            return
        cloids = [self._system.new_hl_cloid() for _ in intents]
        try:
            outs = await self._system.place_many(intents, cloids)
        except Exception as exc:  # noqa: BLE001 - 예외는 전부 실패로 기록(재전송 없음)
            for r in results:
                if r.error is None:
                    r.error = f"{type(exc).__name__}: {exc}"
            self._log.error("[NXT개장] %s 묶음 발주 실패: %s", u.value, exc)
            return
        live = [r for r in results if r.error is None]
        for r, out in zip(live, outs, strict=False):
            r.order_id = getattr(out, "order_id", None)
            r.error = getattr(out, "error", None)
            self._log.info("[NXT개장] %s %s%d %s %s @ %s → %s", u.value,
                           "진입" if r.block == "entry" else "청산", r.index + 1, r.side,
                           r.qty, r.price, r.order_id or f"실패 {r.error}")
        self._auto_off(u, d, self.settings(u))  # 발주했으니 오늘은 끝 — 실행 끔

    # --- 화면 ---

    def live_snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for u in Underlying:
            d = self._day(u)
            self._compute(u, d)
            st = self.settings(u)
            self._auto_off(u, d, st)  # 시세 이벤트가 없어도 화면 갱신 때 시각이 지났으면 끈다
            out[u.value] = {
                "run_entry": st.run_entry, "run_exit": st.run_exit, "ref_qty": st.ref_qty,
                "window": window_state(self._clock(), d.t0, st.start_s, st.end_s),
                "t0": _hms(d.t0_wall) if d.t0_wall else None,
                "t0_rejected": d.t0_rejected,
                "fired": d.fired,
                "fired_tiers": [list(k) for k in sorted(d.fired_tiers)],  # JSON 모양(리스트)
                "fired_at": _hms(d.fired_wall) if d.fired_wall else None,
                "s": d.s, "hl_bid": d.hl_bid_n, "hl_ask": d.hl_ask_n,
                # HL 호가단위 콤보용(체결쏴와 같은 모양) — 지금 가격대의 틱 목록·적용 중인 머지
                "hl_merge_ticks": self._merge_ticks(u),
                "hl_merge_active": self._merge_active(u),
                "fx": self._system.usdkrw_spot, "fx_src": self._system.usdkrw_spot_src,
                "entry_disp": d.entry_disp, "exit_disp": d.exit_disp,
                "note": d.note,
                "results": [vars(r) for r in d.results],
                # 단별 역산 주문가(표시용) — 지금 S·환율로 풀어 격자에 맞춘 값
                "entry_prices": self._tier_prices(u, d, st.entries, Side.SELL),
                "exit_prices": self._tier_prices(u, d, st.exits, Side.BUY),
            }
        return out

    def _merge_ticks(self, u: Underlying) -> list[dict[str, Any]]:
        hq = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        ref = (hq.ask or hq.bid) if hq is not None else None
        if not ref:
            return []
        return [{"tick": s, "n_sig_figs": nsf, "mantissa": mant}
                for s, nsf, mant in merge_tick_options(float(ref))]

    def _merge_active(self, u: Underlying) -> dict[str, Any] | None:
        fn = getattr(self._system, "hl_merge_active", None)
        active = fn(u) if callable(fn) else None
        return ({"n_sig_figs": active[0], "mantissa": active[1]}
                if active is not None else None)

    def _tier_prices(self, u: Underlying, d: DayState, tiers: list[Tier],
                     side: Side) -> list[float | None]:
        fx, s = self._system.usdkrw_spot, d.s
        info = self._system.instruments.get((u, Instrument.HL_PERP))
        sz_dec = getattr(info, "sz_decimals", None) if info is not None else None
        out: list[float | None] = []
        for t in tiers:
            if t.price_spread is None or s is None or fx is None:
                out.append(None)
            else:
                out.append(order_price(s, t.price_spread, fx, side, sz_dec))
        return out


def _hms(wall: float | None) -> str:
    if wall is None:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(wall)) + f".{int(wall * 1000) % 1000:03d}"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:+.3f}%"
