"""상대호가 괴리·스프레드 테스트 — 순수 로직 (엑셀 IM.xlsx 수식 대조)."""
import pytest

from kp_arb.disparity import (
    PairSpread,
    SideDisp,
    disp,
    est_price,
    maker_price_for_spread,
    pair_spread,
    side_disp,
)


def test_maker_price_for_spread() -> None:
    # §6.2-4: P = 기준가 × (1 + HL괴리(est) − 기준값). 체결 시 스프레드 = 기준값 보장.
    price = maker_price_for_spread(300_000.0, 0.001, 0.0006)
    assert price == pytest.approx(300_120.0)
    # 검산: 이 가격의 국내 disp = (P−기준가)/기준가 → HL괴리 − disp = 기준값
    assert 0.001 - (price - 300_000.0) / 300_000.0 == pytest.approx(0.0006)


def test_est_price_vwap() -> None:
    # 델파이 CalcEstPrice 이식(2026-07-23): 주문수량을 쓸어담는 **평균 체결가**.
    # 매수 10 = 매도1호가 100×5 + 매도2호가 101×5 → (500+505)/10 = 100.5
    assert est_price([(100.0, 5), (101.0, 10)], 10) == pytest.approx(100.5)
    # 1호가 잔량으로 충분하면 1호가 그대로
    assert est_price([(100.0, 10), (101.0, 10)], 10) == 100.0
    # 매도(매수호가 사다리, 내림차순): 100×6 + 99×4 → 99.6
    assert est_price([(100.0, 6), (99.0, 10)], 10) == pytest.approx(99.6)


def test_est_price_partial_and_invalid() -> None:
    # 잔량 부족: 확보 가능한 전량(5+3=8)의 평균 — (500+303)/8 (원본 동작)
    assert est_price([(100.0, 5), (101.0, 3)], 10) == pytest.approx(803 / 8)
    assert est_price([], 10) is None
    assert est_price([(100.0, 5)], 0) is None   # 수량 0/음수는 무효
    assert est_price([(100.0, 5)], -1) is None


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
