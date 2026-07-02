"""모니터 데이터 수집부(MonitorState) 테스트 — 창(tkinter)은 수동 확인."""
from kp_arb.domain.enums import Instrument, Underlying
from kp_arb.domain.models import Quote
from kp_arb.gateways.hl import Mark
from kp_arb.monitor import MonitorState

SAMSUNG = Underlying.SAMSUNG


def q(instrument: Instrument, bid: float, ask: float) -> Quote:
    return Quote(underlying=SAMSUNG, instrument=instrument, bid=bid, ask=ask, ts=1.0)


def test_rows_cover_all_11_instruments() -> None:
    # 3종목 × (주식/선물/ETF/HL) = 12행 (현대차 ETF는 미수신 '-'로 표시).
    state = MonitorState()
    rows = state.rows()
    assert len(rows) == 12
    assert all(r[2] == "-" for r in rows)  # 아직 아무 시세도 없음


def test_quote_and_mark_reflected() -> None:
    state = MonitorState()
    state.on_quote(q(Instrument.KR_STOCK, 292_500, 293_000))
    state.on_quote(q(Instrument.KR_STOCK_FUTURE, 293_000, 293_500))
    state.on_quote(q(Instrument.KR_ETF, 17_595, 17_605))
    state.on_mark(Mark(underlying=SAMSUNG, price=184.62))

    rows = state.rows()
    stock, fut, etf, hl = rows[0], rows[1], rows[2], rows[3]
    assert stock == ("삼성전자", "주식", "292,500", "293,000", "292,750")
    assert fut[1] == "선물" and fut[2] == "293,000"
    assert etf[1] == "ETF" and etf[4] == "17,600"
    assert hl[1] == "HL" and hl[4] == "184.62"
    assert state.last_update > 0


def test_name_shown_once_per_underlying() -> None:
    rows = MonitorState().rows()
    names = [r[0] for r in rows]
    assert names.count("삼성전자") == 1
    assert names.count("SK하이닉스") == 1
    assert names.count("현대차") == 1
