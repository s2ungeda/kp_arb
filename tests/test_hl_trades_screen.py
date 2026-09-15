"""HL 체결 창(hl_trades) — 코어 보관·행 변환·표 칸(순수 함수). 창은 띄우지 않는다."""
from __future__ import annotations

from datetime import datetime

from kp_arb.bootstrap import HL_TRADES_KEEP, hl_trade_row, hl_trade_rows
from kp_arb.domain.enums import Instrument, Underlying
from kp_arb.gateways.ls_ws import TradeTick
from kp_arb.hl_trades import rows_signature, side_tag, trade_row_values


def _tick(ts_ms: float, side: str | None, px: float, sz: float) -> TradeTick:
    return TradeTick(underlying=Underlying.SK_HYNIX, instrument=Instrument.HL_PERP, price=px,
                     ts=ts_ms, market="hl", side=side, qty=sz)


def test_hl_trade_row_formats_local_time_side_price_qty() -> None:
    # 사용자 2026-09-15: 체결시각·매도/매수·체결가·체결수량. 시각은 HL epoch ms → 로컬 HH:MM:SS.mmm.
    ts = datetime(2026, 9, 15, 10, 30, 5, 123000).timestamp() * 1000
    row = hl_trade_row(_tick(ts, "buy", 1260.74, 2.197))
    assert row["time"] == "10:30:05.123" and row["side"] == "buy"
    assert row["price"] == 1260.74 and row["qty"] == 2.197 and row["ts"] == ts
    assert hl_trade_row(_tick(0, None, 1.0, 0))["time"] == "-"  # 시각 없음


def test_hl_trade_rows_newest_first_and_keep_is_30() -> None:
    assert HL_TRADES_KEEP == 30  # 사용자 확정 2026-09-15: 30줄
    kept = [hl_trade_row(_tick(1000 + i, "sell", 1.0 + i, 1)) for i in range(3)]
    rows = hl_trade_rows(kept)
    assert [r["price"] for r in rows] == [3.0, 2.0, 1.0]  # 최신이 위


def test_lag_meter_reports_per_window() -> None:
    # 수신 지연 계측(2026-09-15): 10초 창이 차면 (건수, 평균, 최대)를 주고 새 창.
    from kp_arb.bootstrap import LagMeter

    m = LagMeter(window_s=10.0)
    assert m.add(100.0, 1000.0) is None
    assert m.add(300.0, 1005.0) is None
    assert m.add(200.0, 1010.0) == (3, 200.0, 300.0)  # 창 마감 → 집계
    assert m.add(50.0, 1011.0) is None and m.count == 1  # 새 창


def test_screen_cells_and_tags() -> None:
    row = {"time": "10:30:05.123", "side": "sell", "price": 1260.5, "qty": 0.15, "ts": 1.0}
    assert trade_row_values(row) == ("10:30:05.123", "매도", "1260.5", "0.15")
    assert side_tag(row) == "sell"
    row2 = {"time": "10:30:06.000", "side": "buy", "price": 1261.0, "qty": 20.0, "ts": 2.0}
    assert trade_row_values(row2) == ("10:30:06.000", "매수", "1261", "20")
    assert side_tag(row2) == "buy" and side_tag({"side": ""}) == "zero"
    assert trade_row_values({}) == ("-", "-", "-", "-")
    assert rows_signature([row, row2]) != rows_signature([row2, row])  # 순서도 서명에 포함
