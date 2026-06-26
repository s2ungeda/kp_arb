import math

import pytest

from kp_arb.spread import compute_spread, fair_kr_price


def test_fair_price() -> None:
    assert fair_kr_price(100.0, 1400.0, 0.5) == pytest.approx(70000.0)


def test_spread_positive_when_kr_rich() -> None:
    p = compute_spread(71000.0, 100.0, 1400.0, 0.5)
    assert p > 0


def test_spread_negative_when_kr_cheap() -> None:
    p = compute_spread(69000.0, 100.0, 1400.0, 0.5)
    assert p < 0


def test_spread_zero_at_fair() -> None:
    p = compute_spread(70000.0, 100.0, 1400.0, 0.5)
    assert math.isclose(p, 0.0, abs_tol=1e-9)


def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        compute_spread(0.0, 100.0, 1400.0, 0.5)
    with pytest.raises(ValueError):
        compute_spread(70000.0, -1.0, 1400.0, 0.5)
