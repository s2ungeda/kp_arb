"""호가단위·틱 반올림·maker 보정 테스트 — 순수 로직 (DESIGN §6.2-4)."""
from kp_arb.domain.enums import Instrument, Side
from kp_arb.ticks import (
    ceil_to_tick,
    floor_to_tick,
    maker_cap,
    stock_futures_tick,
    stock_tick,
    tick_for,
)


def test_stock_tick_bands() -> None:
    assert stock_tick(1_500) == 1
    assert stock_tick(3_000) == 5
    assert stock_tick(10_000) == 10
    assert stock_tick(30_000) == 50
    assert stock_tick(150_000) == 100
    assert stock_tick(293_000) == 500   # 삼성전자 가격대
    assert stock_tick(600_000) == 1_000


def test_futures_tick_bands() -> None:
    assert stock_futures_tick(5_000) == 10
    assert stock_futures_tick(30_000) == 50
    assert stock_futures_tick(70_000) == 100
    assert stock_futures_tick(293_000) == 500  # 삼성선물 가격대
    assert stock_futures_tick(600_000) == 1_000
    assert tick_for(Instrument.KR_STOCK_FUTURE, 293_000) == 500
    assert tick_for(Instrument.KR_STOCK, 293_000) == 500


def test_tick_rounding() -> None:
    assert floor_to_tick(293_740.0, 500) == 293_500
    assert ceil_to_tick(293_740.0, 500) == 294_000
    assert floor_to_tick(293_500.0, 500) == 293_500  # 경계는 그대로
    assert ceil_to_tick(293_500.0, 500) == 293_500


def test_maker_cap() -> None:
    # 진입(매수): 주문가 ≥ 매도1호가 → 매도1호가 − 1틱 (사용자 확정)
    assert maker_cap(Side.BUY, 294_000, 293_500, 293_000, 500) == 293_000
    assert maker_cap(Side.BUY, 293_000, 293_500, 293_000, 500) == 293_000  # 침범 없음
    # 청산(매도): 주문가 ≤ 매수1호가 → 매수1호가 + 1틱
    assert maker_cap(Side.SELL, 292_500, 293_500, 293_000, 500) == 293_500
    assert maker_cap(Side.SELL, 294_000, 293_500, 293_000, 500) == 294_000
    # 1호가 미수신이면 보정 없이 그대로
    assert maker_cap(Side.BUY, 294_000, None, None, 500) == 294_000
