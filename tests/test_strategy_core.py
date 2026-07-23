"""주문 화면 코어 테스트 — 검증·한도·환산·주문 계획 (DESIGN §6.2 개정 2026-07-22)."""
from datetime import time

import pytest

from kp_arb.domain.enums import Instrument, Side, Venue
from kp_arb.strategy_core import (
    Block,
    ScreenKind,
    ScreenState,
    allowed_order_qty,
    hl_qty_for,
    in_operating_window,
    plan_order,
    taker_price,
    threshold_check,
    validate_run,
)

STOCK = Instrument.KR_STOCK
SF = Instrument.KR_STOCK_FUTURE
NOON = time(12, 0)


def _screen(kind: ScreenKind = ScreenKind.AUTO_M, *, per: int = 5,
            max_pos: int = 100) -> ScreenState:
    screen = ScreenState(kind=kind, per_order_qty=per)
    screen.settings.max_position = max_pos
    for i, value in enumerate((0.006, 0.008, 0.012)):
        screen.entry_sets[i].threshold = value
        screen.entry_sets[i].target_qty = 100
    for i, value in enumerate((0.0, -0.003, -0.005)):
        screen.exit_sets[i].threshold = value
        screen.exit_sets[i].target_qty = 100
    return screen


# --- 기준값 가드 (§6.2-6) ---

def test_threshold_guards() -> None:
    assert threshold_check(Block.ENTRY, 0.006) == ([], [])
    assert threshold_check(Block.ENTRY, 0.0)[1] == ["낮은수치"]      # 경고만
    assert threshold_check(Block.ENTRY, -0.01)[0] != []              # -1% 이하 불가
    assert threshold_check(Block.EXIT, -0.003) == ([], [])
    assert threshold_check(Block.EXIT, 0.0)[1] == ["높은수치"]
    assert threshold_check(Block.EXIT, 0.01)[0] != []                # +1% 이상 불가


def test_validate_run() -> None:
    screen = _screen()
    assert validate_run(screen, Block.ENTRY, 0) == []
    screen.per_order_qty = 0
    assert validate_run(screen, Block.ENTRY, 0) != []
    screen = _screen(max_pos=0)
    assert any("보유최대" in e for e in validate_run(screen, Block.ENTRY, 0))
    screen = _screen()
    screen.entry_sets[0].threshold = None
    assert any("기준값" in e for e in validate_run(screen, Block.ENTRY, 0))


# --- 운영시간 (§6.2-1) ---

def test_operating_windows() -> None:
    # 자동T: 8:00~8:50 / 9:00~15:30 / 15:40~20:00
    assert in_operating_window(ScreenKind.AUTO_T, time(8, 0))
    assert not in_operating_window(ScreenKind.AUTO_T, time(8, 55))
    assert in_operating_window(ScreenKind.AUTO_T, time(15, 40))
    assert not in_operating_window(ScreenKind.AUTO_T, time(20, 0))
    # 자동M: 8:45~15:35
    assert in_operating_window(ScreenKind.AUTO_M, time(8, 45))
    assert not in_operating_window(ScreenKind.AUTO_M, time(15, 35))


# --- 수량 (§6.2-3) ---

def test_hl_qty_ratio() -> None:
    assert hl_qty_for(STOCK, 30) == 30    # 주식 1주 = HL 1계약
    assert hl_qty_for(SF, 5) == 50        # SF 1계약 = HL 10계약


def test_limits_stock_and_futures() -> None:
    # 주식: 0~최대 (공매도 금지), 선물: 양방향 |포지션| ≤ 최대
    assert allowed_order_qty(Block.ENTRY, STOCK, 90, 30, 100) == 10
    assert allowed_order_qty(Block.EXIT, STOCK, 0, 30, 100) == 0
    assert allowed_order_qty(Block.EXIT, STOCK, 7, 30, 100) == 7
    assert allowed_order_qty(Block.EXIT, SF, 0, 30, 100) == 30    # ex부터 = 숏 진입
    assert allowed_order_qty(Block.EXIT, SF, -90, 30, 100) == 10
    assert allowed_order_qty(Block.ENTRY, SF, -100, 30, 100) == 30


# --- 주문 계획 (§6.2-1·2) ---

def test_plan_order_auto_m_legs() -> None:
    # 자동M entry: LS SF 매수 5계약 + HL 매도 50계약
    plan, errors = plan_order(_screen(), Block.ENTRY, 0, position_qty=0, now=NOON)
    assert errors == [] and plan is not None
    assert plan.legs[0].venue is Venue.LS and plan.legs[0].side is Side.BUY
    assert plan.legs[0].qty == 5
    assert plan.legs[1].venue is Venue.HYPERLIQUID and plan.legs[1].side is Side.SELL
    assert plan.legs[1].qty == 50


def test_plan_order_ls_unchecked_hl_only() -> None:
    # 세트별 LS주문 체크 해제 → 그 세트만 HL 다리 (§6.2-2, 사용자 확정: 세트 단위)
    screen = _screen()
    screen.entry_sets[0].ls_order = False
    plan, errors = plan_order(screen, Block.ENTRY, 0, position_qty=0, now=NOON)
    assert errors == [] and plan is not None
    assert len(plan.legs) == 1 and plan.legs[0].venue is Venue.HYPERLIQUID
    # 다른 세트는 영향 없음
    plan, errors = plan_order(screen, Block.ENTRY, 1, position_qty=0, now=NOON)
    assert plan is not None and len(plan.legs) == 2


def test_plan_order_rejections() -> None:
    screen = _screen()
    # 운영시간 밖 (자동M은 8:45~15:35)
    plan, errors = plan_order(screen, Block.ENTRY, 0, position_qty=0, now=time(16, 0))
    assert plan is None and any("운영시간" in e for e in errors)
    # 목표 완료
    screen.entry_sets[0].fired_qty = 100
    plan, errors = plan_order(screen, Block.ENTRY, 0, position_qty=0, now=NOON)
    assert plan is None and any("목표" in e for e in errors)
    # 한도 소진 (보유최대수량 도달)
    screen = _screen()
    plan, errors = plan_order(screen, Block.ENTRY, 0, position_qty=100, now=NOON)
    assert plan is None and any("수량 없음" in e for e in errors)


def test_plan_order_caps_to_remaining_target() -> None:
    # 목표 잔여가 1회주문수량보다 작으면 잔여만 주문
    screen = _screen(per=30)
    screen.entry_sets[0].fired_qty = 90  # 목표 100 → 잔여 10
    plan, errors = plan_order(screen, Block.ENTRY, 0, position_qty=0, now=NOON)
    assert errors == [] and plan is not None
    assert plan.legs[0].qty == 10


def test_taker_price_margin() -> None:
    # 매수 = est-pr 위로, 매도 = 아래로 (§6.2-4). 호가단위 반올림은 실행층.
    assert taker_price(100.0, Side.BUY, 0.01) == pytest.approx(101.0)
    assert taker_price(100.0, Side.SELL, 0.01) == pytest.approx(99.0)
