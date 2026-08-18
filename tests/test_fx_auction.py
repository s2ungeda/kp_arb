"""원달러선물 동시호가 대응주문 순수 로직 테스트 (DESIGN-fx-auction.md §4)."""
from kp_arb.domain.enums import Side
from kp_arb.fx_auction import (
    FuturesAck,
    compute_hedge,
    hedge_price,
    hedge_qty,
    hedge_side,
    in_auction_window,
    parse_futures_ack,
)

WINDOWS = [("08:30", "08:46"), ("15:35", "15:46")]


# --- 시간창 ---


def test_in_window_inside_and_boundary() -> None:
    assert in_auction_window("08:40:00", WINDOWS)
    assert in_auction_window("08:30:00", WINDOWS)   # 시작 경계 포함
    assert in_auction_window("08:46:00", WINDOWS)   # 종료 경계 포함
    assert in_auction_window("15:40:00", WINDOWS)   # 두번째 창


def test_in_window_outside() -> None:
    assert not in_auction_window("08:29:59", WINDOWS)
    assert not in_auction_window("09:00:00", WINDOWS)
    assert not in_auction_window("16:00:00", WINDOWS)


def test_in_window_malformed_skipped() -> None:
    assert not in_auction_window("bad", WINDOWS)          # now 이상 → False
    win = [("", "08:46"), ("15:35", "15:46")]             # 첫 창 형식 이상 → 건너뜀
    assert not in_auction_window("08:40:00", win)
    assert in_auction_window("15:40:00", win)


# --- 방향 ---


def test_hedge_side_opposite() -> None:
    assert hedge_side(Side.BUY) is Side.SELL
    assert hedge_side(Side.SELL) is Side.BUY


# --- 가격 (매수 +틱 / 매도 −틱, 0.1 그리드) ---


def test_hedge_price_sign() -> None:
    assert hedge_price(1421.5, 10, Side.SELL) == 1420.5   # 현재가 − 10×0.1
    assert hedge_price(1421.5, 10, Side.BUY) == 1422.5    # 현재가 + 10×0.1
    assert hedge_price(1421.5, 3, Side.SELL) == 1421.2    # −0.3


def test_hedge_price_snaps_to_tick() -> None:
    assert hedge_price(1421.53, 0, Side.BUY) == 1421.5    # 0.1 그리드로 스냅


# --- 수량 (헤지비율 반내림 안, 정수 계약) ---


def test_hedge_qty_formula() -> None:
    # 10 × 10 × 142150 / 1421.5 / 10000 = 1.0
    assert hedge_qty(10, 142150, 1421.5, 1.0) == 1


def test_hedge_qty_ratio_inside_floor() -> None:
    # 2.0 × 0.5 = 1.0 → 1 (반내림 안에서 곱함)
    assert hedge_qty(20, 142150, 1421.5, 0.5) == 1
    # 1.0 × 0.5 = 0.5 → 0
    assert hedge_qty(10, 142150, 1421.5, 0.5) == 0


def test_hedge_qty_guards() -> None:
    assert hedge_qty(0, 142150, 1421.5, 1.0) == 0
    assert hedge_qty(10, 142150, 0.0, 1.0) == 0    # 현재가 0 방어
    assert hedge_qty(10, 142150, 1421.5, 0.0) == 0  # 헤지비율 0


# --- 종합 ---


def test_compute_hedge_buy_and_sell() -> None:
    # 주식선물 매수 → 원달러선물 매도, 현재가 − 틱
    assert compute_hedge(Side.BUY, 20, 142150, 1421.5, 10, 0.5) == (Side.SELL, 1420.5, 1)
    # 주식선물 매도 → 원달러선물 매수, 현재가 + 틱
    assert compute_hedge(Side.SELL, 20, 142150, 1421.5, 10, 0.5) == (Side.BUY, 1422.5, 1)


# --- 선물 접수(O01) 파싱 (실측 body 키, 2026-08-18) ---


def _o01(fnoisuno: str, bnstp: str, ordqty: str, ordprc: str, ordno: str) -> dict:
    # 실측 O01은 필드가 100+개 — 파서가 쓰는 키만 담아도 동일하게 동작.
    return {"fnoIsuno": fnoisuno, "bnstp": bnstp, "ordqty": ordqty,
            "ordprc": ordprc, "ordno": ordno, "orgordno": "0", "trcode1": "FO01"}


def test_parse_o01_hynix_buy() -> None:  # 실측 order 2222
    ack = parse_futures_ack(_o01("A5069000", "2", "1", "1753000.00", "2222"))
    assert ack == FuturesAck("2222", "A5069000", Side.BUY, 1, 1753000.0)


def test_parse_o01_samsung_sell() -> None:  # 실측 order 2225
    ack = parse_futures_ack(_o01("A1169000", "1", "1", "293000.00", "2225"))
    assert ack == FuturesAck("2225", "A1169000", Side.SELL, 1, 293000.0)


def test_parse_o01_bad_or_missing_returns_none() -> None:
    assert parse_futures_ack({"bnstp": "2", "ordqty": "1"}) is None       # 종목 없음
    assert parse_futures_ack(_o01("A1169000", "9", "1", "1.0", "1")) is None   # 매매구분 이상
    assert parse_futures_ack(_o01("A1169000", "2", "0", "1.0", "1")) is None   # 수량 0
    assert parse_futures_ack(_o01("A1169000", "2", "x", "1.0", "1")) is None   # 수량 비숫자
