"""동시호가 주문 순수 로직(DESIGN-auction-order.md ver2) — 역산 예상주문가, 행 판정·순서, 환 헤지
수량·가격·분할, HL 기준가·4분할·정정가, 시각, 저장/복원."""
from kp_arb.auction_order import (
    AuctionBook,
    AuctionScreen,
    PlacedOrder,
    Row,
    apply_rows,
    book_from_dict,
    expected_order_price,
    fillable_qty,
    fire_order,
    fx_hedge_plan,
    hl_amend_price,
    hl_base_price,
    hl_mid,
    hl_order_price,
    hl_split,
    hms_to_sec,
    monitor_spread,
    net_exposure,
    order_moment,
    row_fires,
    screen_from_dict,
    sec_of_day,
    settings_from_dict,
    split_qty,
)
from kp_arb.domain.enums import Side


def test_expected_order_price_reverses_spread_and_snaps_to_futures_tick() -> None:
    # §2: P = SF이론가 × (1 + (환×HL − S)/S − s). 환 1,327.30 · HL 중간 1,332.5 · S 1,766,000 →
    # HL괴리 ≈ +0.1485%; 이론가 1,770,000 · s 0.3% → 1,767,318 → 호가단위 1,000: 매수 내림
    # 1,767,000 / 매도 올림 1,768,000. 재료가 하나라도 없으면 None.
    fx, hl, s, theory = 1327.30, 1332.5, 1_766_000.0, 1_770_000.0
    buy = expected_order_price(theory, fx, hl, s, 0.003, Side.BUY)
    sell = expected_order_price(theory, fx, hl, s, 0.003, Side.SELL)
    assert buy == 1_767_000 and sell == 1_768_000
    assert expected_order_price(theory, None, hl, s, 0.003, Side.BUY) is None
    assert expected_order_price(theory, fx, hl, s, None, Side.BUY) is None
    assert expected_order_price(theory, fx, hl, 0.0, 0.003, Side.BUY) is None
    assert hl_mid(1332.0, 1333.0) == 1332.5 and hl_mid(None, 1.0) is None


def test_row_fires_and_fire_order_top_down_buy_then_sell() -> None:
    # §3: 매수 = 예상주문가 > 예상체결가, 매도 = 예상주문가 < 예상체결가(같으면 안 냄). 순서는 화면
    # 위→아래(매수 1~8, 매도 1~8), 켜진 행 중 수량·스프레드가 있는 행만.
    assert row_fires(Side.BUY, 1_767_000, 1_766_000)
    assert not row_fires(Side.BUY, 1_766_000, 1_766_000)
    assert row_fires(Side.SELL, 1_765_000, 1_766_000)
    assert not row_fires(Side.SELL, 1_767_000, 1_766_000)
    b = AuctionBook()
    b.buy[2] = Row(0.3, 20, True)
    b.buy[0] = Row(0.35, 15, True)
    b.buy[5] = Row(None, 10, True)       # 스프레드 없음 — 뺀다
    b.sell[1] = Row(-0.1, 20, True)
    b.sell[0] = Row(-0.1, 20, False)     # 꺼짐
    assert fire_order(b) == [(Side.BUY, 0), (Side.BUY, 2), (Side.SELL, 1)]


def test_fx_hedge_net_quantity_price_and_split() -> None:
    # §4: 체결될 수량(매수 = 예상체결가 < 주문가, 매도 = 예상체결가 > 주문가) → 순 수량·큰 쪽 방향
    # → 반대 방향 원달러, 수량 = hedge_qty(예상체결가), 가격 = 예상체결가 ± x틱, 1회수량 분할·나머지
    # 마지막
    orders = [PlacedOrder(Side.BUY, 1_767_000, 20), PlacedOrder(Side.BUY, 1_765_000, 15),
              PlacedOrder(Side.SELL, 1_765_000, 10), PlacedOrder(Side.SELL, 1_767_000, 5)]
    assert fillable_qty(orders, 1_766_000) == (20, 10)   # 1,765,000 매수·1,767,000 매도는 안 됨
    assert net_exposure(20, 10) == (Side.BUY, 10) and net_exposure(10, 20) == (Side.SELL, 10)
    assert net_exposure(7, 7) is None
    side, px, parts = fx_hedge_plan(Side.BUY, 85, 1_766_000, 1327.30, 20, 100, 20)
    assert side is Side.SELL and px == 1325.3                      # 매도 = 예상체결가 − 2.0원
    # 85 × 10 × 1,766,000 / 1,327.30 / 10,000 = 113.1 → 113계약 → 20×5 + 13
    assert parts == [20] * 5 + [13] and sum(parts) == 113
    side2, px2, parts2 = fx_hedge_plan(Side.SELL, 1, 1_766_000, 1327.30, 20, 100, 10)
    assert side2 is Side.BUY and px2 == 1329.3 and parts2 == [1]   # 1.33 → 1
    assert fx_hedge_plan(Side.SELL, 1, 1_766_000, 1327.30, 20, 50, 10)[2] == []  # 0.67 → 0
    assert split_qty(85, 20) == [20, 20, 20, 20, 5] and split_qty(5, 20) == [5]
    assert split_qty(0, 20) == [] and split_qty(40, 20) == [20, 20]


