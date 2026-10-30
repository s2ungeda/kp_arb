"""오전 NXT 개장 매매 실행 엔진 — DESIGN-nxt-open.md §3·§6.

CoreState.nxt_open(종목별 설정)과 LiveSystem을 묶는다. 이벤트 주도(별도 루프 없음):
- NXT 주식 1호가(on_quote, market "nxt" — 통합호가 UH1의 NXT 쪽) → 그날 첫 호가면 T0(개장 시각 +
  유예 안일 때만) + S(1호가 가중값, theory.quote_price) 갱신. 주식 체결은 안 받는다(사용자
  2026-10-28 "NXT 개장 매매에도 현재가 대신 호가 가중치, 주식 체결 시세 구독 안 해도 될 것 같음")
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
from .hl_merge import merge_active_dict, merge_tick_options
from .nxt_open import (
    SOURCE,
    AutoExit,
    NxtOpenScreen,
    NxtOpenSettings,
    Plan,
    Tier,
    base_due,
    day_over,
    disparity,
    hl_current,
    hotime_text,
    judge,
    level_price,
    opposite,
    order_price,
    range_block,
    range_limits,
    remaining_tiers,
    t0_verdict,
    window_state,
)
from .theory import quote_price


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
    auto: bool = False               # 자동청산 주문(§4a) — 상태줄 "진입1청산 …"
    auto_price: float | None = None  # 전략주문이면 그 단에 걸어 둔 자동청산가(없으면 None)


# 등록 전 체결 보관함 크기 — HL 테이커 즉시 체결은 통보가 발주 응답보다 먼저 올 수 있다(§4a-3)
EARLY_FILLS_KEEP = 200
# [임시 진단 2026-10-30] 창 안 판정 로그(DESIGN-nxt-open §6) — 10-30 첫 호가 뒤 1.6초 늦은 발주의
# 원인을 가리려고 넣음. 원인 확인 뒤 False 또는 제거(사용자 "결과 나오면 없앨 수 있게").
DIAG_EVAL_LOG = True


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
    auto_exits: dict[str, AutoExit] = field(default_factory=dict)  # 주문번호 → 자동청산 대기(§4a)
    hl_base: float | None = None      # §4b HL 기준가(HL기준시간의 HL 현재가) — 하루 한 번
    hl_base_wall: float | None = None
    # 금지 사유를 남긴 단 → 그 사유(같은 사유는 한 번, 사유가 바뀌면 다시 남긴다)
    blocked_noted: dict[tuple[str, int], str] = field(default_factory=dict)
    nxt_ts: float | None = None       # 마지막 NXT 호가의 거래소 시각(hotime) — 임시 진단 로그용
    eval_sig: tuple[Any, ...] | None = None  # 직전 판정 로그의 (값·결과) — 같으면 안 남긴다


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
        # 등록 전 체결(§4a-3): 주문번호 → [(체결번호, 수량)] — 발주 응답보다 먼저 온 통보 보관
        self._early_fills: dict[str, list[tuple[str, float]]] = {}
        for name, handler in (("on_quote", self._on_quote), ("on_fx", self._on_fx),
                              ("on_fill", self._on_fill)):
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

    def _on_nxt_quote(self, quote: Quote) -> None:
        """NXT 주식 1호가(UH1의 NXT 쪽) — S = 1호가 가중값(현재가 규칙 ①~③), 그날 첫 호가가 T0
        (사용자 2026-10-28 답 1: 개장 시각 뒤 첫 NXT 호가 — 운영 실측 개장 전 NXT 호가 0건, 08:00
        첫 분에 체결과 같이 옴). 호가가 비어 S가 없으면(규칙 ①) T0 후보로 안 본다."""
        u = quote.underlying
        d = self._day(u)
        s = quote_price(quote.bid, quote.ask, quote.bid_qty, quote.ask_qty)
        d.nxt_ts = quote.ts
        if s is None:
            self._evaluate(u, "NXT")
            return
        d.s = s
        if d.t0 is None and not d.t0_rejected:
            now_wall = self._wall()
            st = self.settings(u)
            verdict = t0_verdict(now_wall, st.open_hms)
            if verdict == "early":
                pass  # 개장 전 호가 — T0 후보 아님(무시), 개장 뒤 첫 호가를 기다린다
            elif verdict == "ok":
                d.t0, d.t0_wall = self._clock(), now_wall
                self._log.info("[NXT개장] %s 첫 NXT 호가 %s S %s — 창 +%.1f~+%.1f초", u.value,
                               _hms(now_wall), f"{d.s:,.0f}", st.start_s, st.end_s)
            else:
                d.t0_rejected = True
                d.note = "오늘 판정 없음(개장 밖)"
                self._log.warning("[NXT개장] %s 첫 NXT 호가가 %s(개장 %s 기준 유예 밖) — 오늘 창 "
                                  "없음", u.value, _hms(now_wall), st.open_hms)
        self._evaluate(u, "NXT")

    def _on_quote(self, quote: Quote) -> None:
        if quote.instrument is Instrument.HL_PERP:
            self._evaluate(quote.underlying, "HL")
        elif quote.instrument is Instrument.KR_STOCK and quote.market == "nxt":
            self._on_nxt_quote(quote)

    def _on_fx(self) -> None:
        for u in Underlying:
            self._evaluate(u, "환율")

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
        nq = self._system.quotes.get((u, Instrument.KR_STOCK, "nxt"))
        s = quote_price(nq.bid, nq.ask, nq.bid_qty, nq.ask_qty) if nq is not None else None
        if s is not None:  # 호가가 비면(규칙 ①) 마지막 S 유지
            d.s = s
        fx = self._system.usdkrw_spot
        d.entry_disp = disparity(fx, d.hl_bid_n, d.s)
        d.exit_disp = disparity(fx, d.hl_ask_n, d.s)
        self._capture_base(u, d)

    # --- 리스크 방지(§4b, 사용자 2026-10-29) ---

    def _hl_now(self, u: Underlying) -> float | None:
        """HL 현재가 = 마지막 체결가, 없으면 호가 중간값(§4b, 확인 5)."""
        hq = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        return hl_current(self._system.trades.get((u, Instrument.HL_PERP, "hl")),
                          hq.bid if hq else None, hq.ask if hq else None)

    def _capture_base(self, u: Underlying, d: DayState) -> None:
        """HL기준시간을 지난 첫 시세(10초 안)에서 HL 현재가를 기준가로 — 하루 한 번."""
        if d.hl_base is not None:
            return
        st = self.settings(u)
        if not base_due(self._wall(), st.hl_base_hms):
            return
        now = self._hl_now(u)
        if now is None:
            return
        d.hl_base, d.hl_base_wall = now, self._wall()
        sell_lim, buy_lim = range_limits(now, st.range_limit)
        self._log.info("[NXT개장] %s HL 기준가 %.1f(%s, 기준시간 %s) — 매도 한도 %.1f 매수 한도 "
                       "%.1f(범위 %g%%)", u.value, now, _hms(d.hl_base_wall), st.hl_base_hms,
                       sell_lim, buy_lim, st.range_limit * 100)

    def _blocked(self, u: Underlying, d: DayState, side: Side) -> str | None:
        """§4b-3: 지금 HL 현재가가 범위 밖이면 사유(금지), 아니면 None."""
        return range_block(side, self._hl_now(u), d.hl_base, self.settings(u).range_limit)

    def _evaluate(self, u: Underlying, src: str = "") -> None:
        """시세 하나가 올 때마다 — src는 깨운 시세("NXT"/"HL"/"환율", 임시 진단 로그용)."""
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
        # §4b 금지: 범위 밖이면 그 단은 이번에 안 내고 발주된 단으로 치지 않는다(창 끝까지 계속
        # 시도 — 확인 8). 사유는 단마다 같은 글이면 한 번만(바뀌면 다시) 남긴다.
        allowed: list[Plan] = []
        blocked: list[str] = []
        for p in plans:
            why = self._blocked(u, d, p.side)
            if why is None:
                allowed.append(p)
                continue
            blocked.append(f"{_tier_name(p.block, p.index)} {why}")
            if d.blocked_noted.get((p.block, p.index)) != why:
                d.blocked_noted[(p.block, p.index)] = why
                d.note = f"{_tier_name(p.block, p.index)} {why}"
                self._log.warning("[NXT개장] %s %s", u.value, d.note)
        plans = allowed
        verdict = (("발주 " + ",".join(_tier_name(p.block, p.index) for p in plans)) if plans
                   else ("금지 " + "; ".join(blocked)) if blocked else "미달")
        self._diag_eval(u, d, src, verdict)
        if not plans:
            return
        d.fired_tiers.update((p.block, p.index) for p in plans)  # 단마다 한 번 — 발주 전에 잠근다
        d.fired, d.fired_wall = True, self._wall()
        task = asyncio.create_task(self._fire(u, d, plans, d.fired_wall))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _diag_eval(self, u: Underlying, d: DayState, src: str, verdict: str) -> None:
        """[임시 진단 2026-10-30] 창 안 판정 한 줄 — 값(S·HL est·환율)이나 결과가 직전 로그와 같으면
        안 남긴다(사용자 "많은 로그는 필요 없음"). DIAG_EVAL_LOG로 끈다."""
        if not DIAG_EVAL_LOG:
            return
        fx = self._system.usdkrw_spot
        sig = (d.s, d.hl_bid_n, d.hl_ask_n, fx, verdict)
        if sig == d.eval_sig:
            return
        d.eval_sig = sig
        self._log.info("[NXT개장] %s 판정 %s(%s) NXT호가시각 %s S %s HL매수est %s HL매도est %s "
                       "환 %s 진입괴리 %s 청산괴리 %s → %s", u.value, _hms(self._wall()),
                       src or "-",
                       hotime_text(d.nxt_ts), f"{d.s:,.0f}" if d.s else "-",
                       f"{d.hl_bid_n:.2f}" if d.hl_bid_n is not None else "-",
                       f"{d.hl_ask_n:.2f}" if d.hl_ask_n is not None else "-",
                       f"{fx:.2f}" if fx else "-", _pct(d.entry_disp), _pct(d.exit_disp), verdict)

    async def _fire(self, u: Underlying, d: DayState, plans: list[Plan],
                    decided_wall: float | None = None) -> None:
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
            # 자동청산가(§4a-2): 발주 시점의 S·환으로 청산가 스프레드를 풀어 반대 방향 격자에 맞춤
            if p.exit_spread is not None and s is not None and fx is not None:
                res.auto_price = order_price(s, p.exit_spread, fx, opposite(p.side), sz_dec)
                if res.auto_price is None:
                    self._log.warning("[NXT개장] %s %s%d 자동청산가 계산 불가 — 자동청산 없음",
                                      u.value, "진입" if p.block == "entry" else "청산",
                                      p.index + 1)
            tag = f"{'진입' if p.block == 'entry' else '청산'}{p.index + 1}"
            intents.append(OrderIntent(
                venue=Venue.HYPERLIQUID, underlying=u, instrument=Instrument.HL_PERP,
                side=p.side, qty=p.qty, order_type=OrderType.LIMIT, price=px,
                source=SOURCE, tag=tag))
        d.results.extend(results)  # 같은 창에서 여러 번 발주될 수 있어 누적
        self._log.info("[NXT개장] %s 판정 %s 발주 %s — 진입괴리 %s 청산괴리 %s S %s 환 %s: %s",
                       u.value, _hms(decided_wall), _hms(self._wall()), _pct(d.entry_disp),
                       _pct(d.exit_disp),
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
            if r.order_id and r.auto_price is not None:
                self._arm_auto_exit(u, d, r)
        self._auto_off(u, d, self.settings(u))  # 발주했으니 오늘은 끝 — 실행 끔

    # --- 자동청산(§4a, 사용자 2026-10-29) ---

    def _arm_auto_exit(self, u: Underlying, d: DayState, r: TierResult) -> None:
        """전략주문 주문번호에 자동청산 대기를 걸고, 등록 전에 온 체결이 있으면 합친다."""
        oid = str(r.order_id)
        ae = AutoExit(order_id=oid, block=r.block, index=r.index, side=opposite(Side(r.side)),
                      qty=float(r.qty), price=float(r.auto_price or 0.0))
        d.auto_exits[oid] = ae
        self._log.info("[NXT개장] %s %s 자동청산 대기 #%s — 전량 체결되면 %s %s @ %s", u.value,
                       _tier_name(r.block, r.index), oid, ae.side.value, ae.qty, ae.price)
        done = False
        for fid, qty in self._early_fills.pop(oid, []):
            done = ae.add_fill(fid, qty) or done
        if done:
            self._fire_auto_exit(u, d, oid)

    def _on_fill(self, fill: Any) -> None:
        """체결 통보 — 자동청산 대기 중인 주문이면 누적, 전량이면 자동청산 발주. 아직 등록 안 된
        주문번호면 잠시 보관(HL 즉시 체결은 통보가 발주 응답보다 먼저 온다)."""
        oid = str(getattr(fill, "order_id", "") or "")
        if not oid:
            return
        fid = str(getattr(fill, "fill_id", "") or "")
        qty = float(getattr(fill, "qty", 0.0) or 0.0)
        for u, d in self._days.items():
            ae = d.auto_exits.get(oid)
            if ae is None:
                continue
            if ae.add_fill(fid, qty):
                self._fire_auto_exit(u, d, oid)
            return
        kept = self._early_fills.setdefault(oid, [])
        kept.append((fid, qty))
        while len(self._early_fills) > EARLY_FILLS_KEEP:  # 오래된 주문번호부터 버림
            self._early_fills.pop(next(iter(self._early_fills)))

    def _fire_auto_exit(self, u: Underlying, d: DayState, oid: str) -> None:
        ae = d.auto_exits.pop(oid, None)
        if ae is None:
            return
        self._spawn(self._place_auto_exit(u, d, ae))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _place_auto_exit(self, u: Underlying, d: DayState, ae: AutoExit) -> None:
        """자동청산 주문(§4a-3): 전략주문 수량 전부를 발주 때 정한 가격에, 반대 방향 GTC 지정가.
        자동청산 주문에는 다시 자동청산을 걸지 않는다."""
        name = _tier_name(ae.block, ae.index)
        res = TierResult(ae.block, ae.index, ae.side.value, ae.qty, ae.price, auto=True)
        d.results.append(res)
        why = self._blocked(u, d, ae.side)  # §4b — 자동청산도 금지 검사, 걸리면 내지 않고 끝
        if why is not None:
            res.error = why
            d.fired, d.fired_wall = True, self._wall()
            self._log.warning("[NXT개장] %s %s청산 안 냄 — %s", u.value, name, why)
            return
        intent = OrderIntent(venue=Venue.HYPERLIQUID, underlying=u, instrument=Instrument.HL_PERP,
                             side=ae.side, qty=ae.qty, order_type=OrderType.LIMIT, price=ae.price,
                             source=SOURCE, tag=f"{name}청산")
        self._log.info("[NXT개장] %s %s청산 발주 — #%s 전량 체결 → %s %s @ %s", u.value, name,
                       ae.order_id, ae.side.value, ae.qty, ae.price)
        try:
            outs = await self._system.place_many([intent], [self._system.new_hl_cloid()])
        except Exception as exc:  # noqa: BLE001 - 실패는 기록만(재전송 없음, CLAUDE.md §7)
            res.error = f"{type(exc).__name__}: {exc}"
            self._log.error("[NXT개장] %s %s청산 발주 실패: %s", u.value, name, exc)
            return
        out = outs[0] if outs else None
        res.order_id = getattr(out, "order_id", None)
        res.error = getattr(out, "error", None)
        d.fired, d.fired_wall = True, self._wall()
        self._log.info("[NXT개장] %s %s청산 %s %s @ %s → %s", u.value, name, ae.side.value, ae.qty,
                       ae.price, res.order_id or f"실패 {res.error}")

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
                # 자동청산 대기(§4a) — 전량 체결을 기다리는 전략주문
                "auto_pending": [{"order_id": a.order_id, "tag": _tier_name(a.block, a.index),
                                  "side": a.side.value, "qty": a.qty, "filled": a.filled,
                                  "price": a.price} for a in d.auto_exits.values()],
                # 리스크 방지(§4b) — 기준가·한도·지금 HL 현재가
                "hl_base": d.hl_base,
                "hl_base_at": _hms(d.hl_base_wall) if d.hl_base_wall else None,
                "sell_limit": (range_limits(d.hl_base, st.range_limit)[0]
                               if d.hl_base is not None else None),
                "buy_limit": (range_limits(d.hl_base, st.range_limit)[1]
                              if d.hl_base is not None else None),
                "hl_last": self._hl_now(u),
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
        hq = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        ref = (hq.ask or hq.bid) if hq is not None else None
        return merge_active_dict(active, ref)  # 틱 글 포함(화면 글자, 2026-10-30)

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


def _tier_name(block: str, index: int) -> str:
    return f"{'진입' if block == 'entry' else '청산'}{index + 1}"


def _hms(wall: float | None) -> str:
    if wall is None:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(wall)) + f".{int(wall * 1000) % 1000:03d}"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:+.3f}%"
