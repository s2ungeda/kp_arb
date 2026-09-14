"""자동M(체결쏴) 실행 뼈대 — 순수 상태변화 (DESIGN-auto-m-exec.md — 자동M 단일 스펙, §11 전략·화면).

I/O 없음. 코어(결선 단계)가 시세마다 ``Signals``를 넣고 ``evaluate``를 부르고, 주문 사건이 오면
``on_*`` 를 부른다. 여기서 나온 ``Action`` 목록을 코어가 실제 발주/취소로 옮긴다.

범위: **정방향·역방향 진입·청산**(선주문 = 국내 SF maker, 후주문 = HL taker, 후주문 수량 = SF 체결
× 10). 역방향(exec §7A·§7B, 2026-09-14)은 세트의 ``reverse`` 값 하나로 뒤집힌다 — 선·후주문 방향,
판정 부등호, 역산 호가창·반올림·한계, RT·SF·HL 부호. 다른 전략도 이 뼈대를 공유한다(exec §0).

상태(exec §2): IDLE 대기 · ARMED 감시 · PRE_RESTING 선주문대기 · PRE_PARTIAL 부분체결 ·
POST_PENDING 후주문대기 · SETTLE_DELAY 딜레이대기 · HALTED 중지(사람이 풀어야 재개).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from enum import StrEnum
from typing import Any

from .disparity import maker_price_for_spread
from .domain.enums import Block, Instrument, Side, Underlying
from .theory import in_time_window
from .ticks import ceil_to_tick, floor_to_tick, tick_for

HL_PER_SF = 10  # SF 1계약 = HL 10계약 (§1)
CANCEL_CONFIRM_S = 3.0   # 취소 보낸 뒤 확인(취소·체결·거부) 기다리는 시간 — 지나면 재전송(exec ㅂ3)
CANCEL_ALARM_TRIES = 3   # 취소 전송이 이 횟수를 넘으면 에러 알람 + "취소실패" 표시(exec ㅂ3)

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
    s_spread_exit: float | None = None  # 청산 쪽 S괴리(HL 매도호가창 est) — 역방향 진입 G5(§7A)
    # HL 호가 원값(USD) — 발주 근거 로그용(사용자 2026-09-14): 1호가와 후주문 수량 est 평균가
    hl_bid1: float | None = None
    hl_ask1: float | None = None
    hl_est_bid: float | None = None   # 매수호가창을 후주문 수량만큼 쓸어담은 평균가(HL 매도 쪽)
    hl_est_ask: float | None = None   # 매도호가창 est(HL 매수 쪽)
    market_halted: bool = False       # 선물시장 정지 오버레이(exec §8)
    resumed_mono: float | None = None  # 정지가 풀린 시각(재개 딜레이)
    fx: float | None = None           # HL 환산 환율(역산가의 HL괴리에 쓰인 값) — 로그용


# --------------------------------------------------------------- 행동(출력) ---

@dataclass(frozen=True)
class Action:
    """코어가 실행할 일. kind: place_pre | cancel_pre | place_post | halt | notify | alarm.

    alarm = 중지는 아니지만 사람이 봐야 하는 일(취소실패 등) — 에러 알람 소리만, 상태는 그대로.
    """

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
    sf_qty: float = 0.0      # SF 체결 계약수(중지 때 짝 맞은 몫만 넣으면 소수 가능)
    sf_px_sum: float = 0.0
    # Sprd 기준값도 **후주문 체결 시점** 값(사용자 확정 2026-09-10 — 실시간을 쓰면 판이 끝나도
    # 값이 계속 움직임): S현재가·SF이론가 × HL 체결수량 합. 없으면(시세 미수신) 0 → Sprd 계산 불가.
    s_px_sum: float = 0.0
    theory_sum: float = 0.0
    ref_qty: float = 0.0     # 위 두 기준값이 기록된 HL 수량(옛 누적·시세 없던 체결 제외) — 분모

    def add_round(self, pending: Accum, matched_only: bool = False) -> None:
        """한 판(pending)을 누적에 합친다 — 후주문 전량 체결이 확인된 뒤에만(사용자 확정 2026-09-08,
        부분값 표시 없음). matched_only=True(중지로 판이 끝남): 짝이 맞은 적은 쪽 몫만 넣는다."""
        if not matched_only:
            self.hl_qty += pending.hl_qty
            self.hl_px_sum += pending.hl_px_sum
            self.fx_sum += pending.fx_sum
            self.s_px_sum += pending.s_px_sum
            self.theory_sum += pending.theory_sum
            self.ref_qty += pending.ref_qty
            self.sf_qty += pending.sf_qty
            self.sf_px_sum += pending.sf_px_sum
            return
        hl_take = pending.matched_hl()
        if hl_take <= 0:
            return
        sf_take = hl_take / HL_PER_SF
        hl_avg, fx_avg, sf_avg = pending.hl_avg(), pending.fx_avg(), pending.sf_avg()
        self.hl_qty += hl_take
        self.hl_px_sum += (hl_avg or 0.0) * hl_take
        self.fx_sum += (fx_avg or 0.0) * hl_take
        if pending.ref_qty > 0:  # 기준값이 있던 판만 — 짝 맞은 몫만큼
            self.s_px_sum += (pending.s_avg() or 0.0) * hl_take
            self.theory_sum += (pending.theory_avg() or 0.0) * hl_take
            self.ref_qty += hl_take
        self.sf_qty += sf_take
        self.sf_px_sum += (sf_avg or 0.0) * sf_take

    def hl_avg(self) -> float | None:
        return self.hl_px_sum / self.hl_qty if self.hl_qty > 0 else None

    def fx_avg(self) -> float | None:
        return self.fx_sum / self.hl_qty if self.hl_qty > 0 else None

    def s_avg(self) -> float | None:
        """후주문 체결 시점 S현재가의 HL 수량 가중평균(기록된 체결이 없으면 None)."""
        return self.s_px_sum / self.ref_qty if self.ref_qty > 0 else None

    def theory_avg(self) -> float | None:
        """후주문 체결 시점 SF이론가의 HL 수량 가중평균(기록된 체결이 없으면 None)."""
        return self.theory_sum / self.ref_qty if self.ref_qty > 0 else None

    def sf_avg(self) -> float | None:
        return self.sf_px_sum / self.sf_qty if self.sf_qty > 0 else None

    def matched_hl(self) -> float:
        """짝이 맞은 체결량(HL 계약) — LS·HL 누적 체결량이 다르면 **적은 쪽** 기준(사용자 확정
        2026-09-08; 보통 HL이 적다 — 부분 체결·거부). SF 1계약 = HL 10계약으로 맞춰 비교."""
        return min(self.hl_qty, self.sf_qty * HL_PER_SF)

    def matched_sf(self) -> float:
        """짝이 맞은 체결량(SF 계약, 소수 가능 — HL 0.588 체결이면 0.0588)."""
        return self.matched_hl() / HL_PER_SF

    def sprd(self) -> float | None:
        """Sprd = (환×HL평균가 − S현재가)/S현재가 − (SF평균가 − SF이론가)/SF이론가 (엑셀 메인 I25).

        네 값 모두 **후주문 체결 시점** 값의 HL 수량 가중평균(사용자 확정 2026-09-10) — 판이 끝나면
        고정된다. (09-04의 "S현재가·SF이론가는 실시간" 결정은 폐기: 시세 따라 계속 바뀌어 매매결과로
        쓸 수 없었음.)"""
        fx, hl, sf = self.fx_avg(), self.hl_avg(), self.sf_avg()
        stock, theory = self.s_avg(), self.theory_avg()
        if None in (fx, hl, sf, stock, theory):
            return None
        assert fx is not None and hl is not None and sf is not None
        assert stock is not None and theory is not None
        return (fx * hl - stock) / stock - (sf - theory) / theory

    def clear(self) -> None:
        self.hl_qty = self.hl_px_sum = self.fx_sum = self.sf_px_sum = 0.0
        self.s_px_sum = self.theory_sum = self.ref_qty = 0.0
        self.sf_qty = 0.0


@dataclass
class Leg:
    """진입 또는 청산 한 줄의 실행 상태(세트마다 진입·청산 각 1개)."""

    block: Block
    reverse: bool = False       # 역방향 세트의 줄(AutoMSet.__post_init__가 맞춤) — 선·후주문 방향
    running: bool = False
    status: LegStatus = LegStatus.IDLE
    pre_order_id: str | None = None
    pre_price: float | None = None
    # 선주문 발주 시점의 HL est(후주문 방향, 후주문 수량만큼 쓸어담은 평균 예상가) — 후주문 체결가와
    # 비교해 "판정 때 본 값대로 잡혔나"를 본다(사용자 2026-09-14). 재발주 때 새 값으로 덮인다.
    pre_est: float | None = None
    pre_qty: int = 0            # 이번 선주문 계약수
    pre_filled: int = 0         # 이번 선주문 체결 계약수
    post_pending: float = 0.0   # 후주문(HL) 체결 대기 계약수 — HL은 소수 체결(0.588 등, 실측 09-07)
    delay_until: float | None = None
    replace_pending: bool = False   # 역산가 바뀜 → 취소 보냄, 취소 확인 대기
    cancel_sent: bool = False       # 관문(G2·G5·G6) 취소를 이미 보냄 — 확인 올 때까지 재전송 안 함
    cancel_sent_mono: float | None = None  # 마지막 취소 전송 시각 — 3초 확인 없으면 재전송(ㅂ3)
    cancel_tries: int = 0           # 이번 선주문에 취소를 보낸 횟수(재전송 포함)
    cancel_alarmed: bool = False    # 취소 재전송 한도 초과 알람을 이미 냈음 → 상태줄 "취소실패"
    await_post_then_delay: bool = False  # 취소 확인됨, 병행 후주문 체결 확인 뒤 딜레이
    reject_streak: int = 0          # 선주문 연속 거부 횟수 — 접수 뒤 체결·취소가 있으면 0으로
    halt_reason: str = ""
    # 마지막 판정 결과 한 줄(어느 게이트에서 막혔나·통과했나 + 숫자) — 로그는 바뀔 때만 남긴다
    block_reason: str = ""
    acc: Accum = field(default_factory=Accum)      # 매매결과 누적 — 후주문 전량 체결 확인된 판만
    pending: Accum = field(default_factory=Accum)  # 진행 중인 한 판(SF·HL 체결 버퍼) — 표시 안 함

    @property
    def pre_side(self) -> Side:
        """선주문(SF) 방향 — 정방향 진입·역방향 청산 = 매수, 정방향 청산·역방향 진입 = 매도."""
        buy = (self.block is Block.ENTRY) != self.reverse
        return Side.BUY if buy else Side.SELL

    @property
    def post_side(self) -> Side:
        """후주문(HL) 방향 — 선주문의 반대."""
        return Side.SELL if self.pre_side is Side.BUY else Side.BUY

    def _clear_pre(self) -> None:
        self.pre_order_id = self.pre_price = None
        self.pre_qty = self.pre_filled = 0
        self.cancel_sent = False
        self.cancel_sent_mono = None
        self.cancel_tries = 0
        self.cancel_alarmed = False
        # 선주문이 끝났으면(체결·취소·거부) 재발주 취소 대기도 끝 — 역산가 변경으로 취소를 보냈는데
        # 취소보다 체결이 먼저 된 경우(LS 01433) 이 표시가 남아 판이 끝나도 '쉼'에서 못 나왔다
        # (실측 2026-09-11 오후: 1·2세트 청산이 딜레이대기에 굳음).
        self.replace_pending = False


@dataclass
class AutoMSet:
    """세트 1개 — 세트 설정 + RT·체결차 + 진입/청산 실행 상태. ``reverse``면 역방향(§7A·§7B)."""

    target_qty: int = 0
    per_qty: int = 0
    switch_delay_s: int = 0                 # 전환딜레이(초)
    en_sf: float | None = None              # 진입 SF 기준값(소수, 0.005 = 0.5%)
    en_s: float | None = None               # 진입 S 기준값
    ex_sf: float | None = None              # 청산 SF 기준값
    # RT선진입(계약) — 선주문(SF) 체결 계약수. 정방향 = SF 매수 체결로 +, 역방향 = SF 매도 체결로
    # −(0 또는 음수, 화면도 −값 그대로 — 사용자 확정 2026-09-14)
    rt: int = 0
    fill_diff: float = 0.0                  # 체결차 = SF잔고×10 + HL잔고 (이 세트 체결 기준, 소수)
    sf_net: int = 0                         # 이 세트가 잡은 SF 순잔고(계약, 매수 +)
    hl_net: float = 0.0                     # 이 세트가 잡은 HL 순잔고(계약, 매도 −)
    reverse: bool = False                   # 역방향 세트(진입 = SF 매도 + HL 매수)
    entry: Leg = field(default_factory=lambda: Leg(Block.ENTRY))
    exit: Leg = field(default_factory=lambda: Leg(Block.EXIT))
    last_entry_fill_mono: float | None = None
    last_exit_fill_mono: float | None = None

    def __post_init__(self) -> None:
        self.entry.reverse = self.exit.reverse = self.reverse

    @property
    def held(self) -> int:
        """들고 있는 선주문 포지션 계약수(항상 0 이상) — 정방향 RT, 역방향 RT×−1(§7A G4)."""
        return -self.rt if self.reverse else self.rt

    def leg(self, block: Block) -> Leg:
        return self.entry if block is Block.ENTRY else self.exit

    def threshold(self, block: Block) -> float | None:
        return self.en_sf if block is Block.ENTRY else self.ex_sf


# ---------------------------------------------------------------- 순수 계산 ---

def order_qty(block: Block, per_qty: int, target_qty: int, rt: int,
              reverse: bool = False) -> int:
    """이번에 낼 계약수(§5) — 진입 Min(1회주문, 목표−들고 있는 양) / 청산 Min(1회주문, 들고 있는
    양). 들고 있는 양 = 정방향 RT, 역방향 RT×−1(§7A·§7B, 사용자 확정 2026-09-14). 0 이하면 0."""
    held = -rt if reverse else rt
    room = target_qty - held if block is Block.ENTRY else held
    return max(0, min(per_qty, room))


def rel_quote(levels: Levels, n: int) -> float | None:
    """상대 N호가 = 호가창의 실제 N번째 호가(빈 단계 건너뜀, §6.3). 없으면 None."""
    prices = [px for px, _q in levels if px and px > 0]
    return prices[n - 1] if 0 < n <= len(prices) else None


def range_start(side: Side, rel_px: float, tick: int) -> float:
    """발주 허용범위의 시작호가(§6.3) — 매수 = 상대매도N호가 − 1틱 / 매도 = 상대매수N호가 + 1틱.
    한계는 여기서 범위(%)만큼 더 물러난 값. 로그에 한계와 함께 남긴다(사용자 2026-09-11)."""
    return rel_px - tick if side is Side.BUY else rel_px + tick


def limit_price(side: Side, rel_px: float, tick: int, rng: float) -> float:
    """발주 허용 한계(§6.3).

    매수 = (상대매도N호가 − 1틱) × (1 − 범위) / 매도 = (상대매수N호가 + 1틱) × (1 + 범위).
    """
    start = range_start(side, rel_px, tick)
    return start * (1.0 - rng) if side is Side.BUY else start * (1.0 + rng)


def pre_order_price(
    side: Side, sf_theory: float, hl_disp: float, threshold: float, tick: int,
) -> float:
    """역산가(§6.1) P = 이론가 × (1 + HL_est괴리 − 기준값) → 주문단위(§6.2: 매수 내림/매도 올림).
    side = 선주문(SF) 방향(정방향 진입·역방향 청산 = 매수, 정방향 청산·역방향 진입 = 매도)."""
    raw = maker_price_for_spread(sf_theory, hl_disp, threshold)
    return floor_to_tick(raw, tick) if side is Side.BUY else ceil_to_tick(raw, tick)


def within_limit(side: Side, price: float, limit: float) -> bool:
    """매수는 한계 이상, 매도는 한계 이하여야 발주(§6.3)."""
    return price >= limit if side is Side.BUY else price <= limit


_EPS = 1e-9  # HL 소수 계약 비교용(0.588 같은 체결이 오므로 "== 0" 대신 사용)


def post_done(leg: Leg) -> bool:
    """후주문 대기분이 없는가(소수 오차 허용)."""
    return leg.post_pending <= _EPS


def set_post_done(s: AutoMSet) -> bool:
    """세트의 진입·청산 **둘 다** 후주문 대기가 없는가 — 체결차 판정 시점(실측 2026-09-11 14:41:
    진입·청산을 같이 돌릴 때 진입 후주문이 먼저 잡히자 청산 후주문 10이 아직 대기 중인데 세트 장부로
    판정해 체결차 −10 → 중지. 판정 대상이 세트 장부이니 시점도 세트 전체 대기 0이어야 한다)."""
    return post_done(s.entry) and post_done(s.exit)


def fill_diff(sf_net_contracts: int, hl_net_contracts: float) -> float:
    """체결차(§8) = SF 잔고 × 10 + HL 잔고(매도 −). 0이면 완전 헤지."""
    return sf_net_contracts * HL_PER_SF + hl_net_contracts


# ---------------------------------------------------------------- 상태변화 ---

def _cancel_if_resting(leg: Leg, force: bool = False, mono: float | None = None) -> list[Action]:
    """걸어둔 선주문이 있으면 취소 요청(취소 확인은 on_pre_cancelled).

    전부 체결된 선주문(pre_filled ≥ pre_qty)은 취소할 잔량이 없다 — 보내면 LS가 거부한다
    (실측 2026-09-07 "정정/취소할 수량이 없습니다", 중지 뒤 실행 끔에서).
    force=True(실행 끔·정지·종료): 재발주 취소 대기(replace_pending)·이미 보냄(cancel_sent)
    표시와 무관하게 다시 보낸다 — 앞 취소가 LS 한도에 걸려 실패했을 수 있다(실측 2026-09-08:
    종료 때 취소를 건너뛰어 선주문이 LS에 남음).
    확인 타임아웃(exec ㅂ3, 2026-09-09): 보낸 지 CANCEL_CONFIRM_S가 지나도 확인이 없으면 "보냄"
    표시를 풀고 다시 보낸다 — 표시가 영원히 남아 주문이 LS에 걸린 채 방치되는 것을 막는다.
    재전송이 CANCEL_ALARM_TRIES를 넘으면 한 번 alarm(에러 소리 + 상태줄 "취소실패")을 낸다.
    """
    if force:
        leg.replace_pending = False
    resting = leg.pre_order_id is not None and not leg.replace_pending
    if not resting or (leg.pre_qty > 0 and leg.pre_filled >= leg.pre_qty):
        return []
    if leg.cancel_sent and leg.cancel_sent_mono is None and mono is not None:
        leg.cancel_sent_mono = mono  # 시각 없이 보낸 취소(접수 때 등)는 지금부터 확인을 기다린다
    timed_out = (leg.cancel_sent and mono is not None and leg.cancel_sent_mono is not None
                 and mono - leg.cancel_sent_mono >= CANCEL_CONFIRM_S)
    if not (force or not leg.cancel_sent or timed_out):
        return []
    # 한 번만 보낸다 — 관문에 막힌 채 매 틱 재전송하면 취소 확인이 오기 전 같은 요청이 여러 번
    # 나간다(실측 2026-09-08: 0.2초에 3번). 확인(취소/거부/체결)이 오면 _clear_pre가 되돌린다.
    leg.cancel_sent = True
    leg.cancel_sent_mono = mono
    leg.cancel_tries += 1
    reason = ""
    if timed_out:
        reason = f"취소 확인 없음 {CANCEL_CONFIRM_S:g}초 → 재전송 {leg.cancel_tries}회"
    acts = [Action("cancel_pre", order_id=leg.pre_order_id, reason=reason)]
    if leg.cancel_tries > CANCEL_ALARM_TRIES and not leg.cancel_alarmed:
        leg.cancel_alarmed = True
        acts.append(Action("alarm", order_id=leg.pre_order_id,
                           reason=f"선주문 #{leg.pre_order_id} 취소 {leg.cancel_tries}회째 실패 — "
                                  "수동 취소 확인 필요"))
    return acts


def _passes_signal(s: AutoMSet, leg: Leg, sig: Signals) -> bool:
    """G5 판정(exec §11.3, 정정 2026-09-07) — 진입: **S(현물)괴리 > 진입S만** / 청산: **비교 없음**.

    SF 기준값(진입SF·청산SF)은 판정이 아니라 **역산가(G6)** 에 쓴다 — 선주문은 그 기준값이
    체결로 보장되는 가격(maker)에 걸므로 SF 괴리를 따로 비교할 이유가 없다. S 괴리는
    진입 허용 조건으로만 본다(실제 매매는 SF+HL).
    """
    if leg.block is Block.ENTRY:
        if s.en_sf is None or s.en_s is None:
            return False
        if s.reverse:  # 역방향 진입(§7A G5): 매도호가창 est 기준 S괴리 < +HP/-S
            return sig.s_spread_exit is not None and sig.s_spread_exit < s.en_s
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

    def won(v: float | None) -> str:
        return f"{v:,.0f}" if v is not None else "-"

    if leg.status is LegStatus.HALTED:
        # 중지 뒤에도 걸린 선주문의 취소 확인은 지켜본다(안 오면 재전송, exec ㅂ3)
        return hold("중지", _cancel_if_resting(leg, mono=sig.mono))
    # G1 실행 꺼짐 → 미체결 취소, 대기
    if not leg.running:
        acts = _cancel_if_resting(leg, mono=sig.mono)
        if post_done(leg) and leg.pre_order_id is None:
            leg.status = LegStatus.IDLE
        return hold("G1 실행 꺼짐", acts)
    if leg.status is LegStatus.IDLE:
        leg.status = LegStatus.ARMED
    # 시장 정지(exec §8) — 신규·정정 중단 + 미체결 취소, HL은 손대지 않음
    if sig.market_halted:
        return hold("시장 정지 — 신규·정정 중단", _cancel_if_resting(leg, mono=sig.mono))
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
        return hold("G2 주문가능시간 밖", _cancel_if_resting(leg, mono=sig.mono))
    # G3 전환대기 · G4 여유 계약수 — 새로 내지 않음(걸어둔 것은 유지)
    if _switch_wait(s, leg, sig.mono):
        return hold(f"G3 전환대기 {s.switch_delay_s}초")
    qty = order_qty(block, s.per_qty, s.target_qty, s.rt, s.reverse)
    if qty < 1 and leg.pre_order_id is None:
        return hold(f"G4 여유 없음 (목표 {s.target_qty} RT {s.rt} 1회 {s.per_qty})")
    # G5 판정
    if not _passes_signal(s, leg, sig):
        if block is Block.ENTRY:
            # 실시간 괴리값을 넣지 않는다 — 틱마다 "바뀐 근거"가 되어 줄이 쌓임(실측 09-10).
            # 이 근거는 파일 로그에도 안 남긴다(엔진 _trace가 "G5 미달" 건너뜀, 사용자 09-10).
            why = f"G5 미달 S괴리 < 진입S {pct(s.en_s)}"
        else:
            why = "G5 청산 기준값 없음"
        return hold(why, _cancel_if_resting(leg, mono=sig.mono))
    # G6 역산가 → 허용범위
    thr = s.threshold(block)
    # HL est 호가창은 후주문 방향으로: HL 매도(정방향 진입·역방향 청산) = 매수호가창, HL 매수 =
    # 매도호가창
    side = leg.pre_side
    hl_disp = sig.hl_disp_bid if leg.post_side is Side.SELL else sig.hl_disp_ask
    tick = settings.pre_tick.get(underlying)
    if sig.sf_theory is None or hl_disp is None or thr is None or not tick:
        return hold(f"G6 입력 없음 (이론가 {sig.sf_theory} HL괴리 {pct(hl_disp)} 틱 {tick})")
    price = pre_order_price(side, sig.sf_theory, hl_disp, thr, tick)
    rel = (rel_quote(sig.sf_asks, settings.rel_buy) if side is Side.BUY
           else rel_quote(sig.sf_bids, settings.rel_sell))
    if rel is None:
        return hold("G6 SF 호가 없음")
    # 한계의 "상대N호가 ∓ 1틱"에서 1틱은 **시세(호가창)의 호가단위** = 한 호가 옆(사용자 확정
    # 2026-09-08). 선주문 주문단위(settings.pre_tick)는 역산가를 주문 단위로 맞추는 데만 쓴다.
    mkt_tick = tick_for(Instrument.KR_STOCK_FUTURE, rel)
    start = range_start(side, rel, mkt_tick)
    limit = limit_price(side, rel, mkt_tick, settings.pre_range)
    # 범위 = 시작호가(상대N호가 ∓ 1틱)부터 한계까지 — 둘 다 로그에(사용자 2026-09-11)
    rng_txt = f"범위 {start:,.0f}~{limit:,.0f}(호가단위 {mkt_tick})"
    if not within_limit(side, price, limit):
        return hold(f"G6 범위 밖 역산가 {price:,.0f} {rng_txt} 상대호가 {rel:,.0f}",
                    _cancel_if_resting(leg, mono=sig.mono))
    # 발주 근거엔 그때의 SF 1호가·환율도 남긴다(사용자 2026-09-11) — 나중에 역산을 되짚을 수 있게
    bid1 = sig.sf_bids[0][0] if sig.sf_bids else None
    ask1 = sig.sf_asks[0][0] if sig.sf_asks else None
    fx_txt = f"{sig.fx:,.2f}" if sig.fx else "-"

    def usd(v: float | None) -> str:
        return f"{v:g}" if v is not None else "-"

    # HL 쪽도 남긴다(사용자 2026-09-14): 1호가와 후주문 수량만큼 쓸어담은 est 평균가(후주문 방향)
    hl_est = sig.hl_est_bid if leg.post_side is Side.SELL else sig.hl_est_ask
    basis = (f"역산가 {price:,.0f} = 이론가 {sig.sf_theory:,.0f}×(1+{pct(hl_disp)}−{pct(thr)}) "
             f"주문단위 {tick} {rng_txt} 매수1 {won(bid1)} 매도1 {won(ask1)} 환율 {fx_txt} "
             f"HL 매수1 {usd(sig.hl_bid1)} 매도1 {usd(sig.hl_ask1)} est {usd(hl_est)}")
    # 통과 — 없으면 발주, 있고 역산가가 바뀌었으면 재발주 규칙(취소→후주문 확인→딜레이→신규)
    if leg.pre_order_id is None and leg.status is LegStatus.ARMED:
        if qty < 1:
            return hold(f"G4 여유 없음 (목표 {s.target_qty} RT {s.rt})")
        leg.pre_price, leg.pre_qty, leg.pre_filled = price, qty, 0
        leg.pre_est = hl_est  # 후주문 체결가 비교 기준(발주 시점 est)
        leg.cancel_sent = False
        leg.status = LegStatus.PRE_RESTING
        return hold(f"통과 → 선주문 {qty}계약 {basis}",
                    [Action("place_pre", side=side, qty=qty, price=price)])
    if leg.pre_order_id is not None and leg.pre_price != price:
        leg.replace_pending = True
        return hold(f"역산가 변경 {leg.pre_price:,.0f}→{price:,.0f} → 취소 후 재발주 ({basis})",
                    [Action("cancel_pre", order_id=leg.pre_order_id,
                            reason=f"역산가 변경 {leg.pre_price:,.0f}→{price:,.0f}")])
    # '유지'는 역산가·한계가 바뀔 때만 새 근거가 되게 짧게 — 이론가·괴리까지 넣으면 매 틱 바뀌어
    # 분당 170줄이 쌓였다(실측 2026-09-08). 상세 근거는 '통과'·'역산가 변경' 줄에 남는다.
    return hold(f"유지 역산가 {price:,.0f} 범위 {start:,.0f}~{limit:,.0f}")


# ------------------------------------------------------------ 주문 사건 처리 ---

def _start_delay(leg: Leg, mono: float, settings: AutoMSettings) -> None:
    leg._clear_pre()
    leg.delay_until = mono + settings.pre_delay_ms / 1000.0
    leg.status = LegStatus.SETTLE_DELAY


def on_pre_ack(s: AutoMSet, block: Block, order_id: str,
               mono: float | None = None) -> list[Action]:
    """선주문 접수 — 주문번호 보관(취소·체결 매칭용).

    발주 요청과 접수 응답 사이에 실행이 꺼지거나(끔·정지·종료) 중지되면 취소할 번호가 없어
    취소가 빠진다(실측 2026-09-09: #13865가 LS에 남음). 접수 때 그 진입/청산이 이미 꺼져
    있으면 그 자리에서 취소 행동을 돌려준다.
    """
    leg = s.leg(block)
    leg.pre_order_id = order_id
    if not leg.running or leg.status in (LegStatus.IDLE, LegStatus.HALTED):
        return _cancel_if_resting(leg, force=True, mono=mono)
    return []


def on_pre_cancel_failed(s: AutoMSet, block: Block) -> None:
    """취소 **요청**이 실패(LS 초당 한도 등) — 주문은 그대로 걸려 있으니 표시만 되돌려 다음
    판정에서 다시 취소를 보내게 한다(실측 2026-09-08: 표시가 남아 재시도·종료 취소가 막힘)."""
    leg = s.leg(block)
    leg.replace_pending = False
    leg.cancel_sent = False


PRE_REJECT_LIMIT = 3  # 선주문 연속 거부 → 세트 양쪽 실행 끔 + 알람(사용자 확정 2026-09-11)


def on_pre_reject(s: AutoMSet, block: Block, mono: float, settings: AutoMSettings,
                  reason: str = "") -> list[Action]:
    """선주문 거부 → 딜레이 뒤 다시 냄(exec ㄴ5). 단 **연속 PRE_REJECT_LIMIT회**면 원인이 남아 있는
    것(증거금 부족·주문가능수량 초과 등)이라 세트 진입·청산 **둘 다 실행을 끄고** 알람(결정 29).
    체결 전이라 헤지가 깨진 게 아니므로 중지(검정)가 아니라 실행 끔 — 사람이 정리 뒤 다시 켠다.
    """
    leg = s.leg(block)
    leg.replace_pending = False
    leg._clear_pre()  # 거부된 주문은 취소할 것도 없음 — 번호·취소 표시 정리
    leg.reject_streak += 1
    why = f"선주문 거부{(': ' + reason) if reason else ''}"
    if leg.reject_streak >= PRE_REJECT_LIMIT:
        s.entry.reject_streak = s.exit.reject_streak = 0
        acts = set_running(s, Block.ENTRY, False, mono) + set_running(s, Block.EXIT, False, mono)
        acts.append(Action("alarm", reason=f"{why} — 연속 {PRE_REJECT_LIMIT}회, 세트 진입·청산 "
                                           f"실행 끔(원인 정리 뒤 다시 켜세요)"))
        return acts
    _start_delay(leg, mono, settings)
    return [Action("notify", reason=f"{why} — 딜레이 뒤 재시도({leg.reject_streak}/"
                                    f"{PRE_REJECT_LIMIT})")]


def on_pre_fill(
    s: AutoMSet, block: Block, qty: int, price: float, mono: float,
) -> list[Action]:
    """선주문 체결(일부/전부) → 체결분 × 10 후주문 즉시(exec ㄴ6·ㄴ7). 누적 SF 갱신."""
    leg = s.leg(block)
    leg.pre_filled += qty
    leg.reject_streak = 0  # 체결됐으면 거부 연속은 끊김
    # 선주문 체결은 판 버퍼(pending)에 보관 — 매매결과(acc)는 후주문 전량 체결 확인 뒤 합친다
    leg.pending.sf_qty += qty
    leg.pending.sf_px_sum += price * qty
    leg.post_pending += float(qty * HL_PER_SF)
    s.sf_net += qty if leg.pre_side is Side.BUY else -qty
    # RT선진입은 **선주문(SF) 체결 계약수** 기준(사용자 확정 2026-09-08) — HL 체결(소수·부분)로
    # 환산하지 않는다. 정방향: 진입 +, 청산 −(0 아래로는 안 감). 역방향: 진입 −, 청산 +(0 위로는
    # 안 감) — RT는 항상 0 또는 음수(사용자 확정 2026-09-14, §7A).
    if block is Block.ENTRY:
        s.rt += -qty if s.reverse else qty
    else:
        s.rt = min(0, s.rt + qty) if s.reverse else max(0, s.rt - qty)
    if leg.pre_filled >= leg.pre_qty and leg.pre_qty > 0:
        leg.status = LegStatus.POST_PENDING
    else:
        leg.status = LegStatus.PRE_PARTIAL
    _refresh_fill_diff(s)  # 화면 체결차 칸 = 장부 실시간(후주문 대기 중엔 +값이 잠깐 보임 — 정상)
    return [Action("place_post", side=leg.post_side, qty=qty * HL_PER_SF)]


def _refresh_fill_diff(s: AutoMSet) -> None:
    """화면 체결차 칸을 장부(sf_net·hl_net)로 다시 계산 — 표시용, 판정과 무관.

    실측 2026-09-10 10:14:54: 중지 뒤 들어온 후주문 체결로 장부는 −10인데 칸은 중지 때 값 −20에
    멈춰 있었다(칸은 판이 끝날 때·중지 때만 갱신). 사람이 정리할 수량을 잘못 보게 되므로 체결마다
    갱신한다.
    """
    s.fill_diff = round(fill_diff(s.sf_net, s.hl_net), 6)


def on_pre_cancelled(s: AutoMSet, block: Block, mono: float, settings: AutoMSettings) -> None:
    """선주문 취소 확인 — 재발주 취소면 병행 후주문 확인 뒤 딜레이, 아니면 감시로."""
    leg = s.leg(block)
    leg.reject_streak = 0  # 걸렸다가 취소된 것 = 접수는 정상 → 거부 연속 끊김
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
    stock_last: float | None = None, sf_theory: float | None = None,
) -> list[Action]:
    """후주문(HL) 체결 → RT 증감·누적(§9a)·헤지 완성 판정(exec ㄹ1).

    fx_quote = 체결 시점 원달러선물 호가(진입 −환은 매수1호가, 청산 +환은 매도1호가).
    stock_last·sf_theory = 체결 시점 S현재가·SF이론가 — Sprd 기준값(사용자 확정 2026-09-10,
    실시간 아님). 없으면 그 판의 Sprd는 계산 불가.
    """
    leg = s.leg(block)
    leg.pending.hl_qty += hl_qty
    leg.pending.hl_px_sum += hl_price * hl_qty
    leg.pending.fx_sum += fx_quote * hl_qty
    if stock_last and sf_theory:  # 둘 다 있을 때만 기준값 기록(분모 ref_qty도 같이)
        leg.pending.s_px_sum += stock_last * hl_qty
        leg.pending.theory_sum += sf_theory * hl_qty
        leg.pending.ref_qty += hl_qty
    leg.post_pending = max(0.0, leg.post_pending - hl_qty)
    # RT는 선주문(SF) 체결에서 갱신(on_pre_fill) — 여기서는 HL 순잔고·전환딜레이 기준 시각만.
    # HL 순잔고는 후주문 방향으로(매도 −, 매수 +): 정방향 진입·역방향 청산 −, 그 반대는 +
    s.hl_net += -hl_qty if leg.post_side is Side.SELL else hl_qty
    if block is Block.ENTRY:
        s.last_entry_fill_mono = mono
    else:
        s.last_exit_fill_mono = mono
    _refresh_fill_diff(s)  # 중지 상태에서 들어온 체결도 칸에 반영(실측 09-10: −20 → −10)
    if not post_done(leg):
        return []
    return _finish_round(s, block, mono, settings)


def diff_limit(s: AutoMSet) -> float:
    """체결차 중지 한도(HL 계약) = 1회주문수량 × 10(사용자 확정 2026-09-10). 1회주문수량이 0이면
    어떤 차이든 중지."""
    return s.per_qty * HL_PER_SF


def _over_limit(s: AutoMSet, diff: float) -> bool:
    limit = diff_limit(s)
    return abs(diff) >= (limit - _EPS if limit > 0 else _EPS)


def _finish_round(s: AutoMSet, block: Block, mono: float, settings: AutoMSettings,
                  shortfall: str = "") -> list[Action]:
    """후주문 대기가 0이 된 순간(판 끝) — 매매결과 합산·체결차 판정(사용자 확정 2026-09-10).

    - 이 판의 HL 체결이 SF 체결×10에 맞으면 전량, 모자라면(잔량 버림·거부) 짝 맞은 몫만 누적(§10).
    - 체결차(세트 누적 장부)는 계속 누적하고 **|체결차| ≥ 1회주문수량×10 이면 그때 중지**.
      그 미만이면 경고만 남기고 딜레이 → 다음 판으로 계속 간다(작은 잔량으로는 멈추지 않음).
    """
    leg = s.leg(block)
    diff = round(fill_diff(s.sf_net, s.hl_net), 6)
    s.fill_diff = diff
    clean = leg.pending.hl_qty + _EPS >= leg.pending.sf_qty * HL_PER_SF
    leg.acc.add_round(leg.pending, matched_only=not clean)
    leg.pending.clear()
    limit = diff_limit(s)
    acts: list[Action] = []
    if leg.status is LegStatus.HALTED:
        # 중지 뒤 들어온 후주문 체결로 판이 닫힘 — 장부·누적만 갱신하고 중지는 유지(사람이 해제).
        # 실측 2026-09-11 14:41:19: 여기서 딜레이대기로 넘어가 청산이 halted → settle_delay 로 풀려
        # 세트 반쪽만 중지로 남았다. 되살아난 후주문(결정 30)의 체결도 같은 경로.
        if abs(diff) < _EPS and "장부 보정" not in leg.halt_reason:
            leg.halt_reason += " → 이후 후주문 체결로 장부 보정(체결차 0), 해제하면 재개"
        return acts
    if not set_post_done(s):
        # 다른 쪽(진입↔청산) 후주문이 아직 대기 중 — 세트 장부에 그 헤지가 빠져 있으니 지금 재면
        # 오판(실측 2026-09-11 14:41 −10 중지). 이 판은 끝내고 판정은 그쪽 판 끝에서.
        if abs(diff) >= _EPS or shortfall:
            acts.append(Action("notify", reason=f"체결차 {diff:g} — 다른 쪽 후주문 대기 중, "
                                                f"그 체결 뒤 판정"
                                                + (f" — {shortfall}" if shortfall else "")))
    elif _over_limit(s, diff):
        return _halt_set(s, block, f"체결차 {diff:g} ≥ 한도 {limit:g}(1회주문수량 {s.per_qty}×10)"
                         + (f" — {shortfall}" if shortfall else ""))
    elif abs(diff) >= _EPS or shortfall:
        acts.append(Action("notify", reason=f"체결차 {diff:g} (한도 {limit:g} 미만, 계속)"
                                            + (f" — {shortfall}" if shortfall else "")))
    # 이번 판 끝 — 남은 선주문 없으면 딜레이, 부분체결 잔량이 남아 있으면 계속 대기
    if leg.await_post_then_delay or leg.pre_order_id is None or leg.pre_filled >= leg.pre_qty:
        leg.await_post_then_delay = False
        _start_delay(leg, mono, settings)
    else:
        # 되살아난 후주문(결정 30)의 체결로 판이 끝나면 선주문이 아직 안 잡힌 채 걸려 있을 수 있다
        leg.status = LegStatus.PRE_PARTIAL if leg.pre_filled > 0 else LegStatus.PRE_RESTING
    return acts


def on_post_recovered(s: AutoMSet, block: Block, qty: float) -> list[Action]:
    """발주 실패로 처리했던 후주문이 살아 있는 것으로 밝혀짐(결정 30, 사용자 확정 2026-09-11) —
    그 수량을 후주문 대기에 도로 넣어 뒤따르는 체결이 세트 장부(HL 잔고·체결차)에 들어가게 한다.
    그 판의 SF 몫은 이미 판 버퍼에서 빠졌으므로 이 체결은 지금 진행 중인 판에 섞인다(매매결과
    평균가만 흐려짐 — 통신 장애 때만 나는 드문 경우라 감수)."""
    leg = s.leg(block)
    leg.post_pending += float(qty)
    _refresh_fill_diff(s)
    return [Action("notify", reason=f"실패 처리했던 후주문 살아 있음 — HL 대기 +{qty:g}, "
                                    f"체결로 장부 보정")]


def _halt_set(s: AutoMSet, block: Block, reason: str) -> list[Action]:
    """세트 중지(exec §2·결정 로그 7·9: 헤지 깨진 **세트**는 멈춤 — 사용자 확인 2026-09-07).

    체결차를 낸 쪽뿐 아니라 **진입·청산 모두** 실행을 끄고 중지로 둔다(청산 체결차인데 진입이 계속
    새 선주문을 내면 안 됨). 다른 쪽에 걸린 선주문은 취소. 해제(release_halt)도 세트 단위.
    """
    leg = s.leg(block)
    leg.status = LegStatus.HALTED
    leg.running = False  # 중지 = 실행 꺼짐(버튼 원색). 다시 켜려면 사람이 해제
    leg.halt_reason = reason
    # 판이 중지로 끝남 — 짝이 맞은(적은 쪽) 몫만 매매결과에 넣고 버퍼를 비운다(사용자 확정 09-08)
    leg.acc.add_round(leg.pending, matched_only=True)
    leg.pending.clear()
    acts: list[Action] = [Action("halt", reason=reason), Action("notify", reason=reason)]
    other = s.leg(Block.EXIT if block is Block.ENTRY else Block.ENTRY)
    if other.status is not LegStatus.HALTED:
        acts += _cancel_if_resting(other)  # 걸린 선주문 취소(취소 확인은 통보로)
        other.status = LegStatus.HALTED
        other.running = False
        other.halt_reason = f"{'청산' if block is Block.EXIT else '진입'} 체결차로 세트 중지"
    return acts


def on_post_reject(
    s: AutoMSet, block: Block, reason: str = "", qty: float | None = None,
    mono: float | None = None, settings: AutoMSettings | None = None,
) -> list[Action]:
    """후주문이 안 잡힌 채 끝남(통째 거부·잔량 버림·밖에서 취소·장부에서 사라짐) — 미체결분을
    후주문 대기에서 빼고, 대기가 0이 되면 판을 끝내며 체결차를 판정한다(사용자 확정 2026-09-10:
    바로 멈추지 않고 |체결차| ≥ 1회주문수량×10 일 때만 중지, ㄹ2 정정). 아직 다른 후주문이 대기
    중이면 그 체결까지 본 뒤 판정. qty가 없으면 남은 대기분 전부가 미체결.
    """
    leg = s.leg(block)
    unfilled = leg.post_pending if qty is None else min(float(qty), leg.post_pending)
    leg.post_pending = max(0.0, leg.post_pending - unfilled)
    _refresh_fill_diff(s)
    note = f"후주문 미체결 HL {unfilled:g}계약{(': ' + reason) if reason else ''}"
    if not post_done(leg):
        return [Action("notify", reason=f"{note} — 남은 후주문 확인 뒤 판정")]
    return _finish_round(s, block, mono if mono is not None else 0.0,
                         settings if settings is not None else AutoMSettings(), shortfall=note)


def halt_if_unhedged(s: AutoMSet, block: Block, diff: float) -> list[Action]:
    """체결차 판정(exec ㅂ1) — 후주문 대기분이 없을 때 |diff| ≥ 1회주문수량×10 이면 세트 중지.
    그 미만은 그대로 진행(사용자 확정 2026-09-10). diff는 코어가 넣는다."""
    leg = s.leg(block)
    s.fill_diff = round(diff, 6)  # HL 소수 계약 그대로(0.412 등) — 정수로 깎으면 체결차가 사라진다
    if not set_post_done(s) or leg.status is LegStatus.HALTED or not _over_limit(s, diff):
        return []
    return _halt_set(s, block,
                     f"체결차 {diff:g} ≥ 한도 {diff_limit(s):g}(1회주문수량 {s.per_qty}×10)")


def set_running(s: AutoMSet, block: Block, value: bool,
                mono: float | None = None) -> list[Action]:
    """실행 켬/끔(exec ㅂ2) — 끄면 미체결 선주문 취소(체결 포지션 유지). 중지 상태는 끄기만 허용."""
    leg = s.leg(block)
    leg.running = value
    if value:
        if leg.status is LegStatus.IDLE:
            leg.status = LegStatus.ARMED
        return []
    # 끔·정지·종료 — 취소 대기 표시와 무관하게 취소(mono는 확인 타임아웃 기준 시각)
    acts = _cancel_if_resting(leg, force=True, mono=mono)
    if leg.pre_order_id is None and post_done(leg) and leg.status is not LegStatus.HALTED:
        leg.status = LegStatus.IDLE
    return acts


def on_post_partial_reject(
    s: AutoMSet, block: Block, unfilled: float,
    mono: float | None = None, settings: AutoMSettings | None = None,
) -> list[Action]:
    """후주문 일부만 체결되고 나머지가 취소/거부/버려짐 — 미체결분(소수 그대로)을 대기에서 빼고
    판 끝 판정(on_post_reject와 같은 규칙: 한도 미만이면 계속)."""
    return on_post_reject(s, block, "", qty=unfilled, mono=mono, settings=settings)


def release_halt(s: AutoMSet, block: Block) -> None:
    """중지 해제 — 사람이 정리한 뒤 직접 푼다(exec §2). **세트 단위**(중지가 세트 단위이므로):
    진입·청산 어느 쪽에서 풀든 둘 다 꺼진 대기(idle)로 돌아간다.

    세트 장부(sf_net·hl_net·체결차)는 **건드리지 않는다**(사용자 확정 2026-09-10 오후): 사람이
    헤지를 정리한 뒤 세트설정 "체결차 Clear"로 직접 0으로 만든다. 해제가 장부를 지우면 정리 안 한
    차이가 사라져 보이므로. 진행 중이던 판 버퍼·후주문 대기·딜레이 표시만 정리한다.
    """
    for leg in (s.entry, s.exit):
        if leg.status is not LegStatus.HALTED:
            continue
        leg.status = LegStatus.IDLE
        leg.running = False
        leg.halt_reason = ""
        leg._clear_pre()
        leg.post_pending = 0.0
        leg.pending.clear()
        leg.replace_pending = leg.await_post_then_delay = False
        leg.delay_until = None


# ---------------------------------------------------------- 화면 단위 묶음 ---

SET_COUNT = 3


@dataclass
class AutoMBook:
    """종목 하나의 자동M 상태 — 정방향 3세트 + 기준수량 + 월물(사용자 확정 2026-09-08: 종목별 독립).

    창(order_autom)은 종목 콤보로 어느 책을 보여줄지 고를 뿐이고, 실행은 코어가 종목마다 따로 돈다.
    같은 종목을 두 창에서 열면 같은 책을 함께 보여준다(중복 실행 아님)."""

    sets: list[AutoMSet] = field(default_factory=lambda: [AutoMSet() for _ in range(SET_COUNT)])
    # 역방향 3세트(§7A·§7B, 2026-09-14) — 정방향과 같은 뼈대, reverse=True
    rev_sets: list[AutoMSet] = field(
        default_factory=lambda: [AutoMSet(reverse=True) for _ in range(SET_COUNT)])
    ref_qty: int = 1  # 상단 기준수량(계약) — 모니터 3칸(진입SF·진입S·청산SF) est 계산용
    future_month: str = "near"  # 선물 월물 "near"|"next" (exec §11.9, DESIGN §5.11)

    def sets_of(self, reverse: bool) -> list[AutoMSet]:
        return self.rev_sets if reverse else self.sets

    def all_sets(self) -> list[tuple[bool, int, AutoMSet]]:
        """(역방향 여부, 세트 번호, 세트) 전부 — 엔진 순회용."""
        return ([(False, i, s) for i, s in enumerate(self.sets)]
                + [(True, i, s) for i, s in enumerate(self.rev_sets)])

    def any_running(self) -> bool:
        return any(s.entry.running or s.exit.running for _r, _i, s in self.all_sets())

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
    # 역방향(§11.9): 진입 < en, 청산 > ex, 청산−진입 > gap (코어 저장 2026-09-14)
    risk_rev_en: float = 0.005
    risk_rev_ex: float = 0.0
    risk_rev_gap: float = 0.001

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
    for key, targets in (("sets", book.sets), ("rev_sets", book.rev_sets)):
        _sets_from_dict(targets, raw.get(key))


def _sets_from_dict(targets: list[AutoMSet], sets: object) -> None:
    """세트 목록(정방향 sets 또는 역방향 rev_sets) 복원 — 값 오류는 그 필드만 기본값."""
    if isinstance(sets, list):
        for target, rs in zip(targets, sets, strict=False):
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
                target.fill_diff = round(fill_diff(target.sf_net, target.hl_net), 6)
            except (TypeError, ValueError):
                pass
            for name, leg in (("entry", target.entry), ("exit", target.exit)):
                found = rs.get(name)
                raw_leg: dict[str, Any] = found if isinstance(found, dict) else {}
                # 중지는 재시동 뒤에도 유지(사용자 확정 2026-09-10, exec §2 "사람이 직접 풀어야
                # 재개"). 실측 10:42 재시동이 중지를 대기로 되살려 정리·해제 없이 다음 판이 돌았다.
                # 다른 진행 상태(접수·후주문대기 등)는 주문 추적이 끊기므로 지금처럼 대기로.
                if str(raw_leg.get("status", "")) == LegStatus.HALTED.value:
                    leg.status = LegStatus.HALTED
                    leg.running = False
                    reason = str(raw_leg.get("halt_reason") or "")
                    leg.halt_reason = (reason if reason.startswith("재시동 전")
                                       else f"재시동 전 {reason}")
                    try:
                        leg.post_pending = float(raw_leg.get("post_pending") or 0.0)
                    except (TypeError, ValueError):
                        pass
                acc = raw_leg.get("acc")
                if isinstance(acc, dict):
                    try:
                        leg.acc.hl_qty = float(acc.get("hl_qty", 0) or 0)
                        leg.acc.hl_px_sum = float(acc.get("hl_px_sum", 0) or 0)
                        leg.acc.fx_sum = float(acc.get("fx_sum", 0) or 0)
                        leg.acc.s_px_sum = float(acc.get("s_px_sum", 0) or 0)
                        leg.acc.theory_sum = float(acc.get("theory_sum", 0) or 0)
                        leg.acc.ref_qty = float(acc.get("ref_qty", 0) or 0)  # 옛 저장분은 0
                        leg.acc.sf_qty = float(acc.get("sf_qty", 0) or 0)
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
    for key in ("risk_fwd_en", "risk_fwd_ex", "risk_fwd_gap",
                "risk_rev_en", "risk_rev_ex", "risk_rev_gap"):
        try:
            setattr(screen, key, float(raw.get(key, getattr(screen, key))))
        except (TypeError, ValueError):
            pass
