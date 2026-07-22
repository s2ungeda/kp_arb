"""전략 코어 — 세트 상태·입력 검증·수량 한도·주문 계획 (DESIGN §6.2). 순수 로직.

"코어 하나 + 여러 화면"(§12): 화면(strategy_panel)은 여기의 명령을 호출만 하고,
판단(검증·한도·환산)은 전부 이 모듈이 한다. 접속·주문 전송(LiveSystem 연결)은
다음 단계 — 이 모듈은 I/O 없음.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime
from enum import StrEnum

from .domain.enums import Instrument, Side, Underlying, Venue
from .theory import in_time_window

FUTURES_SHARES_PER_CONTRACT = 10  # 주식선물 1계약 = 10주 (§6.2-2)


class Mode(StrEnum):
    MANUAL = "수동"
    AUTO_T = "자동T"   # 신호 시 양쪽 동시 taker
    AUTO_M = "자동M"   # 국내 maker(페깅) 체결 → HL taker


class OrderAction(StrEnum):
    ENTER = "진입"   # LS 매수 + HL 매도 (방향 A)
    EXIT = "청산"    # LS 매도 + HL 매수


@dataclass
class SetInput:
    """세트 1개의 입력값 (§6.2-1). 수량은 국내 기준(주식=주, 선물=계약)."""

    total_qty: int = 0            # 총 진입수량 = 국내 최대 보유 한도
    per_order_qty: int = 0        # 1회 주문수량
    entry_threshold: float | None = None  # 진입 기준값 (자동 필수)
    exit_threshold: float | None = None   # 청산 기준값 (자동 필수)


@dataclass
class SetState:
    """세트 런타임 상태. started 해제 = 프로세스 중지+취소 / paused = 발주만 중단."""

    inputs: SetInput = field(default_factory=SetInput)
    started: bool = False
    paused: bool = False


@dataclass
class RetryOptions:
    """옵션 창 값 (§6.2-4·5). credit=주식 신용거래."""

    max_retries: int = 3
    retry_interval_s: float = 1.0
    wait_timer_s: float = 5.0
    order_window: tuple[str, str] = ("09:00", "15:30")  # 주문 가능 시간
    stock_credit: bool = False


@dataclass(frozen=True)
class Leg:
    """주문 계획의 한 다리."""

    venue: Venue
    side: Side
    qty: int  # 그 거래소의 주문 단위 수량 (LS=국내 단위, HL=주 환산)


@dataclass(frozen=True)
class OrderPlan:
    """검증을 통과한 양다리(또는 수동 단독) 주문 계획."""

    action: OrderAction
    legs: tuple[Leg, ...]


SET_COUNT = 3  # 입력 세트 수 (§6.2-1 — 추후 확장 가능)


@dataclass
class PanelState:
    """전략 화면 1장의 전체 상태 — 코어가 단일 진실로 보관 (§12).

    화면은 이 상태를 읽어 표시만 하고, 변경은 명령(core_server)으로만 한다.
    """

    mode: Mode = Mode.MANUAL
    underlying: Underlying = Underlying.SK_HYNIX          # 콤보1 기본
    counterpart: Instrument = Instrument.KR_STOCK_FUTURE  # 콤보2 기본
    ls_enabled: bool = True
    hl_enabled: bool = True
    monitor_qty: int = 0  # 모니터링 전용 수량 — estprice 계산용 (§6.2-1)
    sets: list[SetState] = field(default_factory=lambda: [SetState() for _ in range(SET_COUNT)])
    options: RetryOptions = field(default_factory=RetryOptions)

    def start_set(self, index: int, value: bool) -> list[str]:
        """시작 체크/해제. 켤 때는 입력 검증 통과 필수 — 실패 사유 반환(켜지지 않음)."""
        target = self.sets[index]
        if value:
            errors = validate_inputs(target.inputs, self.mode)
            if self.mode is Mode.MANUAL:
                errors.append("수동 모드에는 시작이 없음")
            if errors:
                return errors
        target.started = value
        if not value:
            target.paused = False  # 중지 시 일시정지도 초기화
        return []

    def pause_set(self, index: int, value: bool) -> None:
        self.sets[index].paused = value


def validate_inputs(inputs: SetInput, mode: Mode) -> list[str]:
    """세트 입력값 검증 — 위반 사유 목록(비면 통과). §6.2-4 주문 전 체크."""
    errors: list[str] = []
    if inputs.total_qty <= 0:
        errors.append("총 진입수량은 1 이상이어야 함")
    if inputs.per_order_qty <= 0:
        errors.append("1회 주문수량은 1 이상이어야 함")
    elif inputs.total_qty > 0 and inputs.per_order_qty > inputs.total_qty:
        errors.append("1회 주문수량이 총 진입수량보다 큼")
    if mode is not Mode.MANUAL:  # 자동: 기준값 필수 + 진입 > 청산 (§6.2-1)
        if inputs.entry_threshold is None or inputs.exit_threshold is None:
            errors.append("자동 모드는 진입/청산 기준값 필수")
        elif inputs.entry_threshold <= inputs.exit_threshold:
            errors.append("진입 기준값은 청산 기준값보다 커야 함")
    return errors


def hl_qty_for(counterpart: Instrument, kr_qty: int) -> int:
    """국내 수량 → HL 수량(주 환산): 주식 1:1, 주식선물 1계약=10주 (§6.2-2)."""
    if counterpart is Instrument.KR_STOCK_FUTURE:
        return kr_qty * FUTURES_SHARES_PER_CONTRACT
    return kr_qty


def allowed_order_qty(
    action: OrderAction,
    counterpart: Instrument,
    position_qty: int,
    inputs: SetInput,
) -> int:
    """이번 주문에 허용되는 국내 수량 (§6.2-2 한도).

    - 주식: 0 ≤ 포지션 ≤ 총진입 (공매도 금지 — 청산은 보유분까지만)
    - 주식선물: |포지션| ≤ 총진입 (잔고 없어도 청산부터 = 숏 진입 가능)
    """
    total, per = inputs.total_qty, inputs.per_order_qty
    if action is OrderAction.ENTER:
        room = total - position_qty
    elif counterpart is Instrument.KR_STOCK_FUTURE:
        room = total + position_qty   # 청산(매도): 포지션 - q ≥ -총진입
    else:
        room = position_qty           # 주식 매도는 보유분까지만
    return max(0, min(per, room))


def order_window_ok(now: dtime, options: RetryOptions) -> bool:
    """주문 가능 시간(옵션) 안인가 — 밖이면 주문 금지 (§6.2)."""
    from .theory import parse_hhmm

    start, end = options.order_window
    return in_time_window(now, parse_hhmm(start), parse_hhmm(end))


def plan_order(
    action: OrderAction,
    counterpart: Instrument,
    position_qty: int,
    inputs: SetInput,
    *,
    mode: Mode,
    ls_enabled: bool,
    hl_enabled: bool,
    now: dtime,
    options: RetryOptions,
) -> tuple[OrderPlan | None, list[str]]:
    """주문 전 검증(§6.2-4) 후 주문 계획 생성. (계획, 위반 사유) — 계획은 통과 시만.

    - 진입 = LS 매수 + HL 매도 / 청산 = LS 매도 + HL 매수.
    - LS/HL 단독 체크는 **수동 전용**(사용자 확정 — 급변·시세 이상 시 판단 주문).
      자동은 양쪽 모두 체크돼야 한다.
    """
    if counterpart not in (Instrument.KR_STOCK, Instrument.KR_STOCK_FUTURE):
        return None, [f"지원하지 않는 국내 상대: {counterpart}"]
    errors = validate_inputs(inputs, mode)
    if not ls_enabled and not hl_enabled:
        errors.append("LS/HL 둘 다 해제됨 — 주문 불가")
    elif mode is not Mode.MANUAL and not (ls_enabled and hl_enabled):
        errors.append("자동 모드는 LS/HL 모두 체크 필요")
    if not order_window_ok(now, options):
        errors.append("주문 가능 시간 밖")
    qty = allowed_order_qty(action, counterpart, position_qty, inputs)
    if qty <= 0:
        errors.append("한도 내 주문 가능 수량 없음")
    if errors:
        return None, errors

    ls_side = Side.BUY if action is OrderAction.ENTER else Side.SELL
    hl_side = Side.SELL if action is OrderAction.ENTER else Side.BUY
    legs: list[Leg] = []
    if ls_enabled:
        legs.append(Leg(venue=Venue.LS, side=ls_side, qty=qty))
    if hl_enabled:
        legs.append(Leg(venue=Venue.HYPERLIQUID, side=hl_side,
                        qty=hl_qty_for(counterpart, qty)))
    return OrderPlan(action=action, legs=tuple(legs)), []
