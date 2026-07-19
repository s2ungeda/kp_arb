"""외환현물(네이버 하나은행 고시) 응답 파싱 테스트 — 순수 로직."""
from kp_arb.gateways.fx_spot import parse_spot


def test_parse_spot_calc_price() -> None:
    # calcPrice는 숫자로 옴 (exrate_rtd_server.py 실측 동작과 동일)
    assert parse_spot({"result": {"calcPrice": 1385.5}}) == 1385.5
    assert parse_spot({"result": {"calcPrice": "1385.50"}}) == 1385.5


def test_parse_spot_close_price_fallback() -> None:
    # calcPrice 없으면 closePrice(콤마 문자열) 폴백
    assert parse_spot({"result": {"closePrice": "1,385.50"}}) == 1385.5
    assert parse_spot({"result": {"calcPrice": "", "closePrice": "1,400.00"}}) == 1400.0


def test_parse_spot_invalid() -> None:
    assert parse_spot({}) is None
    assert parse_spot({"result": {}}) is None
    assert parse_spot({"result": {"calcPrice": "abc"}}) is None
    assert parse_spot({"result": {"calcPrice": 0}}) is None  # 0/음수는 무효