def test_hl_base_price_nearest_grid_tie_higher_and_offsets() -> None:
    # §5: 기준가 = bbo 중간에 가장 가까운 호가(머지 콤보 격자, 없으면 HL 격자), 둘이면 높은 쪽
    assert hl_base_price(1332.4, 1332.6, None, None, 1) == 1332.5            # 격자 0.1
    assert hl_base_price(1332.4, 1332.5, None, None, 1) == 1332.5            # 1332.45 → 높은 쪽
    assert hl_base_price(1332.0, 1333.0, 5, 5, 1) == 1332.5                  # 머지 0.5틱
    assert hl_base_price(1332.0, 1332.6, 4, None, 1) == 1332.0  # 머지 1틱: 1332.3 → 1332
    assert hl_base_price(None, 1332.0, None, None, 1) is None
    assert hl_order_price(1332.5, 0.3, 1) == 1332.8 and hl_order_price(1332.5, -1.0, 1) == 1331.5
    assert hl_order_price(1332.5, 0.0, 1) == 1332.5
    assert hl_amend_price(1332.5, 0.5, Side.BUY, 1) == 1333.0                # 매수 정정은 위로
    assert hl_amend_price(1332.5, 0.5, Side.SELL, 1) == 1332.0               # 매도는 아래로


def test_hl_split_four_ways_two_decimals_remainder_to_a() -> None:
    # §5: 4 균등 분할 소수 2자리, 나머지는 a. 최소 수량 규칙은 없음(사용자 2026-10-26 — 1계약 체결의
    # 4분할이 구조상 하한). 소수 자리에서 0이 되는 건만 안 만든다.
    assert hl_split(85.0, 2) == [21.25, 21.25, 21.25, 21.25]
    assert hl_split(10.0, 2) == [2.5, 2.5, 2.5, 2.5]                           # 1계약 체결
    assert hl_split(85.0, 1) == [21.4, 21.2, 21.2, 21.2]                     # 메타가 1자리면 1자리
    assert hl_split(10.0, 0) == [4.0, 2.0, 2.0, 2.0]                          # 정수 종목
    assert hl_split(3.0, 2) == [0.75, 0.75, 0.75, 0.75]
    assert hl_split(0.03, 2) == [0.01, 0.01, 0.01]                 # 4건이면 0 건이 생김 → 3건
    assert hl_split(1.0, 0) == [1.0]                               # 정수 종목 1 → 한 건
    assert hl_split(0.001, 2) == [] and hl_split(0, 2) == []


def test_order_moment_is_pc_clock_plus_ms() -> None:
    # §3: 주문시각 = HH:MM:SS + ms, PC 시계 그대로(서버 시각 차이는 사용자가 주문시각에 넣음, 10-26)
    assert hms_to_sec("15:44:55") == 15 * 3600 + 44 * 60 + 55 and hms_to_sec("24:00:00") is None
    assert hms_to_sec("x") is None
    base = 15 * 3600 + 44 * 60 + 55
    assert order_moment("15:44:55", 100) == base + 0.1
    assert order_moment("bad", 0) is None
    # 2026-10-26 15:44:55 KST = epoch 1793083495 → 하루 중 초
    assert abs(sec_of_day(1793083495.0) - base) < 1e-6


def test_apply_rows_rejects_on_without_inputs_and_roundtrip() -> None:
    # §2 켜기 조건 + 저장/복원(종목별, 켬 상태 포함, 예약실행은 저장 안 함)
    b = AuctionBook()
    errs = apply_rows(b, {"buy": [{"spread": 0.3, "qty": 20, "on": True},
                                  {"spread": "", "qty": 0, "on": True}]})
    assert errs == ["매수 2행: 켜려면 주문Sprd·수량이 있어야 합니다"]
    assert not b.buy[0].on  # 통째로 거부
    assert apply_rows(b, {"buy": [{"spread": "0.3", "qty": "20", "on": True}],
                          "sell": [{"spread": -0.1, "qty": 5}]}) == []
    assert b.buy[0] == Row(0.3, 20, True) and b.sell[0] == Row(-0.1, 5, False)
    b.month = "next"
    b.settings.ls_hms, b.settings.tick, b.settings.hl_off["b"] = "15:44:50", 25, -0.5
    b.hl_orders = {"a": "123", "c": "456"}
    import dataclasses

    raw = dataclasses.asdict(b)
    back = book_from_dict(raw)
    assert back == b
    st = settings_from_dict({"ls_hms": "25:00:00", "ls_ms": "x", "tick": "30",
                             "hl_pt": {"a": "0.2", "z": 1}})
    assert st.ls_hms == "15:44:55" and st.ls_ms == 100 and st.tick == 30 and st.hl_pt["a"] == 0.2
    sc = AuctionScreen()
    screen_from_dict(sc, {"books": {"samsung": raw, "nope": "x"}})
    assert sc.book("samsung") == b and sc.book("nope") == AuctionBook()


def test_monitor_spread_uses_expected_price_as_futures_leg() -> None:
    # 사용자 2026-10-26: 모니터 = (환×HL − S)/S − (예상체결가 − 이론가)/이론가. 환 1327.30·HL
    # 1332.5·S 1,766,000 → +0.1485%; 예상체결가 1,774,000·이론가 1,770,000 → +0.226% → 차 −0.0775%
    v = monitor_spread(1_774_000.0, 1_770_000.0, 1327.30, 1332.5, 1_766_000.0)
    assert v is not None and abs(v - (-0.000775)) < 1e-5
    assert monitor_spread(None, 1_770_000.0, 1327.30, 1332.5, 1_766_000.0) is None
    assert monitor_spread(1_774_000.0, 0.0, 1327.30, 1332.5, 1_766_000.0) is None
