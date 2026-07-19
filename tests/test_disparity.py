"""상대호가 괴리·스프레드 테스트 — 순수 로직 (엑셀 IM.xlsx 수식 대조)."""
import pytest

from kp_arb.disparity import PairSpread, SideDisp, disp, pair_spread, side_disp


def test_disp_basic() -> None:
    # 엑셀 AE7: (환산 매도호가 − 주식현재가) ÷ 주식현재가.
    assert disp(2_429_199.58, 2_436_000) == pytest.approx(-0.0027916, abs=1e-6)
    assert disp(None, 2_436_000) is None
    assert disp(2_429_199.58, None) is None
    assert disp(2_429_199.58, 0) is None


def test_side_disp() -> None:
    s = side_disp(101.0, 99.0, 100.0)
    assert s.ask == pytest.approx(0.01)
    assert s.bid == pytest.approx(-0.01)


def test_pair_spread_matches_excel() -> None:
    # 엑셀 개정판(meme.xlsx, 국내 maker 기준):
    #   메인!L12(en) = HL 매수호가disp − SF 매수호가disp (진입)
    #   메인!L14(ex) = HL 매도호가disp − SF 매도호가disp (청산)
    # 2026-07-13 실측 disp 값(시세!AE7/AF7·AE61/AF61)으로 대조.
    hl = SideDisp(ask=-0.0011270167839740623, bid=-0.0019273957769356468)
    sf = SideDisp(ask=2.2100920405295353e-05, bid=-0.002677742764585)
    s = pair_spread(hl, sf)
    assert s.entry == pytest.approx(0.000750346987649353, abs=1e-9)   # 메인!L12
    assert s.exit == pytest.approx(-0.0011491177043793576, abs=1e-9)  # 메인!L14


def test_pair_spread_none_propagates() -> None:
    hl = side_disp(None, None, 100.0)
    kr = side_disp(101.0, 99.0, 100.0)
    assert pair_spread(hl, kr) == PairSpread(entry=None, exit=None)


def test_net_entry() -> None:
    from kp_arb.disparity import net_entry

    # 진입 0.50%, 청산 0.60% → 호가폭합 0.10% → 순진입 = 0.50% − 0.05% − 0.042%
    s = PairSpread(entry=0.0050, exit=0.0060)
    assert net_entry(s, 0.00042) == pytest.approx(0.0050 - 0.0005 - 0.00042)
    assert net_entry(PairSpread(entry=None, exit=0.1), 0.0) is None


def test_net_exit() -> None:
    from kp_arb.disparity import net_exit

    # 순청산 = 청산 − 왕복비용/2 = (진입+청산)/2. 수렴하면 0 이하.
    assert net_exit(PairSpread(entry=0.0050, exit=0.0060)) == pytest.approx(0.0055)
    assert net_exit(PairSpread(entry=-0.0007, exit=0.0003)) == pytest.approx(-0.0002)
    assert net_exit(PairSpread(entry=None, exit=0.1)) is None
