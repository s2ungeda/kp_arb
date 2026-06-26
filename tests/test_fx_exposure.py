from kp_arb.domain.enums import Account, Instrument, Side, Underlying, Venue
from kp_arb.domain.models import Position
from kp_arb.fx import usd_exposure


def _pos(
    instrument: Instrument,
    underlying: Underlying,
    side: Side,
    qty: float,
    price: float,
    account: Account | None = None,
) -> Position:
    venue = Venue.HYPERLIQUID if instrument is Instrument.HL_PERP else Venue.LS
    return Position(
        venue=venue,
        instrument=instrument,
        underlying=underlying,
        side=side,
        qty=qty,
        avg_price=price,
        account=account,
    )


def test_net_usd_exposure() -> None:
    positions = [
        _pos(Instrument.HL_PERP, Underlying.SAMSUNG, Side.BUY, 10, 200.0),    # +2000
        _pos(Instrument.HL_PERP, Underlying.SK_HYNIX, Side.SELL, 5, 1300.0),  # -6500
    ]
    assert usd_exposure(positions) == 2000.0 - 6500.0


def test_ignores_domestic_positions() -> None:
    positions = [
        _pos(Instrument.KR_STOCK, Underlying.SAMSUNG, Side.BUY, 100, 70000.0, Account.KR_STOCK),
        _pos(Instrument.HL_PERP, Underlying.SAMSUNG, Side.SELL, 10, 200.0),
    ]
    assert usd_exposure(positions) == -2000.0


def test_mark_override_used() -> None:
    positions = [_pos(Instrument.HL_PERP, Underlying.SAMSUNG, Side.BUY, 10, 200.0)]
    assert usd_exposure(positions, {Underlying.SAMSUNG: 210.0}) == 2100.0
