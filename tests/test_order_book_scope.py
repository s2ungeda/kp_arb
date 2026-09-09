"""OrderBook.load_snapshot scope — 한 시장의 재동기가 다른 시장 장부를 건드리지 않는다."""
from __future__ import annotations

import time

from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent, Position
from kp_arb.order_book import OrderBook


def _pos(inst: Instrument, account: Account | None, qty: int, venue: Venue) -> Position:
    return Position(venue=venue, instrument=inst, underlying=Underlying.SAMSUNG, side=Side.BUY,
                    qty=qty, avg_price=100.0, account=account)


def _intent(inst: Instrument, account: Account | None, venue: Venue) -> OrderIntent:
    return OrderIntent(venue=venue, underlying=Underlying.SAMSUNG, instrument=inst,
                       side=Side.BUY, qty=1, order_type=OrderType.LIMIT, price=100.0,
                       account=account)


def test_scoped_snapshot_replaces_only_that_market() -> None:
    # 실측 2026-09-09: HL 재연결이 LS까지 전체 재동기 → 선물 선주문이 유령으로 지워짐.
    ob = OrderBook()
    ob.load_snapshot(
        positions=[_pos(Instrument.KR_STOCK, Account.KR_STOCK, 10, Venue.LS),
                   _pos(Instrument.KR_STOCK_FUTURE, Account.KR_DERIV, 2, Venue.LS),
                   _pos(Instrument.HL_PERP, None, 5, Venue.HYPERLIQUID)],
        balances={Account.KR_STOCK: 1000.0, Account.KR_DERIV: 500.0})
    fut = ob.track("D1", _intent(Instrument.KR_STOCK_FUTURE, Account.KR_DERIV, Venue.LS))
    fut.placed_ts = time.monotonic() - 60  # 유예 지난 옛 주문
    # HL만 재동기(scope={None}): HL 포지션은 새 값(3), LS 포지션·잔고·선물 주문은 그대로
    ob.load_snapshot(positions=[_pos(Instrument.HL_PERP, None, 3, Venue.HYPERLIQUID)],
                     open_orders=(), reconcile_accounts={None}, scope={None})
    assert ob.position_qty(Underlying.SAMSUNG, Instrument.HL_PERP, None) == 3
    assert ob.position_qty(Underlying.SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 10
    assert ob.position_qty(Underlying.SAMSUNG, Instrument.KR_STOCK_FUTURE, Account.KR_DERIV) == 2
    assert ob.balance(Account.KR_DERIV) == 500.0
    assert ob.order("D1") is not None
    # 전체 재동기(scope 없음)는 옛 동작 — 안 준 포지션은 사라진다
    ob.load_snapshot(positions=[_pos(Instrument.KR_STOCK, Account.KR_STOCK, 7, Venue.LS)],
                     balances={Account.KR_STOCK: 900.0})
    assert ob.position_qty(Underlying.SAMSUNG, Instrument.HL_PERP, None) == 0
    assert ob.position_qty(Underlying.SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 7
