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
    auto_exit_price,
    base_due,
    day_over,
    disparity,
    filter_quote,
    filter_settings_error,
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
    # 2026-10-29 추가 칸(저장·복원만): HL기준시간·범위제한, 단의 목표수익률(11-17, 옛 청산가 자리)·
    # 자동청산
    st.hl_base_hms, st.range_limit = "07:59:55", 0.03
    st.entries[0] = Tier(0.012, 100, 0.009, 0.005, True)
    back2 = settings_from_dict(asdict(st))
    assert (back2.hl_base_hms, back2.range_limit) == ("07:59:55", 0.03)
    assert back2.entries[0] == Tier(0.012, 100, 0.009, 0.005, True)
    assert back2.entries[1] == Tier(0.010, 100, 0.007) and back2.entries[1].auto_exit is False
    # 옛 청산가 키(exit_price_spread)·0 이하 목표수익률은 복원 안 함(뜻이 다름, 양수만)
    old = settings_from_dict({"entries": [{"spread": 0.01, "qty": 1, "price_spread": 0.0,
                                           "exit_price_spread": -0.002, "auto_exit": True},
                                          {"target_profit": -0.001}, {"target_profit": 0}]})
    assert [t.target_profit for t in old.entries] == [None, None, None]
    # 11-17 호가 필터 칸(저장·복원만): 5호가합·호가제외수량(0 이상)·최소호가스프레드(양수, 비우면
    # 없음)
    st.sum5_qty, st.exclude_qty, st.min_spread = 1000.0, 30.0, 0.003
    back3 = settings_from_dict(asdict(st))
    assert (back3.sum5_qty, back3.exclude_qty, back3.min_spread) == (1000.0, 30.0, 0.003)
    # 비거나 틀린 값은 없음(None) — 셋 다 필수(답 6 "가")라 0과 빈칸을 가른다(0은 0으로 복원)
    dflt = settings_from_dict({"sum5_qty": -1, "exclude_qty": "x", "min_spread": 0})
    assert (dflt.sum5_qty, dflt.exclude_qty, dflt.min_spread) == (None, None, None)
    assert settings_from_dict({"exclude_qty": 0}).exclude_qty == 0.0
    # 5호가합 0은 하한이 아니라 빈칸(앞 판이 빈칸을 0으로 저장했던 값도 빈칸으로 돌아온다)
    assert settings_from_dict({"sum5_qty": 0}).sum5_qty is None
    assert NxtOpenSettings().sum5_qty is None
    st4 = NxtOpenSettings()
    assert apply_settings(st4, {"sum5_qty": 500, "exclude_qty": 10, "min_spread": 0.002}) == []
    assert (st4.sum5_qty, st4.exclude_qty, st4.min_spread) == (500.0, 10.0, 0.002)
    assert apply_settings(st4, {"sum5_qty": None}) == [] and st4.sum5_qty is None  # 지움
    # 0 이하를 적으면 거부(빈칸과 구분 — "값 없음"이 나오는 혼동 방지), 호가제외수량 0은 허용
    assert apply_settings(st4, {"sum5_qty": 0}) == ["5호가합은 양수만"]
    assert apply_settings(st4, {"min_spread": -0.001}) == ["최소호가스프레드는 양수만"]
    assert apply_settings(st4, {"exclude_qty": 0}) == [] and st4.exclude_qty == 0.0
    assert apply_settings(st4, {"entries": [{"spread": 0.01, "qty": 1, "price_spread": 0.0,
                                            "target_profit": -0.001}]}) == ["목표수익률은 양수만"]
    assert apply_settings(st4, {"exits": [{"target_profit": -1}]}) == ["목표수익률은 양수만"]
    assert st4.entries[0] == Tier() and st4.exits[0] == Tier()  # 거부면 그대로
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
    # 자동청산 체크 단은 목표수익률도 있어야 켠다(§4a-5, 사용자 2026-10-29 확인 4; 11-17 청산가→
    # 목표수익률)
    assert run_block_error([full, Tier(0.002, 1, -0.005, None, True)], "진입") == (
        "진입2 자동청산 단 목표수익률 없음")
    assert run_block_error([Tier(0.002, 1, -0.005, 0.001, True)], "진입") is None
    assert run_block_error([Tier(0.002, 0, -0.005, None, True), full], "진입") is None  # 무효 단


def test_auto_exit_and_range_block_pure() -> None:
    # §4a: 판정 결과(Plan)에 체크+목표수익률 있는 단만 목표수익률이 실린다; 반대 방향; 체결 누적
    st = NxtOpenSettings()
    st.run_entry = True
    st.entries = [Tier(0.010, 100, 0.009, 0.002, True), Tier(0.008, 50, 0.007, 0.001, False),
                  Tier(0.006, 10, 0.005, None, True)]
    plans = judge(0.012, None, st)
    assert [p.target_profit for p in plans] == [0.002, None, None]
    assert opposite(Side.SELL) is Side.BUY and opposite(Side.BUY) is Side.SELL
    ae = AutoExit("O1", "entry", 0, Side.BUY, 100.0, 0.002)
    assert ae.avg_price() is None
    assert not ae.add_fill("f1", 40.0, 1300.0) and not ae.add_fill("f1", 40.0, 1300.0)  # 같은 번호
    assert ae.filled == 40.0 and ae.add_fill("f2", 60.0, 1310.0)                  # 전량 → True
    assert ae.avg_price() == 1306.0  # (40×1300 + 60×1310)/100
    # 가격이 안 실린 체결은 전량 판정엔 들어가고 평균가엔 안 들어간다(검토 2026-11-17)
    ae2 = AutoExit("O2", "exit", 0, Side.SELL, 10.0, 0.001)
    assert not ae2.add_fill("g1", 4.0, 0.0) and ae2.avg_price() is None
    assert ae2.add_fill("g2", 6.0, 800.0) and ae2.avg_price() == 800.0
    # 자동청산가(사용자 2026-11-17): 진입(매도) 단의 청산 = 매수 → 평균가 × (1 − 목표) 내림,
    # 청산(매수) 단의 청산 = 매도 → 평균가 × (1 + 목표) 올림
    assert auto_exit_price(1306.0, 0.002, Side.BUY, None) == 1303.3   # 1303.388 → 내림
    assert auto_exit_price(1306.0, 0.002, Side.SELL, None) == 1308.7  # 1308.612 → 올림
    assert auto_exit_price(822.23, 0.004, Side.SELL, None) == 825.52  # 825.5189 → 올림
    assert auto_exit_price(0.0, 0.002, Side.BUY, None) is None
    assert auto_exit_price(1306.0, 0.0, Side.BUY, None) is None
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


