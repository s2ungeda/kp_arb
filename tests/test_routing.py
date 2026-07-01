import pytest

from kp_arb.domain.enums import Account, Instrument
from kp_arb.routing import account_for


@pytest.mark.parametrize(
    "instrument,expected",
    [
        (Instrument.KR_STOCK, Account.KR_STOCK),
        (Instrument.KR_ETF, Account.KR_STOCK),
        (Instrument.KR_STOCK_FUTURE, Account.KR_DERIV),
    ],
)
def test_account_routing(instrument: Instrument, expected: Account) -> None:
    assert account_for(instrument) == expected


def test_hl_perp_has_no_ls_account() -> None:
    with pytest.raises(ValueError):
        account_for(Instrument.HL_PERP)
