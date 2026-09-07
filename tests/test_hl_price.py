"""HL 주문가 격자 맞춤(hl_price) — 유효숫자 5·소수 6−szDecimals, 매수 올림/매도 내림."""
from __future__ import annotations

from kp_arb.domain.enums import Side
from kp_arb.hl_price import hl_price_decimals, hl_round_price


def test_decimals_by_significant_figures_and_sz_decimals() -> None:
    assert hl_price_decimals(197.48, 1) == 2       # 유효숫자 5 → 소수 2
    assert hl_price_decimals(1281.06, 1) == 1      # 4자리 정수부 → 소수 1
    assert hl_price_decimals(12.345, 1) == 3
    assert hl_price_decimals(0.012345, 0) == 6     # 소수 한도 6 − 0 (유효숫자로는 7)
    assert hl_price_decimals(0.012345, 2) == 4     # 6 − 2
    assert hl_price_decimals(123456.0, 1) == 0     # 정수는 항상 허용


def test_round_is_aggressive_per_side() -> None:
    # 실측 2026-09-07: SKHX 매도 후주문 1281.06(6자리) → 거부. 매도는 내림 → 1281.0
    assert hl_round_price(1281.06, Side.SELL, 1) == 1281.0
    assert hl_round_price(1281.06, Side.BUY, 1) == 1281.1
    assert hl_round_price(197.4831, Side.BUY, 1) == 197.49
    assert hl_round_price(197.4831, Side.SELL, 1) == 197.48
    assert hl_round_price(197.48, Side.SELL, 1) == 197.48   # 이미 격자 위면 그대로
    assert hl_round_price(999.999, Side.BUY, 1) == 1000.0   # 자릿수 올라가도 정수라 허용
    assert hl_round_price(0.0, Side.BUY, 1) == 0.0
