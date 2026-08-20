"""일일 HL 체결액 한도 — 순수 로직 (DESIGN-settings §1)."""
from kp_arb.limits import DailyFilled, would_exceed_daily_limit


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
