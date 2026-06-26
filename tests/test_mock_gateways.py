import pytest

from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent
from kp_arb.gateways.mock_hl import MockHLGateway
from kp_arb.gateways.mock_ls import MockLSGateway


async def test_ls_routes_etf_to_stock_account() -> None:
    gw = MockLSGateway()
    await gw.connect()
    assert gw.connected
    oi = OrderIntent(
        venue=Venue.LS,
        underlying=Underlying.SAMSUNG,
        instrument=Instrument.KR_ETF,
        side=Side.BUY,
        qty=10,
        order_type=OrderType.MARKET,
    )
    oid = await gw.place_order(oi)
    assert oid.startswith("LS-")
    assert gw.placed[0].account == Account.KR_STOCK


async def test_ls_routes_future_to_deriv_account() -> None:
    gw = MockLSGateway()
    oi = OrderIntent(
        venue=Venue.LS,
        underlying=Underlying.SK_HYNIX,
        instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL,
        qty=2,
        order_type=OrderType.MARKET,
    )
    await gw.place_order(oi)
    assert gw.placed[0].account == Account.KR_DERIV


async def test_ls_rejects_hl_order() -> None:
    gw = MockLSGateway()
    oi = OrderIntent(
        venue=Venue.HYPERLIQUID,
        underlying=Underlying.SAMSUNG,
        instrument=Instrument.HL_PERP,
        side=Side.BUY,
        qty=1,
        order_type=OrderType.MARKET,
    )
    with pytest.raises(ValueError):
        await gw.place_order(oi)


async def test_hl_records_perp_order() -> None:
    gw = MockHLGateway()
    await gw.connect()
    oi = OrderIntent(
        venue=Venue.HYPERLIQUID,
        underlying=Underlying.HYUNDAI,
        instrument=Instrument.HL_PERP,
        side=Side.SELL,
        qty=3,
        order_type=OrderType.MARKET,
    )
    oid = await gw.place_order(oi)
    assert oid.startswith("HL-")
    assert gw.placed[0].instrument is Instrument.HL_PERP
