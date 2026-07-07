"""ETF 이론가(등락률 기반, 문서 §1·§2) 테스트 — 순수 로직만."""
import pytest

from kp_arb.etf_theory import (
    EtfTheoryInputs,
    disparity_pct,
    theory_after,
    theory_regular,
)

INPUTS = EtfTheoryInputs(prev_nav=10_000.0, leverage=2.0, exchange_inav=10_050.0)


def test_regular_uses_base_rate() -> None:
    # 기초 KRX 등락률(drate) +1% → 2배 ETF 이론가 +2%.
    assert theory_regular(INPUTS, 1.0) == pytest.approx(10_200.0)
    assert theory_regular(INPUTS, -2.0) == pytest.approx(9_600.0)


def test_regular_inverse_leverage() -> None:
    # 인버스(음수 배율): 기초 +1% → -2% (예: 252670 = -2.0).
    inverse = EtfTheoryInputs(prev_nav=10_000.0, leverage=-2.0)
    assert theory_regular(inverse, 1.0) == pytest.approx(9_800.0)


def test_regular_fallback_chain() -> None:
    # 등락률 미수신 → 거래소 공식 iNAV → 그것도 없으면 전일NAV (문서 §1 대체 순서).
    assert theory_regular(INPUTS, None) == pytest.approx(10_050.0)
    no_inav = EtfTheoryInputs(prev_nav=10_000.0, leverage=2.0)
    assert theory_regular(no_inav, None) == pytest.approx(10_000.0)
    assert theory_regular(None, 1.0) is None


def test_after_session_formula() -> None:
    # 애프터: 당일종가NAV(종가 등락 +1% → 10,200) × (1 + 2 × 애프터 +1%) = 10,404.
    got = theory_after(INPUTS, 1.0, 70_700.0, 71_407.0)
    assert got == pytest.approx(10_200.0 * 1.02)


def test_after_without_after_trade_is_close_nav() -> None:
    # 애프터 체결이 아직 없으면 당일종가NAV 그대로.
    assert theory_after(INPUTS, 1.0, 70_700.0, None) == pytest.approx(10_200.0)


def test_disparity_pct() -> None:
    assert disparity_pct(10_200.0, 10_000.0) == pytest.approx(2.0)
    assert disparity_pct(None, 10_000.0) is None
    assert disparity_pct(10_000.0, None) is None
