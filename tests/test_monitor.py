"""모니터 데이터 수집부(MonitorState) 테스트 — 창(tkinter)은 수동 확인."""
from kp_arb.domain.enums import Instrument, Underlying
from kp_arb.domain.models import Quote
from kp_arb.gateways.hl import Mark
from kp_arb.gateways.ls_ws import ExpectedPrice, TradeTick
from kp_arb.monitor import MonitorState, funding_countdown

SAMSUNG = Underlying.SAMSUNG


def q(instrument: Instrument, bid: float, ask: float, *,
      bid_qty: float = 100, ask_qty: float = 50, market: str = "krx") -> Quote:
    return Quote(underlying=SAMSUNG, instrument=instrument, bid=bid, ask=ask,
                 ts=1.0, bid_qty=bid_qty, ask_qty=ask_qty, market=market)


def test_ls_rows_shape_and_values() -> None:
    state = MonitorState()
    state.on_quote(q(Instrument.KR_STOCK, 292_500, 293_000))
    state.on_trade(TradeTick(underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
                             price=292_800))
    state.on_expected(ExpectedPrice(underlying=SAMSUNG, price=292_700))

    rows = state.ls_rows()
    assert len(rows) == 9  # 3종목 × (주식/선물/ETF)
    stock = rows[0]
    # (종목, 매도잔량, 매도가, 현재가, 매수가, 매수잔량, 예상가)
    assert stock == ("삼성전자 주식", "50", "293,000", "292,800",
                     "292,500", "100", "292,700")
    assert rows[1][0] == "선물" and rows[1][2] == "-"  # 미수신은 '-'


def test_krx_nxt_quotes_merged_like_hts() -> None:
    # 통합 시세: 매수는 높은 쪽(NXT), 매도는 낮은 쪽(KRX)을 선택.
    state = MonitorState()
    state.on_quote(q(Instrument.KR_STOCK, 292_500, 293_000, market="krx",
                     bid_qty=100, ask_qty=50))
    state.on_quote(q(Instrument.KR_STOCK, 292_550, 293_050, market="nxt",
                     bid_qty=30, ask_qty=20))

    stock = state.ls_rows()[0]
    # (종목, 매도잔량, 매도가, 현재가, 매수가, 매수잔량, 예상체결가)
    assert stock[2] == "293,000" and stock[1] == "50"   # 매도: KRX가 더 낮음
    assert stock[4] == "292,550" and stock[5] == "30"   # 매수: NXT가 더 높음


def test_hl_rows_include_funding_and_countdown() -> None:
    state = MonitorState()
    state.on_quote(q(Instrument.HL_PERP, 184.55, 184.65, bid_qty=12.5, ask_qty=3.2,
                     market="hl"))
    state.on_mark(Mark(underlying=SAMSUNG, price=184.62))
    state.on_funding(SAMSUNG, 0.0001841)
    state.funding_prev[SAMSUNG] = 0.0001595

    rows = state.hl_rows(now_epoch=3600 * 10 + 3540)  # 정각 60초 전
    assert len(rows) == 3
    samsung = rows[0]
    assert samsung[0] == "삼성전자"
    assert samsung[2] == "184.65" and samsung[4] == "184.55"  # 매도/매수
    assert samsung[3] == "184.62"                             # 현재가(마크)
    assert samsung[6] == "0.0159%" and samsung[7] == "0.0184%"  # 펀딩 직전/예정
    assert samsung[8] == "01:00"                              # 남은시간


def test_funding_countdown_wraps_hourly() -> None:
    assert funding_countdown(0) == "60:00"
    assert funding_countdown(3599) == "00:01"
    assert funding_countdown(3600) == "60:00"
