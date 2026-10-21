"""오전 NXT 개장 매매 순수 로직(DESIGN-nxt-open.md) — 괴리·N호가·역산 주문가·창·판정·저장."""
from __future__ import annotations

import time

from kp_arb.domain.enums import Side, Underlying
from kp_arb.nxt_open import (
    NxtOpenScreen,
    NxtOpenSettings,
    Tier,
    accept_t0,
    apply_settings,
    disparity,
    judge,
    level_price,
    order_price,
    screen_from_dict,
    settings_from_dict,
    window_state,
)


def test_disparity_formula_and_missing_inputs() -> None:
    # 엑셀 예: HL 1294 × 환 1338.4 = 1,731,890 vs NXT 1,729,000 → +0.167%
    d = disparity(1338.4, 1294.0, 1_729_000.0)
    assert d is not None and abs(d - 0.0016715) < 1e-6
    assert disparity(None, 1294.0, 1_729_000.0) is None
    assert disparity(1338.4, None, 1_729_000.0) is None
    assert disparity(1338.4, 1294.0, None) is None
    assert disparity(1338.4, 1294.0, 0.0) is None


def test_level_price_picks_nth_from_best() -> None:
    bids = [(1294.0, 10.0), (1293.9, 5.0), (1293.8, 7.0)]
    assert level_price(bids) == 1294.0 and level_price(bids, 1) == 1294.0
    assert level_price(bids, 3) == 1293.8
    assert level_price(bids, 4) is None       # 단계 부족
    assert level_price(bids, 0) is None
    assert level_price(None, 1) is None
    assert level_price([], 1) is None


def test_order_price_rounds_sell_up_and_buy_down_on_hl_grid() -> None:
    # S 1,729,000 × (1 + 0.009) / 1338.4 = 1303.47… → 매도는 바로 위 호가(0.1 격자) 1303.5,
    # 매수(청산, t = −0.002)는 1289.27… → 바로 아래 1289.2
    sell = order_price(1_729_000.0, 0.009, 1338.4, Side.SELL, sz_decimals=1)
    buy = order_price(1_729_000.0, -0.002, 1338.4, Side.BUY, sz_decimals=1)
    assert sell == 1303.5 and buy == 1289.2
    assert order_price(0.0, 0.009, 1338.4, Side.SELL, 1) is None
    assert order_price(1_729_000.0, 0.009, 0.0, Side.SELL, 1) is None


def test_window_state_by_first_trade_time() -> None:
    assert window_state(100.0, None, 0.5, 5.0) == "none"
    assert window_state(100.2, 100.0, 0.5, 5.0) == "before"
    assert window_state(100.5, 100.0, 0.5, 5.0) == "open"
    assert window_state(105.0, 100.0, 0.5, 5.0) == "open"
    assert window_state(105.01, 100.0, 0.5, 5.0) == "closed"


def test_accept_t0_only_right_after_open_time() -> None:
    # 08:00:03에 온 첫 체결은 T0, 08:41(코어를 늦게 켠 날)은 아님 — 뒤늦은 첫 수신을 개장으로
    # 오인해 창을 열지 않게
    def wall(h: int, m: int, s: float) -> float:
        lt = time.localtime()
        base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return base + h * 3600 + m * 60 + s

    assert accept_t0(wall(8, 0, 3.2), "08:00:00")
    assert accept_t0(wall(8, 0, 10.0), "08:00:00")
    assert not accept_t0(wall(8, 0, 10.5), "08:00:00")
    assert not accept_t0(wall(7, 59, 59.9), "08:00:00")
    assert not accept_t0(wall(8, 41, 0), "08:00:00")
    assert accept_t0(wall(9, 0, 1), "09:00:00")  # 개장 시각은 설정값


