"""주문 화면(자동T/자동M) 코어 — 상태·검증·한도·주문 계획. 순수 로직, I/O 없음.

DESIGN §6.2 전면 개정 2026-07-22 (원본 주문화면0722.xlsx):
- 화면 2개: 자동T = HL-주식(동시 taker) / 자동M = HL-주식선물(LS maker→HL taker)
- 화면마다 entry 3세트 + exit 3세트, 1회주문수량은 화면당 1개(est-pr 공용)
- 수량 비율 고정: SF 1계약 = 주식 10주 = HL 10계약
화면은 명령 호출만("코어 하나 + 여러 화면" §12). LiveSystem 결합은 다음 단계.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime
from enum import StrEnum

from .domain.enums import Instrument, Side, Underlying, Venue
from .theory import in_time_window, parse_hhmm

FUTURES_SHARES_PER_CONTRACT = 10  # 주식선물 1계약 = 10주 = HL 10계약 (§6.2-3)
SET_COUNT = 3                     # entry/exit 각 세트 수
THRESHOLD_LIMIT = 0.01            # 기준값 절대 한계 ±1% (§6.2-6)


class ScreenKind(StrEnum):
    AUTO_T = "autoT"   # HL-주식 — 양쪽 동시 taker
    AUTO_M = "autoM"   # HL-주식선물 — LS 선주문(maker) → HL 후주문(taker)

    @property
    def counterpart(self) -> Instrument:
        return (Instrument.KR_STOCK if self is ScreenKind.AUTO_T
                else Instrument.KR_STOCK_FUTURE)


class Block(StrEnum):
    ENTRY = "entry"   # en: 국내 매수 + HL 매도
    EXIT = "exit"     # ex: 국내 매도 + HL 매수


# 운영시간 기본값 (§6.2-1, 문서 값). 세션 가드(JIF)와 별개의 화면 규칙.
OPERATING_WINDOWS: dict[ScreenKind, tuple[tuple[str, str], ...]] = {
    ScreenKind.AUTO_T: (("08:00", "08:50"), ("09:00", "15:30"), ("15:40", "20:00")),
    ScreenKind.AUTO_M: (("08:45", "15:35"),),
}


def in_operating_window(kind: ScreenKind, now: dtime) -> bool:
    """화면 운영시간 안인가 — 밖이면 자동 주문 발생 금지 (§6.2-1)."""
    return any(
        in_time_window(now, parse_hhmm(start), parse_hhmm(end))
        for start, end in OPERATING_WINDOWS[kind]
    )


@dataclass
class SpreadSet:
    """세트 1개 (1st/2nd/3rd): 기준값·목표량·실행 상태·발주 누적 (§6.2-2)."""

    threshold: float | None = None  # 스프레드 기준값 (소수 — 0.006 = 0.6%)
    target_qty: int = 0             # 목표진입량 (국내 단위)
    running: bool = False           # 실행 버튼(토글) — 저장 시 복원 안 함
    fired_qty: int = 0              # 발주 주문수량 누적 (체결수량 아님 — §6.2-2)
    ls_order: bool = True           # LS주문 체크 — 해제 시 HL 주문만 (세트별)


@dataclass
class ScreenSettings:
    """설정창 값 (§6.2-4·6)."""

    kr_margin_ticks: int = 10        # taker 주문가 여유 — 국내 다리(틱)
    hl_margin_pct: float = 0.01      # taker 주문가 여유 — HL 다리(1% = 0.01)
    delay_ms: int = 500              # 자동M: 체결/거부/취소 후 재주문 딜레이
    pre_order_range_ticks: int = 0   # 자동M: 선주문진입범위(틱, 0=제한 없음)
    max_position: int = 0            # 종목보유최대수량 — 한 방향 최대(국내 단위)
    daily_limit_100m: float = 0.0    # 일거래한도(억) — 국내 매수+매도 대금. 0=미사용


@dataclass
class ScreenState:
    """주문 화면 1개(자동T 또는 자동M)의 전체 상태."""

    kind: ScreenKind
    underlying: Underlying = Underlying.SK_HYNIX
    per_order_qty: int = 0           # 1회주문수량 (화면당 1개 — est-pr 공용)
    entry_sets: list[SpreadSet] = field(
        default_factory=lambda: [SpreadSet() for _ in range(SET_COUNT)])
    exit_sets: list[SpreadSet] = field(
        default_factory=lambda: [SpreadSet() for _ in range(SET_COUNT)])
    settings: ScreenSettings = field(default_factory=ScreenSettings)

    def sets_of(self, block: Block) -> list[SpreadSet]:
        return self.entry_sets if block is Block.ENTRY else self.exit_sets


@dataclass
class CoreState:
    """코어 전체 상태 — 화면 2개 + 공통 선택."""

    screens: dict[ScreenKind, ScreenState] = field(default_factory=lambda: {
        ScreenKind.AUTO_T: ScreenState(kind=ScreenKind.AUTO_T),
        ScreenKind.AUTO_M: ScreenState(kind=ScreenKind.AUTO_M),
    })
    fx_month: str = "near"  # 환율 표시용 원달러선물 월물 선택: near/next (§6.2-7)


# --- 검증 (§6.2-6) ---

def threshold_check(block: Block, value: float) -> tuple[list[str], list[str]]:
    """기준값 입력 가드 → (오류, 경고). 오류는 입력 불가, 경고는 확인창."""
    errors: list[str] = []
    warnings: list[str] = []
    if block is Block.ENTRY:
        if value <= -THRESHOLD_LIMIT:
            errors.append("entry 기준값은 -1% 이하 입력 불가")
        elif value <= 0:
            warnings.append("낮은수치")
    else:
        if value >= THRESHOLD_LIMIT:
            errors.append("exit 기준값은 +1% 이상 입력 불가")
        elif value >= 0:
            warnings.append("높은수치")
    return errors, warnings


def validate_run(screen: ScreenState, block: Block, index: int) -> list[str]:
    """실행 버튼 켤 때 검증 — 위반 사유 목록(비면 통과)."""
    errors: list[str] = []
    if screen.per_order_qty <= 0:
        errors.append("1회주문수량은 1 이상이어야 함")
    if screen.settings.max_position <= 0:
        errors.append("종목보유최대수량 설정 필요")
    target = screen.sets_of(block)[index]
    if target.target_qty <= 0:
        errors.append("목표진입량은 1 이상이어야 함")
    if target.threshold is None:
        errors.append("스프레드 기준값 필수")
    else:
        errors.extend(threshold_check(block, target.threshold)[0])
    return errors


# --- 수량 (§6.2-3) ---

def hl_qty_for(counterpart: Instrument, kr_qty: int) -> int:
    """국내 수량 → HL 계약수: 주식 1주=1계약, 선물 1계약=10계약."""
    if counterpart is Instrument.KR_STOCK_FUTURE:
        return kr_qty * FUTURES_SHARES_PER_CONTRACT
    return kr_qty


def allowed_order_qty(
    block: Block,
    counterpart: Instrument,
    position_qty: int,
    per_order_qty: int,
    max_position: int,
) -> int:
    """이번 주문에 허용되는 국내 수량 — 보유최대수량 한도 (§6.2-3).

    - 주식: 0 ≤ 포지션 ≤ 최대 (공매도 금지 — exit은 보유분까지만)
    - 주식선물: |포지션| ≤ 최대 (잔고 없어도 ex부터 = 숏 스프레드 가능)
    """
    if block is Block.ENTRY:
        room = max_position - position_qty
    elif counterpart is Instrument.KR_STOCK_FUTURE:
        room = max_position + position_qty  # 매도: 포지션 - q ≥ -최대
    else:
        room = position_qty                 # 주식 매도는 보유분까지만
    return max(0, min(per_order_qty, room))


# --- 주문 계획 (§6.2-1·2) ---

@dataclass(frozen=True)
class Leg:
    """주문 계획의 한 다리."""

    venue: Venue
    side: Side
    qty: int  # 그 거래소 단위 수량 (LS=국내 단위, HL=계약)


@dataclass(frozen=True)
class OrderPlan:
    """검증 통과한 주문 계획 (LS 다리는 LS주문 체크 시에만)."""

    block: Block
    legs: tuple[Leg, ...]


def plan_order(
    screen: ScreenState,
    block: Block,
    index: int,
    position_qty: int,
    now: dtime,
) -> tuple[OrderPlan | None, list[str]]:
    """세트 1회 주문 계획 — 운영시간·한도·목표 잔여까지 검증 (§6.2).

    LS 다리는 블록의 LS주문 체크 시에만 포함(해제 = HL 주문만). HL 다리는 항상.
    """
    errors = validate_run(screen, block, index)
    if not in_operating_window(screen.kind, now):
        errors.append("운영시간 밖")
    target = screen.sets_of(block)[index]
    remaining = max(0, target.target_qty - target.fired_qty)
    counterpart = screen.kind.counterpart
    ls_enabled = target.ls_order  # 세트별 LS주문 체크 (해제 = HL 주문만)
    qty = min(
        allowed_order_qty(block, counterpart, position_qty,
                          screen.per_order_qty, screen.settings.max_position),
        remaining,
    )
    if remaining <= 0:
        errors.append("목표진입량 완료")
    elif qty <= 0:
        errors.append("한도 내 주문 가능 수량 없음")
    if errors:
        return None, errors

    ls_side = Side.BUY if block is Block.ENTRY else Side.SELL
    hl_side = Side.SELL if block is Block.ENTRY else Side.BUY
    legs: list[Leg] = []
    if ls_enabled:
        legs.append(Leg(venue=Venue.LS, side=ls_side, qty=qty))
    legs.append(Leg(venue=Venue.HYPERLIQUID, side=hl_side,
                    qty=hl_qty_for(counterpart, qty)))
    return OrderPlan(block=block, legs=tuple(legs)), []


def taker_price(est_price: float, side: Side, margin: float) -> float:
    """taker 지정가 = est-pr에 여유를 더한 가격 (§6.2-4 — 즉시 체결 보장용).

    매수 = est-pr × (1+여유) (위로), 매도 = est-pr × (1−여유) (아래로).
    호가단위 내림/올림은 거래소 규칙이 필요해 실행층(게이트웨이)에서 한다.
    """
    factor = 1.0 + margin if side is Side.BUY else 1.0 - margin
    return est_price * factor


# --- 저장/복원 (§6.2-0 상태 저장) ---

def _set_from_dict(target: SpreadSet, raw: object) -> None:
    if not isinstance(raw, dict):
        return
    try:
        value = raw.get("threshold")
        target.threshold = None if value is None else float(value)
        target.target_qty = int(raw.get("target_qty", 0))
        target.fired_qty = int(raw.get("fired_qty", 0))
        target.ls_order = bool(raw.get("ls_order", True))
        # running은 복원하지 않음 — 재시동 후 자동은 항상 꺼짐 (안전)
    except (TypeError, ValueError):
        pass


def state_from_dict(data: dict[str, object]) -> CoreState:
    """저장 스냅샷(JSON dict) → CoreState 복원. 값 오류는 해당 필드만 기본값."""
    state = CoreState()
    fx = data.get("fx_month")
    if fx in ("near", "next"):
        state.fx_month = str(fx)
    screens = data.get("screens")
    if not isinstance(screens, dict):
        return state
    for kind, screen in state.screens.items():
        raw = screens.get(kind.value)
        if not isinstance(raw, dict):
            continue
        try:
            screen.underlying = Underlying(str(raw.get("underlying", screen.underlying)))
        except ValueError:
            pass
        try:
            screen.per_order_qty = int(raw.get("per_order_qty", 0))
        except (TypeError, ValueError):
            pass
        for name, sets in (("entry_sets", screen.entry_sets),
                           ("exit_sets", screen.exit_sets)):
            raw_sets = raw.get(name)
            if isinstance(raw_sets, list):
                for target, raw_set in zip(sets, raw_sets, strict=False):
                    _set_from_dict(target, raw_set)
        raw_settings = raw.get("settings")
        if isinstance(raw_settings, dict):
            s = screen.settings
            try:
                s.kr_margin_ticks = int(raw_settings.get("kr_margin_ticks", s.kr_margin_ticks))
                s.hl_margin_pct = float(raw_settings.get("hl_margin_pct", s.hl_margin_pct))
                s.delay_ms = int(raw_settings.get("delay_ms", s.delay_ms))
                s.pre_order_range_ticks = int(
                    raw_settings.get("pre_order_range_ticks", s.pre_order_range_ticks))
                s.max_position = int(raw_settings.get("max_position", s.max_position))
                s.daily_limit_100m = float(
                    raw_settings.get("daily_limit_100m", s.daily_limit_100m))
            except (TypeError, ValueError):
                pass
    return state
