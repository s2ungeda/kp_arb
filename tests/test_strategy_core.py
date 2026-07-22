"""전략 코어 테스트 — 검증·한도·환산·주문 계획 (DESIGN §6.2, 순수 로직)."""
from datetime import time

from kp_arb.domain.enums import Instrument, Side, Venue
from kp_arb.strategy_core import (
    Mode,
    OrderAction,
    RetryOptions,
    SetInput,
    allowed_order_qty,
    hl_qty_for,
    order_window_ok,
    plan_order,
    validate_inputs,
)

STOCK = Instrument.KR_STOCK
SF = Instrument.KR_STOCK_FUTURE
NOON = time(12, 0)
OPTS = RetryOptions()  # 주문 가능 시간 기본 09:00~15:30


def _inputs(total: int = 100, per: int = 20,
            entry: float | None = 0.15, exit_: float | None = 0.0) -> SetInput:
    return SetInput(total_qty=total, per_order_qty=per,
                    entry_threshold=entry, exit_threshold=exit_)


# --- 입력 검증 (§6.2-4) ---

def test_validate_ok() -> None:
    assert validate_inputs(_inputs(), Mode.AUTO_T) == []
    # 수동은 기준값 없어도 됨 (에디트 disable)
    assert validate_inputs(_inputs(entry=None, exit_=None), Mode.MANUAL) == []


def test_validate_quantities() -> None:
    assert validate_inputs(_inputs(total=0), Mode.MANUAL) != []
    assert validate_inputs(_inputs(per=0), Mode.MANUAL) != []
    assert validate_inputs(_inputs(total=10, per=20), Mode.MANUAL) != []


def test_validate_auto_thresholds() -> None:
    # 자동: 기준값 필수 + 진입 > 청산 (§6.2-1)
    assert validate_inputs(_inputs(entry=None), Mode.AUTO_T) != []
    assert validate_inputs(_inputs(entry=0.0, exit_=0.0), Mode.AUTO_M) != []
    assert validate_inputs(_inputs(entry=0.1, exit_=0.2), Mode.AUTO_T) != []


# --- 수량 환산 (§6.2-2) ---

def test_hl_qty_conversion() -> None:
    assert hl_qty_for(STOCK, 7) == 7          # 주식 1:1
    assert hl_qty_for(SF, 7) == 70            # 선물 1계약 = 10주


# --- 한도 (§6.2-2) ---

def test_stock_limits() -> None:
    ins = _inputs(total=100, per=20)
    assert allowed_order_qty(OrderAction.ENTER, STOCK, 0, ins) == 20
    assert allowed_order_qty(OrderAction.ENTER, STOCK, 90, ins) == 10   # 한도 잔여만
    assert allowed_order_qty(OrderAction.ENTER, STOCK, 100, ins) == 0
    assert allowed_order_qty(OrderAction.EXIT, STOCK, 5, ins) == 5      # 보유분까지만
    assert allowed_order_qty(OrderAction.EXIT, STOCK, 0, ins) == 0      # 공매도 금지


def test_futures_two_way_limits() -> None:
    # 주식선물: |포지션| ≤ 총진입 — 잔고 없어도 청산부터(숏 진입) 가능
    ins = _inputs(total=100, per=30)
    assert allowed_order_qty(OrderAction.EXIT, SF, 0, ins) == 30
    assert allowed_order_qty(OrderAction.EXIT, SF, -90, ins) == 10      # -100까지만
    assert allowed_order_qty(OrderAction.EXIT, SF, -100, ins) == 0
    assert allowed_order_qty(OrderAction.ENTER, SF, -100, ins) == 30    # 숏 → 롱 방향 여유
    assert allowed_order_qty(OrderAction.ENTER, SF, 100, ins) == 0


# --- 주문 가능 시간 (§6.2 옵션) ---

def test_order_window() -> None:
    assert order_window_ok(time(9, 0), OPTS)
    assert not order_window_ok(time(8, 59), OPTS)
    assert not order_window_ok(time(15, 30), OPTS)


# --- 주문 계획 (§6.2-4) ---

def test_plan_enter_stock_both_legs() -> None:
    plan, errors = plan_order(
        OrderAction.ENTER, STOCK, 0, _inputs(), mode=Mode.MANUAL,
        ls_enabled=True, hl_enabled=True, now=NOON, options=OPTS)
    assert errors == [] and plan is not None
    assert plan.legs[0].venue is Venue.LS and plan.legs[0].side is Side.BUY
    assert plan.legs[1].venue is Venue.HYPERLIQUID and plan.legs[1].side is Side.SELL
    assert plan.legs[0].qty == 20 and plan.legs[1].qty == 20  # 주식 1:1


def test_plan_exit_futures_converts_qty() -> None:
    plan, errors = plan_order(
        OrderAction.EXIT, SF, 0, _inputs(), mode=Mode.MANUAL,
        ls_enabled=True, hl_enabled=True, now=NOON, options=OPTS)
    assert errors == [] and plan is not None
    assert plan.legs[0].side is Side.SELL and plan.legs[1].side is Side.BUY
    assert plan.legs[0].qty == 20 and plan.legs[1].qty == 200  # 1계약=10주


def test_plan_single_venue_manual_only() -> None:
    # 수동: 단독 체크 허용(사용자 판단 주문) — 다리 1개만
    plan, errors = plan_order(
        OrderAction.ENTER, STOCK, 0, _inputs(), mode=Mode.MANUAL,
        ls_enabled=True, hl_enabled=False, now=NOON, options=OPTS)
    assert errors == [] and plan is not None and len(plan.legs) == 1
    # 자동: 양쪽 필수
    plan, errors = plan_order(
        OrderAction.ENTER, STOCK, 0, _inputs(), mode=Mode.AUTO_T,
        ls_enabled=True, hl_enabled=False, now=NOON, options=OPTS)
    assert plan is None and any("모두 체크" in e for e in errors)


def test_plan_rejections() -> None:
    # 주문 가능 시간 밖
    plan, errors = plan_order(
        OrderAction.ENTER, STOCK, 0, _inputs(), mode=Mode.MANUAL,
        ls_enabled=True, hl_enabled=True, now=time(8, 0), options=OPTS)
    assert plan is None and any("시간" in e for e in errors)
    # 한도 소진
    plan, errors = plan_order(
        OrderAction.ENTER, STOCK, 100, _inputs(), mode=Mode.MANUAL,
        ls_enabled=True, hl_enabled=True, now=NOON, options=OPTS)
    assert plan is None and any("수량 없음" in e for e in errors)
    # 거래소 전부 해제
    plan, errors = plan_order(
        OrderAction.ENTER, STOCK, 0, _inputs(), mode=Mode.MANUAL,
        ls_enabled=False, hl_enabled=False, now=NOON, options=OPTS)
    assert plan is None
