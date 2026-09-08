"""자동M(체결쏴) 실행 뼈대 — 순수 상태변화 (DESIGN-auto-m-exec.md — 자동M 단일 스펙, §11 전략·화면).

I/O 없음. 코어(결선 단계)가 시세마다 ``Signals``를 넣고 ``evaluate``를 부르고, 주문 사건이 오면
``on_*`` 를 부른다. 여기서 나온 ``Action`` 목록을 코어가 실제 발주/취소로 옮긴다.

범위: **정방향 진입·청산**(선주문 = 국내 SF maker, 후주문 = HL taker, 후주문 수량 = SF 체결 × 10).
역방향은 대칭이라 같은 뼈대로 뒤에 붙인다(exec §11). 다른 전략도 이 뼈대를 공유한다(exec §0).

상태(exec §2): IDLE 대기 · ARMED 감시 · PRE_RESTING 선주문대기 · PRE_PARTIAL 부분체결 ·
POST_PENDING 후주문대기 · SETTLE_DELAY 딜레이대기 · HALTED 중지(사람이 풀어야 재개).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from enum import StrEnum

from .disparity import maker_price_for_spread
from .domain.enums import Block, Instrument, Side, Underlying
from .theory import in_time_window
from .ticks import ceil_to_tick, floor_to_tick, tick_for

HL_PER_SF = 10  # SF 1계약 = HL 10계약 (§1)

Levels = Sequence[tuple[float, float]]  # 호가창 [(가격, 잔량), …] 1호가부터


class LegStatus(StrEnum):
    IDLE = "idle"
    ARMED = "armed"
    PRE_RESTING = "pre_resting"
    PRE_PARTIAL = "pre_partial"
    POST_PENDING = "post_pending"
    SETTLE_DELAY = "settle_delay"
    HALTED = "halted"


# ---------------------------------------------------------------- 설정·입력 ---

def parse_hms(text: str) -> dtime:
    """"08:30:10" → time. "HH:MM"도 허용(초 0). 형식이 틀리면 ValueError."""
    parts = text.strip().split(":")
    if len(parts) == 2:
        parts.append("0")
    if len(parts) != 3:
        raise ValueError(f"시각 형식 오류: {text!r}")
    return dtime(int(parts[0]), int(parts[1]), int(parts[2]))


@dataclass
class AutoMSettings:
    """공통설정(체결쏴 설정) — DESIGN-auto-m §9 공통설정 창."""

    windows: tuple[tuple[str, str], ...] = (("08:30:10", "08:46:20"), ("15:35:30", "15:46:55"))
    pre_tick: dict[Underlying, int] = field(default_factory=lambda: {
        Underlying.SK_HYNIX: 3000, Underlying.SAMSUNG: 500, Underlying.HYUNDAI: 1000})
    pre_delay_ms: int = 1000     # 선주문 딜레이(ms) — 후주문 체결·거부·재발주 뒤 대기
    resume_delay_s: int = 10     # 시장 정지가 풀린 뒤 대기(초)
    pre_range: float = 0.004     # 선주문 발주 허용범위(0.4%)
    rel_buy: int = 1             # 매수 한계의 상대 매도N호가
    rel_sell: int = 1            # 매도 한계의 상대 매수N호가
    # 후주문(HL) 지정가 여유 — 항상 지정가(Gtc)만 쓴다(사용자 확정 2026-09-04, IOC·FOK 없음).
    # HL 매수 = 매도1호가 × (1 + hl_margin_buy) / HL 매도 = 매수1호가 × (1 − hl_margin_sell).
    # 잔량은 선주문 딜레이만큼 기다린 뒤 취소 → 체결차 → 중지.
    hl_margin_buy: float = 0.01
    hl_margin_sell: float = 0.01

    def post_price(self, side: Side, hl_bid: float, hl_ask: float) -> float:
        """후주문 지정가 — 상대 1호가에 여유를 얹어 taker로 잡히게."""
        if side is Side.BUY:
            return hl_ask * (1.0 + self.hl_margin_buy)
        return hl_bid * (1.0 - self.hl_margin_sell)

    def in_window(self, now: dtime) -> bool:
        return any(in_time_window(now, parse_hms(s), parse_hms(e)) for s, e in self.windows)


@dataclass(frozen=True)
class Signals:
    """시세 1건마다 코어가 계산해 넣는 입력(§3 판정·§6 역산에 필요한 것만)."""

    now: datetime
    mono: float                       # 단조 시계(초) — 딜레이·전환대기용
    # 실시간 괴리(§3): SF괴리 = HL est(호가창) 괴리 − SF 1호가 괴리 / S괴리는 계약수×10주 등가
    sf_spread_entry: float | None     # 진입용(HL 매수호가창 est − SF 매수1호가)
    s_spread_entry: float | None      # 진입용 S괴리
    sf_spread_exit: float | None      # 청산용(HL 매도호가창 est − SF 매도1호가)
    hl_disp_bid: float | None         # 후주문수량 est(HL 매수호가창) 괴리 — 진입 역산용
    hl_disp_ask: float | None         # 후주문수량 est(HL 매도호가창) 괴리 — 청산 역산용
    sf_theory: float | None           # 선물이론가(기준가_SF)
    stock_last: float | None          # 주식 현재가(Sprd 기준)
    sf_asks: Levels = ()              # SF 매도호가창(1호가부터) — 매수 한계용
    sf_bids: Levels = ()              # SF 매수호가창 — 매도 한계용
    market_halted: bool = False       # 선물시장 정지 오버레이(exec §8)
    resumed_mono: float | None = None  # 정지가 풀린 시각(재개 딜레이)


# --------------------------------------------------------------- 행동(출력) ---

@dataclass(frozen=True)
class Action:
    """코어가 실행할 일. kind: place_pre | cancel_pre | place_post | halt | notify."""

    kind: str
    side: Side | None = None
    qty: int = 0
    price: float | None = None
    order_id: str | None = None
    reason: str = ""


# ---------------------------------------------------------------- 상태 모델 ---

@dataclass
class Accum:
    """누적 매매결과 한 블록(§9a) — 체결마다 갱신, clear로 0."""

    hl_qty: float = 0.0
    hl_px_sum: float = 0.0   # HL 체결가 × 수량 합
    fx_sum: float = 0.0      # 환진입가(원달러선물 호가) × HL 수량 합
    sf_qty: int = 0          # SF 체결 계약수
    sf_px_sum: float = 0.0

    def hl_avg(self) -> float | None:
        return self.hl_px_sum / self.hl_qty if self.hl_qty > 0 else None

    def fx_avg(self) -> float | None:
        return self.fx_sum / self.hl_qty if self.hl_qty > 0 else None

    def sf_avg(self) -> float | None:
        return self.sf_px_sum / self.sf_qty if self.sf_qty > 0 else None

    def sprd(self, stock_last: float | None, sf_theory: float | None) -> float | None:
        """Sprd = (환×HL평균가 − S현재가)/S현재가 − (SF평균가 − SF이론가)/SF이론가 (엑셀 메인 I25).

        평균가는 누적 체결 가중, S현재가·SF이론가는 실시간 값(사용자 확정 2026-09-04)."""
        fx, hl, sf = self.fx_avg(), self.hl_avg(), self.sf_avg()
        if None in (fx, hl, sf) or not stock_last or not sf_theory:
            return None
        assert fx is not None and hl is not None and sf is not None
        return (fx * hl - stock_last) / stock_last - (sf - sf_theory) / sf_theory

    def clear(self) -> None:
        self.hl_qty = self.hl_px_sum = self.fx_sum = self.sf_px_sum = 0.0
        self.sf_qty = 0


@dataclass
class Leg:
    """진입 또는 청산 한 줄의 실행 상태(세트마다 진입·청산 각 1개)."""

    block: Block
    running: bool = False
    status: LegStatus = LegStatus.IDLE
    pre_order_id: str | None = None
    pre_price: float | None = None
    pre_qty: int = 0            # 이번 선주문 계약수
    pre_filled: int = 0         # 이번 선주문 체결 계약수
    post_pending: float = 0.0   # 후주문(HL) 체결 대기 계약수 — HL은 소수 체결(0.588 등, 실측 09-07)
    delay_until: float | None = None
    replace_pending: bool = False   # 역산가 바뀜 → 취소 보냄, 취소 확인 대기
    cancel_sent: bool = False       # 관문(G2·G5·G6) 취소를 이미 보냄 — 확인 올 때까지 재전송 안 함
    await_post_then_delay: bool = False  # 취소 확인됨, 병행 후주문 체결 확인 뒤 딜레이
    halt_reason: str = ""
    # 마지막 판정 결과 한 줄(어느 게이트에서 막혔나·통과했나 + 숫자) — 로그는 바뀔 때만 남긴다
    block_reason: str = ""
    acc: Accum = field(default_factory=Accum)

    @property
    def pre_side(self) -> Side:
        return Side.BUY if self.block is Block.ENTRY else Side.SELL

    @property
    def post_side(self) -> Side:
        return Side.SELL if self.block is Block.ENTRY else Side.BUY

    def _clear_pre(self) -> None:
        self.pre_order_id = self.pre_price = None
        self.pre_qty = self.pre_filled = 0
        self.cancel_sent = False


@dataclass
class AutoMSet:
    """세트 1개(정방향) — 세트 설정 + RT·체결차 + 진입/청산 실행 상태."""

    target_qty: int = 0
    per_qty: int = 0
    switch_delay_s: int = 0                 # 전환딜레이(초)
    en_sf: float | None = None              # 진입 SF 기준값(소수, 0.005 = 0.5%)
    en_s: float | None = None               # 진입 S 기준값
    ex_sf: float | None = None              # 청산 SF 기준값
    rt: int = 0                             # RT선진입(계약) — 후주문 전부 체결 때 증감
    fill_diff: float = 0.0                  # 체결차 = SF잔고×10 + HL잔고 (이 세트 체결 기준, 소수)
    sf_net: int = 0                         # 이 세트가 잡은 SF 순잔고(계약, 매수 +)
    hl_net: float = 0.0                     # 이 세트가 잡은 HL 순잔고(계약, 매도 −)
    hl_rt_carry: float = 0.0                # RT 환산 전 HL 체결 잔여분(10 미만) — 소수 체결 누적용
    entry: Leg = field(default_factory=lambda: Leg(Block.ENTRY))
    exit: Leg = field(default_factory=lambda: Leg(Block.EXIT))
    last_entry_fill_mono: float | None = None
    last_exit_fill_mono: float | None = None

    def leg(self, block: Block) -> Leg:
        return self.entry if block is Block.ENTRY else self.exit

    def threshold(self, block: Block) -> float | None:
        return self.en_sf if block is Block.ENTRY else self.ex_sf


# ---------------------------------------------------------------- 순수 계산 ---

def order_qty(block: Block, per_qty: int, target_qty: int, rt: int) -> int:
    """이번에 낼 계약수(§5) — 진입 Min(1회주문, 목표−RT) / 청산 Min(1회주문, RT). 0 이하면 0."""
    room = target_qty - rt if block is Block.ENTRY else rt
    return max(0, min(per_qty, room))


def rel_quote(levels: Levels, n: int) -> float | None:
    """상대 N호가 = 호가창의 실제 N번째 호가(빈 단계 건너뜀, §6.3). 없으면 None."""
    prices = [px for px, _q in levels if px and px > 0]
    return prices[n - 1] if 0 < n <= len(prices) else None


def limit_price(side: Side, rel_px: float, tick: int, rng: float) -> float:
    """발주 허용 한계(§6.3).

    매수 = (상대매도N호가 − 1틱) × (1 − 범위) / 매도 = (상대매수N호가 + 1틱) × (1 + 범위).
    """
    if side is Side.BUY:
        return (rel_px - tick) * (1.0 - rng)
    return (rel_px + tick) * (1.0 + rng)


def pre_order_price(
    block: Block, sf_theory: float, hl_disp: float, threshold: float, tick: int,
) -> float:
    """역산가(§6.1) P = 이론가 × (1 + HL_est괴리 − 기준값) → 호가단위(§6.2: 매수 내림/매도 올림)."""
    raw = maker_price_for_spread(sf_theory, hl_disp, threshold)
    return floor_to_tick(raw, tick) if block is Block.ENTRY else ceil_to_tick(raw, tick)


def within_limit(side: Side, price: float, limit: float) -> bool:
    """매수는 한계 이상, 매도는 한계 이하여야 발주(§6.3)."""
    return price >= limit if side is Side.BUY else price <= limit


_EPS = 1e-9  # HL 소수 계약 비교용(0.588 같은 체결이 오므로 "== 0" 대신 사용)


def post_done(leg: Leg) -> bool:
    """후주문 대기분이 없는가(소수 오차 허용)."""
    return leg.post_pending <= _EPS


def fill_diff(sf_net_contracts: int, hl_net_contracts: float) -> float:
    """체결차(§8) = SF 잔고 × 10 + HL 잔고(매도 −). 0이면 완전 헤지."""
    return sf_net_contracts * HL_PER_SF + hl_net_contracts


# ---------------------------------------------------------------- 상태변화 ---

def _cancel_if_resting(leg: Leg) -> list[Action]:
    """걸어둔 선주문이 있으면 취소 요청(취소 확인은 on_pre_cancelled).

    전부 체결된 선주문(pre_filled ≥ pre_qty)은 취소할 잔량이 없다 — 보내면 LS가 거부한다
    (실측 2026-09-07 "정정/취소할 수량이 없습니다", 중지 뒤 실행 끔에서).
    """
    resting = leg.pre_order_id is not None and not leg.replace_pending
    if resting and not leg.cancel_sent and (leg.pre_qty <= 0 or leg.pre_filled < leg.pre_qty):
        # 한 번만 보낸다 — 관문에 막힌 채 매 틱 재전송하면 취소 확인이 오기 전 같은 요청이 여러 번
        # 나간다(실측 2026-09-08: 0.2초에 3번). 확인(취소/거부/체결)이 오면 _clear_pre가 되돌린다.
        leg.cancel_sent = True
        return [Action("cancel_pre", order_id=leg.pre_order_id)]
    return []


def _passes_signal(s: AutoMSet, leg: Leg, sig: Signals) -> bool:
    """G5 판정(exec §11.3, 정정 2026-09-07) — 진입: **S(현물)괴리 > 진입S만** / 청산: **비교 없음**.

    SF 기준값(진입SF·청산SF)은 판정이 아니라 **역산가(G6)** 에 쓴다 — 선주문은 그 기준값이
    체결로 보장되는 가격(maker)에 걸므로 SF 괴리를 따로 비교할 이유가 없다. S 괴리는
    진입 허용 조건으로만 본다(실제 매매는 SF+HL).
    """
    if leg.block is Block.ENTRY:
        if s.en_sf is None or s.en_s is None:
            return False
        return sig.s_spread_entry is not None and sig.s_spread_entry > s.en_s
    return s.ex_sf is not None


def _switch_wait(s: AutoMSet, leg: Leg, mono: float) -> bool:
    """G3 전환대기 — 진입은 직전 청산 체결 뒤, 청산은 직전 진입 체결 뒤 N초."""
    last = s.last_exit_fill_mono if leg.block is Block.ENTRY else s.last_entry_fill_mono
    return last is not None and mono - last < s.switch_delay_s


def evaluate(
    s: AutoMSet, block: Block, sig: Signals, settings: AutoMSettings, underlying: Underlying,
) -> list[Action]:
    """시세 1건에 대한 판정(exec §4 G1~G6 + §6 상태 규칙). 반환: 코어가 할 일."""
    leg = s.leg(block)

    def hold(reason: str, acts: list[Action] | None = None) -> list[Action]:
        leg.block_reason = reason
        return acts or []

    def pct(v: float | None) -> str:
        return f"{v * 100:.3f}%" if v is not None else "-"

    if leg.status is LegStatus.HALTED:
        return hold("중지")
    # G1 실행 꺼짐 → 미체결 취소, 대기
    if not leg.running:
        acts = _cancel_if_resting(leg)
        if post_done(leg) and leg.pre_order_id is None:
            leg.status = LegStatus.IDLE
        return hold("G1 실행 꺼짐", acts)
    if leg.status is LegStatus.IDLE:
        leg.status = LegStatus.ARMED
    # 시장 정지(exec §8) — 신규·정정 중단 + 미체결 취소, HL은 손대지 않음
    if sig.market_halted:
        return hold("시장 정지 — 신규·정정 중단", _cancel_if_resting(leg))
    if sig.resumed_mono is not None and sig.mono - sig.resumed_mono < settings.resume_delay_s:
        return hold(f"재개 딜레이 {settings.resume_delay_s}초")
    # 사건 대기 중인 상태는 시세로 바꾸지 않는다
    if leg.status is LegStatus.POST_PENDING or leg.replace_pending or leg.await_post_then_delay:
        return hold("후주문/취소 확인 대기")
    if leg.status is LegStatus.SETTLE_DELAY:
        if leg.delay_until is not None and sig.mono < leg.delay_until:
            return hold(f"선주문 딜레이 {settings.pre_delay_ms}ms")
        leg.delay_until = None
        leg.status = LegStatus.ARMED
    # G2 주문가능시간
    if not settings.in_window(sig.now.time()):
        # 근거에 현재 시각을 넣지 않는다 — 매초 "바뀐 근거"가 되어 초당 한 줄씩 쌓임(실측 09-07)
        return hold("G2 주문가능시간 밖", _cancel_if_resting(leg))
    # G3 전환대기 · G4 여유 계약수 — 새로 내지 않음(걸어둔 것은 유지)
    if _switch_wait(s, leg, sig.mono):
        return hold(f"G3 전환대기 {s.switch_delay_s}초")
    qty = order_qty(block, s.per_qty, s.target_qty, s.rt)
    if qty < 1 and leg.pre_order_id is None:
        return hold(f"G4 여유 없음 (목표 {s.target_qty} RT {s.rt} 1회 {s.per_qty})")
    # G5 판정
    if not _passes_signal(s, leg, sig):
        if block is Block.ENTRY:
            why = (f"G5 미달 S {pct(sig.s_spread_entry)}>{pct(s.en_s)}? "
                   f"(SF {pct(sig.sf_spread_entry)})")
        else:
            why = "G5 청산 기준값 없음"
        return hold(why, _cancel_if_resting(leg))
    # G6 역산가 → 허용범위
    thr = s.threshold(block)
    hl_disp = sig.hl_disp_bid if block is Block.ENTRY else sig.hl_disp_ask
    tick = settings.pre_tick.get(underlying)
    if sig.sf_theory is None or hl_disp is None or thr is None or not tick:
        return hold(f"G6 입력 없음 (이론가 {sig.sf_theory} HL괴리 {pct(hl_disp)} 틱 {tick})")
    price = pre_order_price(block, sig.sf_theory, hl_disp, thr, tick)
    side = leg.pre_side
    rel = (rel_quote(sig.sf_asks, settings.rel_buy) if side is Side.BUY
           else rel_quote(sig.sf_bids, settings.rel_sell))
    if rel is None:
        return hold("G6 SF 호가 없음")
    # 한계의 "상대N호가 ∓ 1틱"에서 1틱은 **시세(호가창)의 호가단위** = 한 호가 옆(사용자 확정
    # 2026-09-08). 선주문 주문단위(settings.pre_tick)는 역산가를 주문 단위로 맞추는 데만 쓴다.
    mkt_tick = tick_for(Instrument.KR_STOCK_FUTURE, rel)
    limit = limit_price(side, rel, mkt_tick, settings.pre_range)
    if not within_limit(side, price, limit):
        return hold(f"G6 한계 밖 역산가 {price:,.0f} 한계 {limit:,.0f} "
                    f"(상대호가 {rel:,.0f} 호가단위 {mkt_tick})", _cancel_if_resting(leg))
    basis = (f"역산가 {price:,.0f} = 이론가 {sig.sf_theory:,.0f}×(1+{pct(hl_disp)}−{pct(thr)}) "
             f"주문단위 {tick} 한계 {limit:,.0f}(호가단위 {mkt_tick})")
    # 통과 — 없으면 발주, 있고 역산가가 바뀌었으면 재발주 규칙(취소→후주문 확인→딜레이→신규)
    if leg.pre_order_id is None and leg.status is LegStatus.ARMED:
        if qty < 1:
            return hold(f"G4 여유 없음 (목표 {s.target_qty} RT {s.rt})")
        leg.pre_price, leg.pre_qty, leg.pre_filled = price, qty, 0
        leg.cancel_sent = False
        leg.status = LegStatus.PRE_RESTING
        return hold(f"통과 → 선주문 {qty}계약 {basis}",
                    [Action("place_pre", side=side, qty=qty, price=price)])
    if leg.pre_order_id is not None and leg.pre_price != price:
        leg.replace_pending = True
        return hold(f"역산가 변경 {leg.pre_price:,.0f}→{price:,.0f} → 취소 후 재발주 ({basis})",
                    [Action("cancel_pre", order_id=leg.pre_order_id,
                            reason=f"역산가 변경 {leg.pre_price:g}→{price:g}")])
    return hold(f"유지 {basis}")


# ------------------------------------------------------------ 주문 사건 처리 ---

def _start_delay(leg: Leg, mono: float, settings: AutoMSettings) -> None:
    leg._clear_pre()
    leg.delay_until = mono + settings.pre_delay_ms / 1000.0
    leg.status = LegStatus.SETTLE_DELAY


def on_pre_ack(s: AutoMSet, block: Block, order_id: str) -> None:
    """선주문 접수 — 주문번호 보관(취소·체결 매칭용)."""
    s.leg(block).pre_order_id = order_id


def on_pre_reject(s: AutoMSet, block: Block, mono: float, settings: AutoMSettings) -> list[Action]:
    """선주문 거부 → 딜레이 뒤 다시 냄(멈추지 않음, exec ㄴ5)."""
    leg = s.leg(block)
    leg.replace_pending = False
    leg._clear_pre()  # 거부된 주문은 취소할 것도 없음 — 번호·취소 표시 정리
    _start_delay(leg, mono, settings)
    return [Action("notify", reason="선주문 거부 — 딜레이 뒤 재시도")]


def on_pre_fill(
    s: AutoMSet, block: Block, qty: int, price: float, mono: float,
) -> list[Action]:
    """선주문 체결(일부/전부) → 체결분 × 10 후주문 즉시(exec ㄴ6·ㄴ7). 누적 SF 갱신."""
    leg = s.leg(block)
    leg.pre_filled += qty
    leg.acc.sf_qty += qty
    leg.acc.sf_px_sum += price * qty
    leg.post_pending += float(qty * HL_PER_SF)
    s.sf_net += qty if block is Block.ENTRY else -qty
    if leg.pre_filled >= leg.pre_qty and leg.pre_qty > 0:
        leg.status = LegStatus.POST_PENDING
    else:
        leg.status = LegStatus.PRE_PARTIAL
    return [Action("place_post", side=leg.post_side, qty=qty * HL_PER_SF)]


def on_pre_cancelled(s: AutoMSet, block: Block, mono: float, settings: AutoMSettings) -> None:
    """선주문 취소 확인 — 재발주 취소면 병행 후주문 확인 뒤 딜레이, 아니면 감시로."""
    leg = s.leg(block)
    if leg.replace_pending:
        leg.replace_pending = False
        if not post_done(leg):
            leg.await_post_then_delay = True  # 헤지 체결(RT 갱신) 확인 뒤 딜레이 → 신규
            leg._clear_pre()
            return
        _start_delay(leg, mono, settings)
        return
    leg._clear_pre()
    if post_done(leg):
        leg.status = LegStatus.ARMED if leg.running else LegStatus.IDLE


def on_post_fill(
    s: AutoMSet, block: Block, hl_qty: float, hl_price: float, fx_quote: float,
    mono: float, settings: AutoMSettings,
) -> list[Action]:
    """후주문(HL) 체결 → RT 증감·누적(§9a)·헤지 완성 판정(exec ㄹ1).

    fx_quote = 체결 시점 원달러선물 호가(진입 −환은 매수1호가, 청산 +환은 매도1호가).
    """
    leg = s.leg(block)
    leg.acc.hl_qty += hl_qty
    leg.acc.hl_px_sum += hl_price * hl_qty
    leg.acc.fx_sum += fx_quote * hl_qty
    leg.post_pending = max(0.0, leg.post_pending - hl_qty)
    # RT(SF 계약)는 HL 체결을 10계약 단위로 환산 — 소수 체결(0.588 등)은 잔여분에 모아 둔다
    s.hl_rt_carry += hl_qty
    sf_contracts = int(s.hl_rt_carry // HL_PER_SF + _EPS)
    s.hl_rt_carry -= sf_contracts * HL_PER_SF
    if block is Block.ENTRY:
        s.rt += sf_contracts
        s.hl_net -= hl_qty
        s.last_entry_fill_mono = mono
    else:
        s.rt = max(0, s.rt - sf_contracts)
        s.hl_net += hl_qty
        s.last_exit_fill_mono = mono
    if not post_done(leg):
        return []
    # 헤지 완성 시점의 체결차 확인(exec ㄹ1) — 이 세트 체결 기준. ≠0이면 중지.
    halted = halt_if_unhedged(s, block, fill_diff(s.sf_net, s.hl_net))
    if halted:
        return halted
    # 이번 판의 헤지 완성 — 남은 선주문 없으면 딜레이, 부분체결 잔량이 남아 있으면 계속 대기
    if leg.await_post_then_delay or leg.pre_order_id is None or leg.pre_filled >= leg.pre_qty:
        leg.await_post_then_delay = False
        _start_delay(leg, mono, settings)
    else:
        leg.status = LegStatus.PRE_PARTIAL
    return []


def _halt_set(s: AutoMSet, block: Block, reason: str) -> list[Action]:
    """세트 중지(exec §2·결정 로그 7·9: 헤지 깨진 **세트**는 멈춤 — 사용자 확인 2026-09-07).

    체결차를 낸 다리뿐 아니라 **다른 다리도** 실행을 끄고 중지로 둔다(청산 체결차인데 진입이 계속
    새 선주문을 내면 안 됨). 다른 다리에 걸린 선주문은 취소. 해제(release_halt)도 세트 단위.
    """
    leg = s.leg(block)
    leg.status = LegStatus.HALTED
    leg.running = False  # 중지 = 실행 꺼짐(버튼 원색). 다시 켜려면 사람이 해제
    leg.halt_reason = reason
    acts: list[Action] = [Action("halt", reason=reason), Action("notify", reason=reason)]
    other = s.leg(Block.EXIT if block is Block.ENTRY else Block.ENTRY)
    if other.status is not LegStatus.HALTED:
        acts += _cancel_if_resting(other)  # 걸린 선주문 취소(취소 확인은 통보로)
        other.status = LegStatus.HALTED
        other.running = False
        other.halt_reason = f"{'청산' if block is Block.EXIT else '진입'} 체결차로 세트 중지"
    return acts


def on_post_reject(s: AutoMSet, block: Block, reason: str = "") -> list[Action]:
    """후주문 거부 → 국내는 체결됐는데 HL 미체결 = 체결차 → 세트 중지 + 알림(ㄹ2, 재시도 없음)."""
    s.fill_diff = round(fill_diff(s.sf_net, s.hl_net), 6)  # 화면 체결차 칸 — 미헤지분(소수)
    return _halt_set(s, block, f"후주문 거부 — 체결차 발생{(': ' + reason) if reason else ''}")


def halt_if_unhedged(s: AutoMSet, block: Block, diff: float) -> list[Action]:
    """체결차 감지(exec ㅂ1) — 후주문 대기분이 없는데 ≠0이면 세트 중지(diff는 코어가 넣는다)."""
    leg = s.leg(block)
    s.fill_diff = round(diff, 6)  # HL 소수 계약 그대로(0.412 등) — 정수로 깎으면 체결차가 사라진다
    if abs(diff) < _EPS or not post_done(leg) or leg.status is LegStatus.HALTED:
        return []
    return _halt_set(s, block, f"체결차 {diff:g} ≠ 0")


def set_running(s: AutoMSet, block: Block, value: bool) -> list[Action]:
    """실행 켬/끔(exec ㅂ2) — 끄면 미체결 선주문 취소(체결 포지션 유지). 중지 상태는 끄기만 허용."""
    leg = s.leg(block)
    leg.running = value
    if value:
        if leg.status is LegStatus.IDLE:
            leg.status = LegStatus.ARMED
        return []
    acts = _cancel_if_resting(leg)
    if leg.pre_order_id is None and post_done(leg) and leg.status is not LegStatus.HALTED:
        leg.status = LegStatus.IDLE
    return acts


def on_post_partial_reject(s: AutoMSet, block: Block, unfilled: float) -> list[Action]:
    """후주문 일부만 체결되고 나머지가 취소/거부됨 — 미체결분(소수 그대로)만큼 체결차 → 중지."""
    leg = s.leg(block)
    leg.post_pending = max(0.0, leg.post_pending - unfilled)
    return on_post_reject(s, block, f"HL {unfilled:g}계약 미체결")


def release_halt(s: AutoMSet, block: Block) -> None:
    """중지 해제 — 사람이 정리한 뒤 직접 푼다(exec §2). **세트 단위**(중지가 세트 단위이므로):
    어느 다리에서 풀든 두 다리 모두 꺼진 대기(idle)로 돌아간다."""
    for leg in (s.entry, s.exit):
        if leg.status is not LegStatus.HALTED:
            continue
        leg.status = LegStatus.IDLE
        leg.running = False
        leg.halt_reason = ""
        leg._clear_pre()
        leg.post_pending = 0.0
        leg.replace_pending = leg.await_post_then_delay = False


# ---------------------------------------------------------- 화면 단위 묶음 ---

SET_COUNT = 3


@dataclass
class AutoMBook:
    """종목 하나의 자동M 상태 — 정방향 3세트 + 기준수량 + 월물(사용자 확정 2026-09-08: 종목별 독립).

    창(order_autom)은 종목 콤보로 어느 책을 보여줄지 고를 뿐이고, 실행은 코어가 종목마다 따로 돈다.
    같은 종목을 두 창에서 열면 같은 책을 함께 보여준다(중복 실행 아님)."""

    sets: list[AutoMSet] = field(default_factory=lambda: [AutoMSet() for _ in range(SET_COUNT)])
    ref_qty: int = 1  # 상단 기준수량(계약) — 모니터 3칸(진입SF·진입S·청산SF) est 계산용
    future_month: str = "near"  # 선물 월물 "near"|"next" (exec §11.9, DESIGN §5.11)

    def any_running(self) -> bool:
        return any(s.entry.running or s.exit.running for s in self.sets)

    @property
    def counterpart(self) -> Instrument:
        return (Instrument.KR_STOCK_FUTURE_NEXT if self.future_month == "next"
                else Instrument.KR_STOCK_FUTURE)


@dataclass
class AutoMScreen:
    """자동M 전체 = 종목별 책(books) + 체결쏴 공통설정 + 리스크방지. core_state.json에 저장.

    복원(autom_from_dict)은 **입력값·RT·누적**만 되살리고 실행 상태(running·status·주문번호)는
    항상 꺼진 채로 시작한다(자동T와 같은 원칙). 설정·리스크방지는 모든 종목 공통(09-08)."""

    books: dict[str, AutoMBook] = field(
        default_factory=lambda: {u.value: AutoMBook() for u in Underlying})
    settings: AutoMSettings = field(default_factory=AutoMSettings)
    # 리스크방지(exec §11.9, 화면 입력 검증용) — 정방향 진입 > en, 청산 < ex, 진입−청산 > gap
    risk_fwd_en: float = 0.0
    risk_fwd_ex: float = 0.005
    risk_fwd_gap: float = 0.001

    def book(self, u: Underlying) -> AutoMBook:
        return self.books.setdefault(u.value, AutoMBook())

    def any_running(self) -> bool:
        return any(b.any_running() for b in self.books.values())

    def running_underlyings(self) -> list[str]:
        return [k for k, b in self.books.items() if b.any_running()]


def _opt_float(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    return float(raw)  # type: ignore[arg-type]


def _book_from_dict(book: AutoMBook, raw: object) -> None:
    """저장 스냅샷의 책 하나(sets·ref_qty·future_month) → AutoMBook. 값 오류는 그 필드만 기본값."""
    if not isinstance(raw, dict):
        return
    try:
        book.ref_qty = int(raw.get("ref_qty", book.ref_qty))
    except (TypeError, ValueError):
        pass
    month = str(raw.get("future_month", book.future_month))
    book.future_month = month if month in ("near", "next") else "near"
    sets = raw.get("sets")
    if isinstance(sets, list):
        for target, rs in zip(book.sets, sets, strict=False):
            if not isinstance(rs, dict):
                continue
            try:
                target.target_qty = int(rs.get("target_qty", target.target_qty))
                target.per_qty = int(rs.get("per_qty", target.per_qty))
                target.switch_delay_s = int(rs.get("switch_delay_s", target.switch_delay_s))
                target.en_sf = _opt_float(rs.get("en_sf"))
                target.en_s = _opt_float(rs.get("en_s"))
                target.ex_sf = _opt_float(rs.get("ex_sf"))
                target.rt = int(rs.get("rt", target.rt))
                target.sf_net = int(rs.get("sf_net", target.sf_net))
                target.hl_net = float(rs.get("hl_net", target.hl_net))
                target.hl_rt_carry = float(rs.get("hl_rt_carry", target.hl_rt_carry))
                target.fill_diff = round(fill_diff(target.sf_net, target.hl_net), 6)
            except (TypeError, ValueError):
                pass
            for name, leg in (("entry", target.entry), ("exit", target.exit)):
                acc = (rs.get(name) or {}).get("acc") if isinstance(rs.get(name), dict) else None
                if isinstance(acc, dict):
                    try:
                        leg.acc.hl_qty = float(acc.get("hl_qty", 0) or 0)
                        leg.acc.hl_px_sum = float(acc.get("hl_px_sum", 0) or 0)
                        leg.acc.fx_sum = float(acc.get("fx_sum", 0) or 0)
                        leg.acc.sf_qty = int(acc.get("sf_qty", 0) or 0)
                        leg.acc.sf_px_sum = float(acc.get("sf_px_sum", 0) or 0)
                    except (TypeError, ValueError):
                        pass


def autom_from_dict(screen: AutoMScreen, raw: object, legacy_underlying: str = "samsung") -> None:
    """저장 스냅샷 → AutoMScreen(입력값·RT·누적만). 값 오류는 그 필드만 기본값.

    새 형식은 ``books: {종목: {sets, ref_qty, future_month}}``. 옛 형식(2026-09-08 이전, 단일
    ``sets``·``ref_qty``)은 그때 화면이 가리키던 종목(legacy_underlying)의 책으로 옮긴다.
    """
    if not isinstance(raw, dict):
        return
    books = raw.get("books")
    if isinstance(books, dict):
        for key, rb in books.items():
            try:
                u = Underlying(str(key))
            except ValueError:
                continue
            _book_from_dict(screen.book(u), rb)
    elif "sets" in raw:  # 옛 단일 형식 → 그 종목 책으로 이전
        try:
            u = Underlying(legacy_underlying)
        except ValueError:
            u = Underlying.SAMSUNG
        _book_from_dict(screen.book(u), raw)
    st = raw.get("settings")
    if isinstance(st, dict):
        s = screen.settings
        try:
            win = st.get("windows")
            if isinstance(win, list) and win:
                parsed = tuple((str(a), str(b)) for a, b in win)
                for a, b in parsed:
                    parse_hms(a)
                    parse_hms(b)
                s.windows = parsed
            ticks = st.get("pre_tick")
            if isinstance(ticks, dict):
                s.pre_tick = {Underlying(str(k)): int(v) for k, v in ticks.items()}
            s.pre_delay_ms = int(st.get("pre_delay_ms", s.pre_delay_ms))
            s.resume_delay_s = int(st.get("resume_delay_s", s.resume_delay_s))
            s.pre_range = float(st.get("pre_range", s.pre_range))
            s.rel_buy = int(st.get("rel_buy", s.rel_buy))
            s.rel_sell = int(st.get("rel_sell", s.rel_sell))
            s.hl_margin_buy = float(st.get("hl_margin_buy", s.hl_margin_buy))
            s.hl_margin_sell = float(st.get("hl_margin_sell", s.hl_margin_sell))
        except (TypeError, ValueError):
            pass
    try:
        screen.risk_fwd_en = float(raw.get("risk_fwd_en", screen.risk_fwd_en))
        screen.risk_fwd_ex = float(raw.get("risk_fwd_ex", screen.risk_fwd_ex))
        screen.risk_fwd_gap = float(raw.get("risk_fwd_gap", screen.risk_fwd_gap))
    except (TypeError, ValueError):
        pass
