"""HL 호가단위(틱) 옵션 순수 로직 — 가격 자릿수 기반 단계."""
from kp_arb.hl_merge import (
    merge_active_dict,
    merge_band,
    merge_choice_for_tick,
    merge_tick_options,
)


def test_choice_for_tick_follows_price_band() -> None:
    # 사용자 2026-10-27 "원래 가격대별로 호가단위가 있어야 함": 고른 틱은 그대로, 인자만 가격대에
    # 맞춘다. 0.1 = 999달러대 nSigFigs 4 / 1,009달러대 원시. 0.01은 1,000달러 위에 없음 → 가장
    # 가까운 0.1. 0.5는 999달러대에 없음 → 로그 거리 0.1(0.7) vs 1(0.3) → 1.
    assert merge_choice_for_tick(999.0, 0.1) == (0.1, 4, None)
    got = merge_choice_for_tick(1009.0, 0.1)
    assert got is not None and abs(got[0] - 0.1) < 1e-12 and got[1:] == (None, None)
    got = merge_choice_for_tick(1009.0, 0.01)
    assert got is not None and abs(got[0] - 0.1) < 1e-12 and got[1:] == (None, None)
    got = merge_choice_for_tick(999.0, 0.5)
    assert got is not None and got[0] == 1.0 and got[1:] == (3, None)
    assert merge_choice_for_tick(999.0, 0.2) == (0.1, 4, None)  # 0.2: 0.1(0.3) vs 1(0.7)
    assert merge_choice_for_tick(0.0, 0.1) is None and merge_choice_for_tick(10.0, 0) is None
    assert merge_band(999.0) == 2 and merge_band(1009.0) == 3 and merge_band(0.0) is None


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


def test_merge_active_dict_carries_tick_text_for_screens() -> None:
    # 2026-10-30: 체결쏴·NXT 개장·동시호가 화면은 콤보 대신 글자라 스냅샷에 틱 글이 있어야 한다
    # (실증: 틱 글 없는 자동M 스냅샷 → 화면 "-"). 163달러대 nSigFigs 5·mantissa 1 = 0.01
    assert merge_active_dict((5, 1), 163.4) == {"n_sig_figs": 5, "mantissa": 1, "tick": "0.01"}
    assert merge_active_dict((None, None), 1081.0) == {"n_sig_figs": None, "mantissa": None,
                                                        "tick": "0.1"}
    assert merge_active_dict((5, 1), None) == {"n_sig_figs": 5, "mantissa": 1, "tick": None}
    assert merge_active_dict(None, 163.4) is None
