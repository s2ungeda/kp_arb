"""자동M(체결쏴) 실행 뼈대 — 순수 상태기계 (DESIGN-auto-m.md §3~§9a, DESIGN-auto-m-exec.md).

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
from .domain.enums import Block, Side, Underlying
from .theory import in_time_window
from .ticks import ceil_to_tick, floor_to_tick

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
    post_pending: int = 0       # 후주문(HL) 체결 확인 대기 계약수
    delay_until: float | None = None
    replace_pending: bool = False   # 역산가 바뀜 → 취소 보냄, 취소 확인 대기
    await_post_then_delay: bool = False  # 취소 확인됨, 병행 후주문 체결 확인 뒤 딜레이
    halt_reason: str = ""
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
    fill_diff: int = 0                      # 체결차 = SF잔고×10 + HL잔고 (이 세트 체결 기준)
    sf_net: int = 0                         # 이 세트가 잡은 SF 순잔고(계약, 매수 +)
    hl_net: float = 0.0                     # 이 세트가 잡은 HL 순잔고(계약, 매도 −)
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


def fill_diff(sf_net_contracts: int, hl_net_contracts: float) -> float:
    """체결차(§8) = SF 잔고 × 10 + HL 잔고(매도 −). 0이면 완전 헤지."""
    return sf_net_contracts * HL_PER_SF + hl_net_contracts


# ---------------------------------------------------------------- 상태기계 ---

def _cancel_if_resting(leg: Leg) -> list[Action]:
    """걸어둔 선주문이 있으면 취소 요청(취소 확인은 on_pre_cancelled)."""
    if leg.pre_order_id is not None and not leg.replace_pending:
        return [Action("cancel_pre", order_id=leg.pre_order_id)]
    return []


def _passes_signal(s: AutoMSet, leg: Leg, sig: Signals) -> bool:
    """G5 판정(§3) — 진입: SF괴리 > 진입SF AND S괴리 > 진입S / 청산: SF괴리 < 청산SF."""
    if leg.block is Block.ENTRY:
        if s.en_sf is None or s.en_s is None:
            return False
        return (sig.sf_spread_entry is not None and sig.s_spread_entry is not None
                and sig.sf_spread_entry > s.en_sf and sig.s_spread_entry > s.en_s)
    if s.ex_sf is None:
        return False
    return sig.sf_spread_exit is not None and sig.sf_spread_exit < s.ex_sf


def _switch_wait(s: AutoMSet, leg: Leg, mono: float) -> bool:
    """G3 전환대기 — 진입은 직전 청산 체결 뒤, 청산은 직전 진입 체결 뒤 N초."""
    last = s.last_exit_fill_mono if leg.block is Block.ENTRY else s.last_entry_fill_mono
    return last is not None and mono - last < s.switch_delay_s


def evaluate(
    s: AutoMSet, block: Block, sig: Signals, settings: AutoMSettings, underlying: Underlying,
) -> list[Action]:
    """시세 1건에 대한 판정(exec §4 G1~G6 + §6 상태 규칙). 반환: 코어가 할 일."""
    leg = s.leg(block)
    if leg.status is LegStatus.HALTED:
        return []
    # G1 실행 꺼짐 → 미체결 취소, 대기
    if not leg.running:
        acts = _cancel_if_resting(leg)
        if leg.post_pending == 0 and leg.pre_order_id is None:
            leg.status = LegStatus.IDLE
        return acts
    if leg.status is LegStatus.IDLE:
        leg.status = LegStatus.ARMED
    # 시장 정지(exec §8) — 신규·정정 중단 + 미체결 취소, HL은 손대지 않음
    if sig.market_halted:
        return _cancel_if_resting(leg)
    if sig.resumed_mono is not None and sig.mono - sig.resumed_mono < settings.resume_delay_s:
        return []  # 재개 딜레이
    # 사건 대기 중인 상태는 시세로 바꾸지 않는다
    if leg.status is LegStatus.POST_PENDING or leg.replace_pending or leg.await_post_then_delay:
        return []
    if leg.status is LegStatus.SETTLE_DELAY:
        if leg.delay_until is not None and sig.mono < leg.delay_until:
            return []
        leg.delay_until = None
        leg.status = LegStatus.ARMED
    # G2 주문가능시간
    if not settings.in_window(sig.now.time()):
        acts = _cancel_if_resting(leg)
        return acts
    # G3 전환대기 · G4 여유 계약수 — 새로 내지 않음(걸어둔 것은 유지)
    if _switch_wait(s, leg, sig.mono):
        return []
    qty = order_qty(block, s.per_qty, s.target_qty, s.rt)
    if qty < 1 and leg.pre_order_id is None:
        return []
    # G5 판정
    if not _passes_signal(s, leg, sig):
        return _cancel_if_resting(leg)
    # G6 역산가 → 허용범위
    thr = s.threshold(block)
    hl_disp = sig.hl_disp_bid if block is Block.ENTRY else sig.hl_disp_ask
    tick = settings.pre_tick.get(underlying)
    if sig.sf_theory is None or hl_disp is None or thr is None or not tick:
        return []
    price = pre_order_price(block, sig.sf_theory, hl_disp, thr, tick)
    side = leg.pre_side
    rel = (rel_quote(sig.sf_asks, settings.rel_buy) if side is Side.BUY
           else rel_quote(sig.sf_bids, settings.rel_sell))
    if rel is None:
        return []
    if not within_limit(side, price, limit_price(side, rel, tick, settings.pre_range)):
        return _cancel_if_resting(leg)
    # 통과 — 없으면 발주, 있고 역산가가 바뀌었으면 재발주 규칙(취소→후주문 확인→딜레이→신규)
    if leg.pre_order_id is None and leg.status is LegStatus.ARMED:
        if qty < 1:
            return []
        leg.pre_price, leg.pre_qty, leg.pre_filled = price, qty, 0
        leg.status = LegStatus.PRE_RESTING
        return [Action("place_pre", side=side, qty=qty, price=price)]
    if leg.pre_order_id is not None and leg.pre_price != price:
        leg.replace_pending = True
        return [Action("cancel_pre", order_id=leg.pre_order_id,
                       reason=f"역산가 변경 {leg.pre_price:g}→{price:g}")]
    return []


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
    leg.post_pending += qty * HL_PER_SF
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
        if leg.post_pending > 0:
            leg.await_post_then_delay = True  # 헤지 체결(RT 갱신) 확인 뒤 딜레이 → 신규
            leg._clear_pre()
            return
        _start_delay(leg, mono, settings)
        return
    leg._clear_pre()
    if leg.post_pending == 0:
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
    leg.post_pending = max(0, leg.post_pending - int(round(hl_qty)))
    sf_contracts = int(round(hl_qty / HL_PER_SF))
    if block is Block.ENTRY:
        s.rt += sf_contracts
        s.hl_net -= hl_qty
        s.last_entry_fill_mono = mono
    else:
        s.rt = max(0, s.rt - sf_contracts)
        s.hl_net += hl_qty
        s.last_exit_fill_mono = mono
    if leg.post_pending > 0:
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


def on_post_reject(s: AutoMSet, block: Block, reason: str = "") -> list[Action]:
    """후주문 거부 → 국내는 체결됐는데 HL 미체결 = 체결차 → 중지 + 알림(exec ㄹ2, 재시도 없음)."""
    leg = s.leg(block)
    leg.status = LegStatus.HALTED
    leg.halt_reason = f"후주문 거부 — 체결차 발생{(': ' + reason) if reason else ''}"
    return [Action("halt", reason=leg.halt_reason), Action("notify", reason=leg.halt_reason)]


def halt_if_unhedged(s: AutoMSet, block: Block, diff: float) -> list[Action]:
    """체결차 감지(exec ㅂ1) — 후주문 대기분이 없는데 ≠0이면 중지. 코어가 잔고로 diff를 넣는다."""
    leg = s.leg(block)
    s.fill_diff = int(diff)
    if diff == 0 or leg.post_pending > 0 or leg.status is LegStatus.HALTED:
        return []
    leg.status = LegStatus.HALTED
    leg.halt_reason = f"체결차 {diff:g} ≠ 0"
    return [Action("halt", reason=leg.halt_reason), Action("notify", reason=leg.halt_reason)]


def set_running(s: AutoMSet, block: Block, value: bool) -> list[Action]:
    """실행 켬/끔(exec ㅂ2) — 끄면 미체결 선주문 취소(체결 포지션 유지). 중지 상태는 끄기만 허용."""
    leg = s.leg(block)
    leg.running = value
    if value:
        if leg.status is LegStatus.IDLE:
            leg.status = LegStatus.ARMED
        return []
    acts = _cancel_if_resting(leg)
    if leg.pre_order_id is None and leg.post_pending == 0 and leg.status is not LegStatus.HALTED:
        leg.status = LegStatus.IDLE
    return acts


def on_post_partial_reject(s: AutoMSet, block: Block, unfilled: int) -> list[Action]:
    """후주문 일부만 체결되고 나머지 거부/취소(IOC 잔량) — 미체결분만큼 체결차 → 중지."""
    leg = s.leg(block)
    leg.post_pending = max(0, leg.post_pending - unfilled)
    return on_post_reject(s, block, f"HL {unfilled}계약 미체결")


def release_halt(s: AutoMSet, block: Block) -> None:
    """중지 해제 — 사람이 정리한 뒤 직접 푼다(exec §2). 실행은 꺼진 대기로 돌아간다."""
    leg = s.leg(block)
    leg.status = LegStatus.IDLE
    leg.running = False
    leg.halt_reason = ""
    leg._clear_pre()
    leg.post_pending = 0
    leg.replace_pending = leg.await_post_then_delay = False


# ---------------------------------------------------------- 화면 단위 묶음 ---

SET_COUNT = 3


@dataclass
class AutoMScreen:
    """자동M 화면 전체(정방향 3세트 + 체결쏴 공통설정 + 리스크방지). core_state.json에 저장.

    복원(autom_from_dict)은 **입력값·RT·누적**만 되살리고 실행 상태(running·status·주문번호)는
    항상 꺼진 채로 시작한다(자동T와 같은 원칙)."""

    sets: list[AutoMSet] = field(default_factory=lambda: [AutoMSet() for _ in range(SET_COUNT)])
    settings: AutoMSettings = field(default_factory=AutoMSettings)
    ref_qty: int = 1  # 상단 기준수량(계약) — 모니터 3칸(진입SF·진입S·청산SF) est 계산용
    # 리스크방지(DESIGN-auto-m §10, 화면 입력 검증용) — 정방향 진입 > en, 청산 < ex, 진입−청산 > gap
    risk_fwd_en: float = 0.0
    risk_fwd_ex: float = 0.005
    risk_fwd_gap: float = 0.001

    def any_running(self) -> bool:
        return any(s.entry.running or s.exit.running for s in self.sets)


def _opt_float(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    return float(raw)  # type: ignore[arg-type]


def autom_from_dict(screen: AutoMScreen, raw: object) -> None:
    """저장 스냅샷 → AutoMScreen(입력값·RT·누적만). 값 오류는 그 필드만 기본값."""
    if not isinstance(raw, dict):
        return
    sets = raw.get("sets")
    if isinstance(sets, list):
        for target, rs in zip(screen.sets, sets, strict=False):
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
        except (TypeError, ValueError):
            pass
    try:
        screen.ref_qty = int(raw.get("ref_qty", screen.ref_qty))
        screen.risk_fwd_en = float(raw.get("risk_fwd_en", screen.risk_fwd_en))
        screen.risk_fwd_ex = float(raw.get("risk_fwd_ex", screen.risk_fwd_ex))
        screen.risk_fwd_gap = float(raw.get("risk_fwd_gap", screen.risk_fwd_gap))
    except (TypeError, ValueError):
        pass
