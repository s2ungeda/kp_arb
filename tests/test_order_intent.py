import pytest

from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent


def test_ls_intent_auto_routes_account() -> None:
    oi = OrderIntent(
        venue=Venue.LS,
        underlying=Underlying.SAMSUNG,
        instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL,
        qty=1,
        order_type=OrderType.MARKET,
    )
    assert oi.account == Account.KR_DERIV


def test_ls_intent_wrong_account_rejected() -> None:
    with pytest.raises(ValueError):
        OrderIntent(
            venue=Venue.LS,
            underlying=Underlying.SAMSUNG,
            instrument=Instrument.KR_STOCK,
            side=Side.BUY,
            qty=1,
            order_type=OrderType.MARKET,
            account=Account.KR_DERIV,
        )


def test_hl_intent_must_not_have_account() -> None:
    with pytest.raises(ValueError):
        OrderIntent(
            venue=Venue.HYPERLIQUID,
            underlying=Underlying.SAMSUNG,
            instrument=Instrument.HL_PERP,
            side=Side.SELL,
            qty=1,
            order_type=OrderType.MARKET,
            account=Account.KR_STOCK,
        )


def test_limit_requires_price() -> None:
    with pytest.raises(ValueError):
        OrderIntent(
            venue=Venue.HYPERLIQUID,
            underlying=Underlying.SAMSUNG,
            instrument=Instrument.HL_PERP,
            side=Side.BUY,
            qty=1,
            order_type=OrderType.LIMIT,
        )


def test_qty_must_be_positive() -> None:
    with pytest.raises(ValueError):
        OrderIntent(
            venue=Venue.HYPERLIQUID,
            underlying=Underlying.SAMSUNG,
            instrument=Instrument.HL_PERP,
            side=Side.BUY,
            qty=0,
            order_type=OrderType.MARKET,
        )


def test_venue_instrument_mismatch_rejected() -> None:
    with pytest.raises(ValueError):
        OrderIntent(
            venue=Venue.LS,
            underlying=Underlying.SAMSUNG,
            instrument=Instrument.HL_PERP,
            side=Side.BUY,
            qty=1,
            order_type=OrderType.MARKET,
        )
