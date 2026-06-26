from kp_arb.domain.enums import Instrument, SessionPhase
from kp_arb.session import build_session, reference_instrument, tradeable_instruments


def test_regular_session() -> None:
    s = build_session(SessionPhase.REGULAR)
    t = tradeable_instruments(s)
    assert Instrument.KR_STOCK in t
    assert Instrument.KR_ETF in t
    assert Instrument.KR_STOCK_FUTURE in t
    assert Instrument.KR_NIGHT_FUTURE not in t
    assert reference_instrument(s) == Instrument.KR_STOCK


def test_night_session() -> None:
    s = build_session(SessionPhase.NIGHT_DERIV)
    assert tradeable_instruments(s) == {Instrument.KR_NIGHT_FUTURE}
    assert reference_instrument(s) == Instrument.KR_NIGHT_FUTURE


def test_deadzone_has_nothing() -> None:
    s = build_session(SessionPhase.DEAD)
    assert tradeable_instruments(s) == set()
    assert reference_instrument(s) is None


def test_holiday_overrides_regular() -> None:
    s = build_session(SessionPhase.REGULAR, is_holiday=True)
    assert tradeable_instruments(s) == set()
    assert reference_instrument(s) is None
