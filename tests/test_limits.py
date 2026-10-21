"""일일 HL 체결액 한도 — 순수 로직 (DESIGN-settings §1)."""
from kp_arb.domain.enums import Side
from kp_arb.limits import (
    DailyFilled,
    price_limit_error,
    qty_limit_error,
    would_exceed_daily_limit,
)


def test_would_exceed_respects_limit() -> None:
    assert would_exceed_daily_limit(900.0, 200.0, 1000.0)       # 900+200 > 1000 → 거부
    assert not would_exceed_daily_limit(700.0, 200.0, 1000.0)   # 900 < 1000 → 허용
    assert not would_exceed_daily_limit(0.0, 5000.0, 0.0)       # 한도 0 = 무제한
    assert not would_exceed_daily_limit(0.0, 5000.0, -1.0)      # 음수 = 무제한


def test_would_exceed_boundary_is_inclusive() -> None:
    assert not would_exceed_daily_limit(500.0, 500.0, 1000.0)   # 정확히 한도 = 허용
    assert would_exceed_daily_limit(500.0, 500.01, 1000.0)      # 살짝 넘음 = 거부


def test_daily_filled_accumulates_within_day() -> None:
    acc = DailyFilled()
    assert acc.total("20260820") == 0.0
    assert acc.add("20260820", 300.0) == 300.0
    assert acc.add("20260820", 200.0) == 500.0
    assert acc.total("20260820") == 500.0


def test_daily_filled_resets_on_new_day() -> None:
    acc = DailyFilled()
    acc.add("20260820", 500.0)
    assert acc.total("20260821") == 0.0        # 날 바뀜 → 리셋
    acc.add("20260821", 100.0)
    assert acc.total("20260821") == 100.0
    assert acc.total("20260820") == 0.0        # 과거 날짜는 유지 안 함


def test_price_limit_by_side_and_oracle() -> None:
    # DESIGN-settings §1a(사용자 2026-10-20): 매도 주문가 > 오라클×(1−x), 매수 주문가 < 오라클×(1+x)
    assert price_limit_error(Side.SELL, 99.0, 100.0, 0.02) is None      # 99 > 98
    assert price_limit_error(Side.SELL, 98.0, 100.0, 0.02) is not None  # 98 ≤ 98 거부(경계 포함)
    assert price_limit_error(Side.BUY, 101.0, 100.0, 0.02) is None      # 101 < 102
    assert price_limit_error(Side.BUY, 102.0, 100.0, 0.02) is not None  # 102 ≥ 102 거부
    assert price_limit_error(Side.BUY, 150.0, 100.0, 0.0) is None       # 0 = 검사 없음
    # 오라클이 없으면 거부(사용자 "오라클은 받아야지"), 가격 없음도 거부
    assert "오라클" in str(price_limit_error(Side.SELL, 99.0, None, 0.02))
    assert "가격 없음" in str(price_limit_error(Side.SELL, None, 100.0, 0.02))
    assert price_limit_error(Side.SELL, None, None, 0.0) is None


def test_qty_limit_is_exclusive_of_limit_value() -> None:
    assert qty_limit_error(99, 100) is None           # 한도 미만만 허용
    assert qty_limit_error(100, 100) is not None      # 한도 값부터 거부(엑셀 "이상")
    assert qty_limit_error(101, 100) is not None
    assert qty_limit_error(-101, 100) is not None     # 부호 무관(절댓값)
    assert qty_limit_error(10_000, 0) is None         # 0 = 없음
