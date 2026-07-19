"""캐리 이론가·만기 계산 테스트 — 순수 로직."""
from datetime import date, datetime, time

import pytest

from kp_arb.theory import (
    carry_theory,
    days_to_expiry,
    expiry_date,
    in_time_window,
    is_rolled,
    parse_hhmm,
    parse_ym,
    select_usd_futures,
)


def test_fx_spot_window_boundaries() -> None:
    # 외환현물 창(07:50~18:10): 시작 포함, 끝 미포함.
    start, end = parse_hhmm("07:50"), parse_hhmm("18:10")
    assert not in_time_window(time(7, 49), start, end)
    assert in_time_window(time(7, 50), start, end)
    assert in_time_window(time(18, 9, 59), start, end)
    assert not in_time_window(time(18, 10), start, end)


def test_time_window_over_midnight() -> None:
    # start > end면 자정을 넘는 창 (야간 세션 대비).
    start, end = parse_hhmm("18:00"), parse_hhmm("02:00")
    assert in_time_window(time(23, 0), start, end)
    assert in_time_window(time(1, 0), start, end)
    assert not in_time_window(time(10, 0), start, end)


def test_expiry_eq_second_thursday() -> None:
    # 2026-07: 첫 목요일 7/2 → 둘째 목요일 7/9.
    assert expiry_date(202607, "EQ") == date(2026, 7, 9)


def test_expiry_usd_third_monday() -> None:
    # 2026-07: 월요일 6, 13, 20 → 셋째 월요일 7/20.
    assert expiry_date(202607, "USD") == date(2026, 7, 20)


def test_days_to_expiry_min_one() -> None:
    assert days_to_expiry(202607, "USD", date(2026, 7, 7)) == 13
    assert days_to_expiry(202607, "USD", date(2026, 7, 25)) == 1  # 지났어도 최소 1


def test_is_rolled_at_1545_on_expiry() -> None:
    assert not is_rolled(202607, "EQ", datetime(2026, 7, 9, 15, 44))
    assert is_rolled(202607, "EQ", datetime(2026, 7, 9, 15, 45))
    assert is_rolled(202607, "EQ", datetime(2026, 7, 10, 9, 0))


def test_carry_theory() -> None:
    # 100,000원, 잔존 73일, 연 3.5% → ×(1 + 0.035×0.2) = 100,700.
    assert carry_theory(100_000, 73, 0.035) == pytest.approx(100_700.0)


def test_parse_ym() -> None:
    assert parse_ym("미국달러    F 202608") == 202608
    assert parse_ym("F 2608") == 202608
    assert parse_ym("이상한이름") is None


def test_select_usd_futures_near_month_skips_rolled_and_spread() -> None:
    rows: list[dict[str, object]] = [
        {"hname": "미국달러    F 202607", "shcode": "175W07"},
        {"hname": "미국달러    F 202608", "shcode": "175W08"},
        {"hname": "미국달러 SP F 202607-202608", "shcode": "475W07"},  # 스프레드 제외
        {"hname": "금         F 202608", "shcode": "167W08"},          # 타상품 제외
    ]
    # 7월물 만기(7/20) 전 → 7월물.
    assert select_usd_futures(rows, datetime(2026, 7, 7, 10, 0)) == ("175W07", 202607)
    # 만기일 15:45 이후 → 8월물로 롤.
    assert select_usd_futures(rows, datetime(2026, 7, 20, 16, 0)) == ("175W08", 202608)
