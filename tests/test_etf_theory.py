"""ETF 이론가(정규장 공식) 테스트 — 순수 로직만 (문서: ETF 이론가.md)."""
import pytest

from kp_arb.etf_theory import EtfTheoryInputs, disparity_pct, theory_price

INPUTS = EtfTheoryInputs(
    prev_nav=10_000.0, leverage=2.0, base_prev_close=70_000.0, exchange_inav=10_050.0
)


def test_theory_regular_session_formula() -> None:
    # 기초 +1% → 2배 ETF 이론가 +2%.
    assert theory_price(INPUTS, 70_700.0) == pytest.approx(10_200.0)


def test_theory_negative_return() -> None:
    # 기초 -2% → -4%.
    assert theory_price(INPUTS, 68_600.0) == pytest.approx(9_600.0)


def test_theory_inverse_leverage() -> None:
    # 인버스(음수 배율): 기초 +1% → -2% (예: 252670 = -2.0, 문서 §1).
    inverse = EtfTheoryInputs(prev_nav=10_000.0, leverage=-2.0, base_prev_close=70_000.0)
    assert theory_price(inverse, 70_700.0) == pytest.approx(9_800.0)


def test_theory_fallback_to_exchange_inav_then_prev_nav() -> None:
    # 기초가 없으면: 거래소 공식 iNAV → 그것도 없으면 전일NAV (문서 §1 대체 순서).
    assert theory_price(INPUTS, None) == pytest.approx(10_050.0)
    no_inav = EtfTheoryInputs(prev_nav=10_000.0, leverage=2.0, base_prev_close=70_000.0)
    assert theory_price(no_inav, None) == pytest.approx(10_000.0)


def test_theory_none_when_no_inputs() -> None:
    assert theory_price(None, 70_000.0) is None


def test_disparity_pct() -> None:
    assert disparity_pct(10_200.0, 10_000.0) == pytest.approx(2.0)
    assert disparity_pct(None, 10_000.0) is None
    assert disparity_pct(10_000.0, None) is None
