"""HL 호가단위(틱) 옵션 순수 로직 — 가격 자릿수 기반 단계."""
from kp_arb.hl_merge import merge_tick_options


def test_options_from_price_magnitude() -> None:
    # 삼성 ~163: 기준틱 = 10^(2-4)=0.01 → 0.01/0.02/0.05/0.1/1/10
    ticks = [s for s, _, _ in merge_tick_options(163.0)]
    assert ticks == ["0.01", "0.02", "0.05", "0.1", "1", "10"]
    # nSigFigs/mantissa 동반 (2단계 = (5,2), 원시 = (None,None))
    opts = merge_tick_options(163.0)
    assert opts[0] == ("0.01", None, None)
    assert opts[1] == ("0.02", 5, 2)


def test_options_scale_with_price() -> None:
    # 하이닉스 ~1400: 기준틱 = 10^(3-4)=0.1 → 0.1/0.2/0.5/1/10/100
    assert [s for s, _, _ in merge_tick_options(1400.0)] == [
        "0.1", "0.2", "0.5", "1", "10", "100"]


def test_nonpositive_price_empty() -> None:
    assert merge_tick_options(0.0) == []
    assert merge_tick_options(-5.0) == []