def _settings() -> NxtOpenSettings:
    st = NxtOpenSettings()
    st.entries = [Tier(0.012, 100, 0.009), Tier(0.010, 100, 0.007), Tier(0.008, 100, 0.005)]
    st.exits = [Tier(-0.004, 100, -0.002), Tier(-0.008, 100, -0.006), Tier(-0.010, 100, -0.008)]
    st.run_entry = st.run_exit = True
    return st


def test_judge_sends_every_matching_tier_at_once_entry_first() -> None:
    st = _settings()
    # 괴리 1.3% → 세 단 다 맞음(사용자 "3세트 다 맞으면 다 나가야지") → HL 매도 3건
    plans = judge(0.013, None, st)
    assert [(p.block, p.index, p.side, p.qty, p.price_spread) for p in plans] == [
        ("entry", 0, Side.SELL, 100, 0.009), ("entry", 1, Side.SELL, 100, 0.007),
        ("entry", 2, Side.SELL, 100, 0.005)]
    # 1.05% → 2·3단만
    assert [p.index for p in judge(0.0105, None, st)] == [1, 2]
    # 0.5% → 없음
    assert judge(0.005, 0.001, st) == []
    # 청산: −0.9% → 1·2단(−0.4·−0.8 이하), 3단(−1.0)은 아님 → HL 매수
    plans = judge(0.0, -0.009, st)
    assert [(p.block, p.index, p.side) for p in plans] == [
        ("exit", 0, Side.BUY), ("exit", 1, Side.BUY)]
    # 진입이 하나라도 맞으면 진입만(한 방향)
    assert all(p.block == "entry" for p in judge(0.013, -0.02, st))
    # 실행이 꺼진 쪽은 보지 않는다 — 진입 끔이면 청산만
    st.run_entry = False
    assert all(p.block == "exit" for p in judge(0.013, -0.009, st))
    st.run_exit = False
    assert judge(0.013, -0.009, st) == []
    st.run_entry = st.run_exit = True
    # 비어 있는 단(수량 0·스프레드 없음)은 건너뜀
    st.entries[0] = Tier(0.001, 0, 0.0)
    st.entries[1] = Tier(None, 100, 0.0)
    assert [p.index for p in judge(0.013, None, st)] == [2]


def test_settings_roundtrip_and_validation() -> None:
    st = _settings()
    st.start_s, st.end_s, st.run_entry, st.ref_qty = 1.0, 4.0, True, 100
    from dataclasses import asdict

    back = settings_from_dict(asdict(st))
    assert back.start_s == 1.0 and back.end_s == 4.0 and back.ref_qty == 100
    assert back.entries[0] == Tier(0.012, 100, 0.009) and back.exits[2] == Tier(-0.010, 100, -0.008)
    assert back.run_entry is False and back.run_exit is False  # 실행은 복원 안 함
    # 틀린 값은 그 항목만 기본값
    bad = settings_from_dict({"start_s": 6.0, "end_s": 1.0, "open_hms": "8:00",
                              "entries": [{"spread": "x", "qty": -1}]})
    assert (bad.start_s, bad.end_s) == (0.5, 5.0)
    assert bad.open_hms == "08:00:00" and bad.entries[0] == Tier()
    # 화면 명령 반영: 틀리면 거부·아무 것도 안 바뀜
    st2 = NxtOpenSettings()
    assert apply_settings(st2, {"start_s": 3.0, "end_s": 2.0}) == ["매매시간은 0 ≤ 시작 ≤ 끝"]
    assert apply_settings(st2, {"entries": [{"spread": 0.01, "qty": 50,
                                            "price_spread": 0.008}]}) == []
    assert st2.entries[0] == Tier(0.01, 50, 0.008) and st2.entries[1] == Tier()
    # 전 종목 복원 — 모르는 종목 키는 버림
    screen = NxtOpenScreen()
    screen_from_dict(screen, {"books": {"sk_hynix": asdict(st), "what": {}}})
    assert screen.of(Underlying.SK_HYNIX).start_s == 1.0 and "what" not in screen.books
    assert screen.of(Underlying.SAMSUNG).start_s == 0.5
