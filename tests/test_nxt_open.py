"""오전 NXT 개장 매매 순수 로직(DESIGN-nxt-open.md) — 괴리·N호가·역산 주문가·창·판정·저장."""
from __future__ import annotations

import time

from kp_arb.domain.enums import Side, Underlying
from kp_arb.nxt_open import (
    AutoExit,
    NxtOpenScreen,
    NxtOpenSettings,
    Tier,
    accept_t0,
    apply_settings,
    base_due,
    day_over,
    disparity,
    hl_current,
    hotime_text,
    judge,
    level_price,
    opposite,
    order_price,
    range_block,
    range_limits,
    remaining_tiers,
    run_block_error,
    screen_from_dict,
    settings_from_dict,
    t0_verdict,
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
    # 개장 전 체결은 거부가 아니라 무시(early) — 개장 시각을 앞으로 두고 시험할 때 그날을 안 죽임
    assert t0_verdict(wall(7, 59, 59.9), "08:00:00") == "early"
    assert t0_verdict(wall(8, 0, 3.2), "08:00:00") == "ok"
    assert t0_verdict(wall(8, 0, 10.5), "08:00:00") == "late"

    # 오늘 기회 끝(사용자 2026-10-21 "시간이 지나면 자동으로 꺼졌으면") — 발주·창 끝·개장 밖은
    # 시각과 무관, 첫 체결이 없으면 개장+유예 10초+창 끝 5초를 넘겨야
    assert day_over(wall(8, 0, 1), "08:00:00", "open", False, True, 5.0)
    assert day_over(wall(8, 0, 6), "08:00:00", "closed", False, False, 5.0)
    assert day_over(wall(8, 41, 0), "08:00:00", "none", True, False, 5.0)
    assert not day_over(wall(8, 0, 1), "08:00:00", "open", False, False, 5.0)
    assert not day_over(wall(8, 0, 0.2), "08:00:00", "before", False, False, 5.0)
    assert not day_over(wall(7, 50, 0), "08:00:00", "none", False, False, 5.0)
    assert not day_over(wall(8, 0, 15), "08:00:00", "none", False, False, 5.0)
    assert day_over(wall(8, 0, 15.1), "08:00:00", "none", False, False, 5.0)
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
    # 이미 나간 단은 뺀다(사용자 2026-10-23 "나간 세트만 멈춤") — 2단이 나갔으면 3단만
    assert [p.index for p in judge(0.0105, None, st, {("entry", 1)})] == [2]
    assert remaining_tiers(st, set()) == 6 and remaining_tiers(st, {("entry", 1)}) == 5
    assert remaining_tiers(st, {(b, i) for b in ("entry", "exit") for i in range(3)}) == 0
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
    # 2026-10-29 추가 칸(저장·복원만): HL기준시간·범위제한, 단의 청산가·자동청산
    st.hl_base_hms, st.range_limit = "07:59:55", 0.03
    st.entries[0] = Tier(0.012, 100, 0.009, 0.005, True)
    back2 = settings_from_dict(asdict(st))
    assert (back2.hl_base_hms, back2.range_limit) == ("07:59:55", 0.03)
    assert back2.entries[0] == Tier(0.012, 100, 0.009, 0.005, True)
    assert back2.entries[1] == Tier(0.010, 100, 0.007) and back2.entries[1].auto_exit is False
    # 기본값 07:59:55·3%(사용자 2026-10-29) — 틀린 값·없는 값은 기본값
    base = settings_from_dict({"hl_base_hms": "7:59", "range_limit": "x"})
    assert (base.hl_base_hms, base.range_limit) == ("07:59:55", 0.03)
    assert NxtOpenSettings().hl_base_hms == "07:59:55" and NxtOpenSettings().range_limit == 0.03
    st3 = NxtOpenSettings()
    assert apply_settings(st3, {"hl_base_hms": "7:59:55"}) == ["HL기준시간은 HH:MM:SS"]
    assert apply_settings(st3, {"hl_base_hms": "07:58:00", "range_limit": 0.025}) == []
    assert (st3.hl_base_hms, st3.range_limit) == ("07:58:00", 0.025)
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
    # 수량 소수(사용자 2026-10-21 "HL 주문이니 소수점 허용") — 종목 수량 자리(sz_decimals)까지만
    assert apply_settings(st2, {"exits": [{"spread": -0.004, "qty": 0.5,
                                          "price_spread": -0.002}]}, sz_decimals=1) == []
    assert st2.exits[0] == Tier(-0.004, 0.5, -0.002) and st2.exits[0].valid()
    assert apply_settings(st2, {"exits": [{"spread": -0.004, "qty": 0.25,
                                          "price_spread": -0.002}]}, sz_decimals=1) == [
        "수량은 소수 1자리까지(HL)"]
    assert st2.exits[0].qty == 0.5  # 거부면 그대로
    assert apply_settings(st2, {"exits": [{"spread": -0.004, "qty": 0.25,
                                          "price_spread": -0.002}]}) == []  # 자리수 모르면 통과
    # 전 종목 복원 — 모르는 종목 키는 버림
    screen = NxtOpenScreen()
    screen_from_dict(screen, {"books": {"sk_hynix": asdict(st), "what": {}}})
    assert screen.of(Underlying.SK_HYNIX).start_s == 1.0 and "what" not in screen.books
    assert screen.of(Underlying.SAMSUNG).start_s == 0.5


def test_run_guard_needs_one_complete_tier_only() -> None:
    # 사용자 2026-10-21 "제대로 된 입력이 2세트뿐이면 2세트만 잠그자" — 반만 채운 단은 거부 사유가
    # 아니라 판정에서 빠지는 단. 유효한 단이 하나도 없을 때만 "단 없음"
    full, empty = Tier(0.002, 1, -0.005), Tier()
    assert run_block_error([full, empty, empty], "진입") is None
    assert run_block_error([Tier(0.002, 0, -0.005), full, empty], "진입") is None
    assert run_block_error([empty, empty, empty], "진입") == "진입 단 없음"
    assert run_block_error([Tier(0.001, 0, None), Tier(0.001, 0, None), empty], "청산") == (
        "청산 단 없음")
    assert Tier(0.002, 0, None).missing() == ["수량", "주문가"] and empty.missing() == [
        "Sprd", "수량", "주문가"]
    # 자동청산 체크 단은 청산가도 있어야 켠다(§4a-5, 사용자 2026-10-29 확인 4)
    assert run_block_error([full, Tier(0.002, 1, -0.005, None, True)], "진입") == (
        "진입2 자동청산 단 청산가 없음")
    assert run_block_error([Tier(0.002, 1, -0.005, 0.001, True)], "진입") is None
    assert run_block_error([Tier(0.002, 0, -0.005, None, True), full], "진입") is None  # 무효 단


def test_auto_exit_and_range_block_pure() -> None:
    # §4a: 판정 결과(Plan)에 체크+청산가 있는 단만 청산가 스프레드가 실린다; 반대 방향; 체결 누적
    st = NxtOpenSettings()
    st.run_entry = True
    st.entries = [Tier(0.010, 100, 0.009, 0.002, True), Tier(0.008, 50, 0.007, 0.001, False),
                  Tier(0.006, 10, 0.005, None, True)]
    plans = judge(0.012, None, st)
    assert [p.exit_spread for p in plans] == [0.002, None, None]
    assert opposite(Side.SELL) is Side.BUY and opposite(Side.BUY) is Side.SELL
    ae = AutoExit("O1", "entry", 0, Side.BUY, 100.0, 1294.4)
    assert not ae.add_fill("f1", 40.0) and not ae.add_fill("f1", 40.0)  # 같은 체결번호 한 번
    assert ae.filled == 40.0 and ae.add_fill("f2", 60.0)                  # 전량 → True
    # §4b: HL 현재가 = 마지막 체결가, 없으면 중간값(확인 5); 기준시간 + 10초 안의 첫 시세만
    assert hl_current(1310.2, 1310.0, 1310.5) == 1310.2
    assert hl_current(None, 1310.0, 1310.5) == 1310.25 and hl_current(None, None, 1310.5) is None
    lt = time.localtime()
    t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 59, 55, 0, 0, -1))
    assert base_due(t, "07:59:55") and base_due(t + 9.9, "07:59:55")
    assert not base_due(t - 0.1, "07:59:55") and not base_due(t + 10.1, "07:59:55")
    assert range_limits(1000.0, 0.03) == (970.0, 1030.0)
    # 금지(확인 6·7): 기준가 없음 → 금지, HL 현재가 기준 등호 포함
    assert range_block(Side.SELL, 990.0, None, 0.03) == "HL 기준가 없음"
    assert range_block(Side.SELL, None, 1000.0, 0.03) == "HL 현재가 없음"
    assert range_block(Side.SELL, 970.0, 1000.0, 0.03) == (
        "매도 금지 — HL 970.0 ≤ 한도 970.0(기준 1,000.0 −3%)")
    assert range_block(Side.SELL, 970.1, 1000.0, 0.03) is None
    assert range_block(Side.BUY, 1030.0, 1000.0, 0.03) == (
        "매수 금지 — HL 1,030.0 ≥ 한도 1,030.0(기준 1,000.0 +3%)")
    assert range_block(Side.BUY, 1029.9, 1000.0, 0.03) is None
    assert range_block(Side.BUY, 970.0, 1000.0, 0.03) is None  # 매수는 아래쪽 급락엔 안 막음


def test_hotime_text_for_diag_log() -> None:
    # §6 임시 진단 로그(2026-10-30): LS 호가의 거래소 시각 HHMMSS → "HH:MM:SS", 없으면 "-"
    assert hotime_text(80001.0) == "08:00:01" and hotime_text(235959.0) == "23:59:59"
    assert hotime_text(0.0) == "-" and hotime_text(None) == "-" and hotime_text(240000.0) == "-"
