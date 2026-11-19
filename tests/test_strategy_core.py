"""주문 화면 코어 테스트 — 검증·한도·환산·주문 계획 (DESIGN §6.2 개정 2026-07-22)."""
from datetime import time

import pytest

from kp_arb.domain.enums import Instrument, Side, Venue
from kp_arb.strategy_core import (
    Block,
    HlPriorityFee,
    PriorityWindow,
    ScreenKind,
    ScreenState,
    allowed_order_qty,
    apply_hl_priority_fee,
    hl_qty_for,
    in_operating_window,
    plan_order,
    priority_p,
    priority_window_for,
    taker_price,
    validate_run,
)

STOCK = Instrument.KR_STOCK
SF = Instrument.KR_STOCK_FUTURE
NOON = time(12, 0)


def _screen(kind: ScreenKind = ScreenKind.AUTO_M, *, per: int = 5,
            max_pos: int = 100) -> ScreenState:
    screen = ScreenState(kind=kind, entry_per_qty=per, exit_per_qty=per)
    screen.settings.max_position = max_pos
    for i, value in enumerate((0.006, 0.008, 0.012)):
        screen.entry_sets[i].threshold = value
        screen.entry_sets[i].target_qty = 100
    for i, value in enumerate((0.0, -0.003, -0.005)):
        screen.exit_sets[i].threshold = value
        screen.exit_sets[i].target_qty = 100
    return screen


# --- 실행 전 검증 (기준값 가드는 제거 — 자유 입력, 사용자 확정 2026-07-24) ---

def test_validate_run() -> None:
    screen = _screen()
    assert validate_run(screen, Block.ENTRY, 0) == []
    screen.entry_per_qty = 0
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
    # 세트별 LS주문 체크 해제 → 그 세트만 HL 주문 (§6.2-2, 사용자 확정: 세트 단위)
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


def test_operating_hours_override() -> None:
    # 설정 운영시간이 있으면 그걸로 판정, 없거나 형식 오류면 기본값 (§6.2-1)
    from kp_arb.strategy_core import in_screen_operating_window, parse_operating_hours

    assert parse_operating_hours("08:00-08:50, 10:00-11:00") == (
        ("08:00", "08:50"), ("10:00", "11:00"))
    assert parse_operating_hours("") == ()

    screen = _screen(ScreenKind.AUTO_M)
    assert not in_screen_operating_window(screen, time(16, 0))  # 기본 8:45~15:35 밖
    screen.settings.operating_hours = "15:50-17:00"
    assert in_screen_operating_window(screen, time(16, 0))      # 덮어쓰기 적용
    assert not in_screen_operating_window(screen, time(12, 0))
    screen.settings.operating_hours = "broken"
    assert in_screen_operating_window(screen, time(12, 0))      # 형식 오류 → 기본값


def test_hl_priority_fee_windows_apply_and_judge() -> None:
    # DESIGN-hl-priority-fee §2(사용자 2026-11-17): 시간구간 4개(체크·시작·종료·bp), 체크된 첫 구간
    # [start, end) 안이면 그 구간, 아니면 None(= GTC). p = bp × 10,000, bp 0이면 None(IOC만)
    fee = HlPriorityFee()
    assert len(fee.windows) == 4 and not any(w.enabled for w in fee.windows)
    apply_hl_priority_fee(fee, {"windows": [
        {"enabled": True, "start": " 08:00:00 ", "end": "08:05:00", "bp": 2},
        {"enabled": False, "start": "", "end": "", "bp": 0},
        {"enabled": True, "start": "09:00:00", "end": "09:01:00", "bp": 0.5}]})
    assert fee.windows[0] == PriorityWindow(True, "08:00:00", "08:05:00", 2.0)
    assert fee.windows[3] == PriorityWindow()                    # 모자란 구간은 빈 구간
    assert priority_window_for(time(8, 0, 0), fee.windows) is fee.windows[0]
    assert priority_window_for(time(8, 4, 59), fee.windows) is fee.windows[0]
    assert priority_window_for(time(8, 5, 0), fee.windows) is None  # 종료는 열린 끝
    assert priority_window_for(time(9, 0, 30), fee.windows) is fee.windows[2]
    assert priority_window_for(time(12, 0), fee.windows) is None
    # 겹치면 위 구간이 이긴다
    fee.windows[1] = PriorityWindow(True, "08:00:00", "08:10:00", 9.0)
    assert priority_window_for(time(8, 1), fee.windows) is fee.windows[0]
    assert priority_window_for(time(8, 7), fee.windows) is fee.windows[1]
    assert priority_p(2.0) == 20_000 and priority_p(0.5) == 5_000 and priority_p(0.0) is None
    # 거부: 체크 구간의 시각 형식·순서, 음수 bp, bp > 10(사용자 2026-11-18 "최대 10까지"), 구간 5개,
    # dict 아님. 키 없음·dict 아님은 그대로
    for bad in ({"windows": [{"enabled": True, "start": "8:00", "end": "08:05:00", "bp": 1}]},
                {"windows": [{"enabled": True, "start": "09:00:00", "end": "08:05:00", "bp": 1}]},
                {"windows": [{"enabled": False, "start": "", "end": "", "bp": -1}]},
                {"windows": [{"enabled": False, "start": "", "end": "", "bp": 10.01}]},
                {"windows": [{} for _ in range(5)]},
                {"windows": "x"}):
        with pytest.raises(ValueError):
            apply_hl_priority_fee(fee, bad)
    before = [PriorityWindow(**vars(w)) for w in fee.windows]
    apply_hl_priority_fee(fee, None)
    assert fee.windows == before
    apply_hl_priority_fee(fee, {"windows": []})                 # 빈 목록 = 전부 빈 구간(GTC)
    assert priority_window_for(time(8, 1), fee.windows) is None
    # 검토 2026-11-17 M6·L4: NaN·inf 거부(다른 클라이언트 JSON), 시각은 두 자리씩만
    for bad in ({"windows": [{"enabled": False, "start": "", "end": "", "bp": float("nan")}]},
                {"windows": [{"enabled": False, "start": "", "end": "", "bp": float("inf")}]},
                {"windows": [{"enabled": True, "start": "8:0:0", "end": "08:05:00", "bp": 1}]}):
        with pytest.raises(ValueError):
            apply_hl_priority_fee(fee, bad)