def test_quote_filter_conditions_in_order() -> None:
    # §1a(사용자 2026-11-17 문답): NXT 호가창 상위 5단계, 조건 1 → 2 → 3 순서, 통과하면
    # 현재가 = (최우선 매도 + 매수) × 0.5
    asks = [(1_001_000.0, 10.0), (1_002_000.0, 40.0), (1_003_000.0, 500.0), (1_004_000.0, 100.0),
            (1_005_000.0, 100.0), (1_006_000.0, 9_000.0)]   # 6단계째는 합에 안 들어감
    bids = [(1_000_000.0, 20.0), (999_000.0, 300.0), (998_000.0, 100.0), (997_000.0, 100.0),
            (996_000.0, 100.0)]
    # 통과: 매도 5합 750·매수 5합 620 ≥ 600 / 제외 40 → 최우선 매도 1,003,000(10·40 건너뜀 — 40은
    # "보다 큰"이 아니라 제외)·매수 999,000(20 건너뜀) / 스프레드 4,000/1,003,000 = 0.399% < 0.5%
    ok = filter_quote(bids, asks, 600.0, 40.0, 0.005)
    assert ok.why is None and (ok.ask, ok.bid) == (1_003_000.0, 999_000.0)
    assert ok.price == 1_001_000.0
    assert filter_quote(bids, asks, 600.0, 30.0, 0.005).ask == 1_002_000.0  # 40 > 30은 포함
    # 조건 1: 한쪽이라도 5합 미달(6단계째 9,000은 안 센다)
    assert filter_quote(bids, asks, 700.0, 40.0, 0.005).why == (
        "조건1 미달 — 매수5호가합 620 < 700")
    assert filter_quote(bids, asks, 751.0, 40.0, 0.005).why == (
        "조건1 미달 — 매도5호가합 750 < 751")
    # 조건 2: 제외수량과 같은 잔량은 건너뜀("보다 큰"); 5단계 안에 없으면 미달
    assert filter_quote(bids, asks, 600.0, 100.0, 0.01).ask == 1_003_000.0
    assert filter_quote(bids, asks, 600.0, 300.0, 0.01).why == (
        "조건2 미달 — 제외수량 300 넘는 매수호가 없음")
    assert filter_quote(bids, asks, 600.0, 500.0, 0.01).why == (
        "조건2 미달 — 제외수량 500 넘는 매도·매수호가 없음")
    # 조건 3: 스프레드가 최소호가스프레드 이상이면 미달(등호 포함), 분모는 매도호가
    assert filter_quote(bids, asks, 600.0, 40.0, 0.003).why == (
        "조건3 미달 — 호가스프레드 0.399% ≥ 0.3%")
    assert filter_quote([(1000.0, 5.0)], [(1000.0, 5.0)], 1.0, 0.0, 0.001).price == 1000.0
    # 경계(검토 2026-11-17): 조건 1은 합 == 5호가합이면 통과, 조건 3은 스프레드 == 최소면 미달
    assert filter_quote(bids, asks, 620.0, 40.0, 0.005).why is None
    assert filter_quote([(999.0, 100.0)], [(1000.0, 100.0)], 1.0, 0.0, 0.001).why == (
        "조건3 미달 — 호가스프레드 0.100% ≥ 0.1%")
    assert filter_quote([(999.0, 100.0)], [(1000.0, 100.0)], 1.0, 0.0, 0.0011).price == 999.5
    # 5단계가 안 되면 있는 만큼 더한다; 양쪽 다 모자라면 매도 쪽 사유
    assert filter_quote([(999.0, 10.0)], [(1000.0, 10.0)], 11.0, 0.0, 0.01).why == (
        "조건1 미달 — 매도5호가합 10 < 11")
    # 값이 비었거나 호가가 없으면 판정 불가
    assert filter_quote(bids, asks, None, 30.0, 0.005).why == "호가 필터 값 없음"
    assert filter_quote([], asks, 1.0, 0.0, 0.01).why == "NXT 호가 없음"
    assert filter_quote(None, None, 1.0, 0.0, 0.01).why == "NXT 호가 없음"
    # 실행 켜기 가드: 셋 다 필수(답 6 "가")
    st = NxtOpenSettings()
    assert filter_settings_error(st) == "호가 필터 값 없음: 5호가합·호가제외수량·최소호가스프레드"
    st.sum5_qty, st.exclude_qty = 100.0, 0.0
    assert filter_settings_error(st) == "호가 필터 값 없음: 최소호가스프레드"
    st.min_spread = 0.003
    assert filter_settings_error(st) is None
