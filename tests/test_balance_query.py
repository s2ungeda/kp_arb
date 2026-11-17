"""잔고 조회(LS·HL 실조회 → 문자열 표) 테스트 — DESIGN-balance-query.md(2026-11-13, 표 개정 11-16).

장부와 섞이지 않게 문자열만 만드는지, 열 합치기·괄호 값·체결차 부호 식·합 행이 맞는지, 출처별 실패가
그 칸과 체결차만 '오류'로 가는지, 신용이 따로 가는지.
"""
from __future__ import annotations

from typing import Any

from kp_arb.balance_query import (
    COLUMNS,
    balance_line,
    build_table,
    empty_table,
    fmt_qty,
    query_balance,
    status_text,
    total_of,
)
from kp_arb.domain.enums import Account, Instrument, Side, Underlying
from kp_arb.domain.models import Position
from kp_arb.gateways.mock_hl import MockHLGateway
from kp_arb.gateways.mock_ls import MockLSGateway


def _pos(u: Underlying, inst: Instrument, side: Side, qty: float,
         account: Account | None) -> Position:
    return Position(venue=inst.venue, instrument=inst, underlying=u, side=side, qty=qty,
                    avg_price=1.0, account=account)


def test_auto_interval_choice_falls_back_to_first() -> None:
    # 자동조회 주기 콤보(사용자 2026-11-13: 30·60초, 10초는 뺌) — 목록 밖·빈 값은 첫 항목
    from kp_arb.balance_query import AUTO_INTERVALS_S, auto_interval_s

    assert AUTO_INTERVALS_S == (30, 60)
    assert auto_interval_s("30") == 30 and auto_interval_s(" 60 ") == 60
    assert auto_interval_s("") == 30 and auto_interval_s("10") == 30 and auto_interval_s(None) == 30


def test_screen_cells_drop_sign_show_inner_and_keep_diff_sign() -> None:
    # 사용자 2026-11-16: 포지션 칸은 '100(50)'(부호 없이, 방향은 색), 체결차는 부호 그대로,
    # 0 아니면 강조
    from kp_arb import ui_theme as T
    from kp_arb.balance_query_screen import cell_color, diff_bg, display_cell, table_rows

    assert display_cell("+1,200", "+50") == "1,200(50)" and display_cell("-20", "0") == "20(0)"
    assert display_cell("-310", None) == "310" and display_cell("-10", None, signed=True) == "-10"
    assert display_cell("-", "-") == "-" and display_cell("오류", "오류") == "오류"
    assert cell_color("+1,200") == T.C_BUY and cell_color("-20") == T.C_SELL
    assert cell_color("0") == T.C_ZERO and cell_color("오류") == T.C_ERR
    assert cell_color("-") == T.C_MUTED
    assert diff_bg("-10") == T.C_HILITE_BG and diff_bg("0") == "white" and diff_bg("-") == "white"
    # 응답 → 행(종목 3 + 합). 조회 전·실패면 전부 '-'. 응답의 baseline은 화면에 안 그림(기준 행 뺌)
    rows = table_rows(None)
    assert set(rows) == {"sk_hynix", "samsung", "hyundai", "sum"}
    assert rows["sum"]["diff"] == "-"
    res = {"ok": True, "rows": {"samsung": {"hl": "+1"}}, "sum": {"hl": "+1"},
           "baseline": {"rows": {}, "sum": {"hl": "+9"}}}
    rows = table_rows(res)
    assert rows["samsung"]["hl"] == "+1" and rows["sum"]["hl"] == "+1" and "baseline" not in rows


def test_saved_auto_restores_check_and_interval_with_fallbacks() -> None:
    # 사용자 2026-11-16 "자동조회 체크박스 화면저장이 안 된다" — 창 필드에 저장·복원, 이상값은 기본
    from kp_arb.balance_query_screen import saved_auto

    assert saved_auto({}) == (False, "30")
    assert saved_auto({"auto": True, "interval": "60"}) == (True, "60")
    assert saved_auto({"auto": 1, "interval": "10"}) == (True, "30")   # 뺀 주기 → 첫 항목
    assert saved_auto({"auto": False, "interval": None}) == (False, "30")


def test_fmt_qty_signed_with_sparse_decimals() -> None:
    assert fmt_qty(0) == "0" and fmt_qty(1e-12) == "0" and fmt_qty(1e-5) == "0"
    assert fmt_qty(1200) == "+1,200" and fmt_qty(-20) == "-20"
    assert fmt_qty(12.5) == "+12.5" and fmt_qty(-0.25) == "-0.25"


def test_build_table_merges_columns_and_signed_diff() -> None:
    # 사용자 표 예: 퍼프 -310, 주식 100(신용 50), 주선 20(차근 10) → 체결차 100 + 20×10 − 310 = −10
    lots = [
        (_pos(Underlying.SK_HYNIX, Instrument.HL_PERP, Side.SELL, 310, None), False),
        (_pos(Underlying.SK_HYNIX, Instrument.KR_STOCK, Side.BUY, 50, Account.KR_STOCK), False),
        (_pos(Underlying.SK_HYNIX, Instrument.KR_STOCK, Side.BUY, 30, Account.KR_STOCK), True),
        (_pos(Underlying.SK_HYNIX, Instrument.KR_STOCK, Side.BUY, 20, Account.KR_STOCK), True),
        (_pos(Underlying.SK_HYNIX, Instrument.KR_STOCK_FUTURE, Side.BUY, 10, Account.KR_DERIV),
         False),
        (_pos(Underlying.SK_HYNIX, Instrument.KR_STOCK_FUTURE_NEXT, Side.BUY, 10,
              Account.KR_DERIV), False),
        # 삼성: 주식 정방향(주식 +100·HL −100) + 주식선물 정방향(SF −10·HL +100) 동시 → 완전 헤지 0
        # (절대값 식이면 200으로 보였을 경우 — 부호 식을 쓰는 이유, 사용자 2026-11-16 "1 ok")
        (_pos(Underlying.SAMSUNG, Instrument.KR_ETF, Side.BUY, 100, Account.KR_STOCK), False),
        (_pos(Underlying.SAMSUNG, Instrument.KR_STOCK_FUTURE, Side.SELL, 10, Account.KR_DERIV),
         False),
        (_pos(Underlying.SAMSUNG, Instrument.HL_PERP, Side.SELL, 100, None), False),
        (_pos(Underlying.SAMSUNG, Instrument.HL_PERP, Side.BUY, 100, None), False),
        # 원달러선물은 표에 없음
        (_pos(Underlying.SAMSUNG, Instrument.KR_FX_FUTURE, Side.BUY, 1, Account.KR_FX), False),
    ]
    t = build_table(lots)
    assert list(t["rows"]) == ["sk_hynix", "samsung", "hyundai"]      # 사용자 표 행 순서
    assert t["rows"]["sk_hynix"] == {"hl": "-310", "stock": "+100", "credit": "+50", "sf": "+20",
                                     "sf_next": "+10", "diff": "-10"}
    assert t["rows"]["samsung"] == {"hl": "0", "stock": "+100", "credit": "0", "sf": "-10",
                                    "sf_next": "0", "diff": "0"}
    assert t["rows"]["hyundai"] == dict.fromkeys(COLUMNS, "0")
    # 합 = 종목·상품별 절대값 합(토탈잔고 구성), 체결차만 부호 합
    assert t["sum"] == {"hl": "+310", "stock": "+200", "credit": "+50", "sf": "+30",
                        "sf_next": "+10", "diff": "-10"}
    assert all(isinstance(v, str) for row in t["rows"].values() for v in row.values())


def test_build_table_marks_failed_source_and_diff() -> None:
    lots = [(_pos(Underlying.SAMSUNG, Instrument.HL_PERP, Side.SELL, 2, None), False)]
    t = build_table(lots, failed=["ls_stock"])
    row = t["rows"]["samsung"]
    assert row["stock"] == "오류" and row["credit"] == "오류" and row["diff"] == "오류"
    assert row["hl"] == "-2" and row["sf"] == "0" and t["sum"]["diff"] == "오류"
    assert empty_table()["rows"]["hyundai"] == dict.fromkeys(COLUMNS, "-")
    assert empty_table()["sum"] == dict.fromkeys(COLUMNS, "-")


async def test_live_system_balance_query_leaves_ledger_untouched_and_loads_baseline() -> None:
    # DESIGN-balance-query §0: 실조회는 보기 전용 — 장부(OrderBook)·hl_detail은 조회 전후 그대로,
    # 결과는 장부가 아니라 거래소(게이트웨이) 값이다. 둘을 일부러 다르게 심어 확인. 기준 행은 시동
    # 실조회 복사본(§2).
    from kp_arb.bootstrap import LiveSystem
    from kp_arb.gateways.ls_ws import LSWebSocketClient
    from kp_arb.order_book import OrderBook
    from kp_arb.session_service import SessionService

    class _Conn:
        async def connect(self) -> Any:
            raise RuntimeError("이 테스트는 WS를 안 연다")

    ls = MockLSGateway()
    ls.seed_position(_pos(Underlying.SAMSUNG, Instrument.KR_STOCK, Side.BUY, 300,
                          Account.KR_STOCK))
    hl = MockHLGateway()
    hl.seed_position(_pos(Underlying.SAMSUNG, Instrument.HL_PERP, Side.SELL, 300, None))
    book = OrderBook()
    book.load_snapshot(positions=[  # 장부는 거래소와 다른 값(100 / -100)
        _pos(Underlying.SAMSUNG, Instrument.KR_STOCK, Side.BUY, 100, Account.KR_STOCK),
        _pos(Underlying.SAMSUNG, Instrument.HL_PERP, Side.SELL, 100, None)])
    system = LiveSystem(gateway=ls, order_book=book, session=SessionService(),  # type: ignore[arg-type]
                        stock_ws=LSWebSocketClient(_Conn()), hl_gateway=hl)
    system.hl_detail = {Underlying.SAMSUNG: {"margin": 1.0}}
    before = sorted((p.instrument.value, p.signed_qty) for p in book.positions())
    changed = {"n": 0}
    book.on_change.append(lambda: changed.__setitem__("n", changed["n"] + 1))

    assert system.balance_baseline is None
    await system.load_balance_baseline()          # 시동 때 1회 — 기준 행
    assert system.balance_baseline is not None
    assert system.balance_baseline["rows"]["samsung"]["stock"] == "+300"
    hl.seed_position(_pos(Underlying.SAMSUNG, Instrument.HL_PERP, Side.SELL, 10, None))  # 변동
    out = await system.balance_query()

    assert out["rows"]["samsung"]["stock"] == "+300" and out["rows"]["samsung"]["hl"] == "-310"
    assert out["rows"]["samsung"]["diff"] == "-10"
    assert out["baseline"]["rows"]["samsung"]["hl"] == "-300"   # 기준은 시동 값 그대로
    assert sorted((p.instrument.value, p.signed_qty) for p in book.positions()) == before
    assert before == [("hl_perp", -100.0), ("kr_stock", 100.0)]  # 장부는 그대로 100 / -100
    assert changed["n"] == 0  # 장부 변경 콜백 한 번도 안 울림
    assert system.hl_detail == {Underlying.SAMSUNG: {"margin": 1.0}}


async def test_query_balance_keeps_other_sources_when_one_fails() -> None:
    ls = MockLSGateway()
    ls.seed_position(_pos(Underlying.SAMSUNG, Instrument.KR_STOCK, Side.BUY, 100,
                          Account.KR_STOCK))
    ls.seed_credit_position(_pos(Underlying.SAMSUNG, Instrument.KR_STOCK, Side.BUY, 40,
                                 Account.KR_STOCK))
    ls.seed_position(_pos(Underlying.HYUNDAI, Instrument.KR_STOCK_FUTURE, Side.SELL, 4,
                          Account.KR_DERIV))
    hl = MockHLGateway()
    hl.seed_position(_pos(Underlying.SAMSUNG, Instrument.HL_PERP, Side.SELL, 140, None))
    out = await query_balance(ls, hl, now=0.0)
    assert out["ok"] and out["errors"] == {} and out["baseline"] is None
    assert out["rows"]["samsung"] == {"hl": "-140", "stock": "+140", "credit": "+40", "sf": "0",
                                      "sf_next": "0", "diff": "0"}
    assert out["rows"]["hyundai"]["sf"] == "-4" and out["rows"]["hyundai"]["diff"] == "-40"

    # 선물 계좌 조회가 터져도 주식·HL은 채우고, 선물 칸·체결차만 '오류' + 사유
    async def _boom(account: Account) -> Any:
        if account is Account.KR_DERIV:
            raise RuntimeError("t0441 거부")
        return [(p, False) for p in ls._positions[account]]

    ls.get_positions_detail = _boom  # type: ignore[method-assign]
    out = await query_balance(ls, None, now=0.0, baseline={"rows": {}, "sum": {}})
    assert set(out["errors"]) == {"ls_deriv", "hl"}
    assert out["errors"]["ls_deriv"] == "RuntimeError: t0441 거부"
    assert out["rows"]["hyundai"]["sf"] == "오류" and out["rows"]["hyundai"]["diff"] == "오류"
    assert out["rows"]["samsung"]["stock"] == "+140" and out["rows"]["samsung"]["hl"] == "오류"
    assert out["baseline"] == {"rows": {}, "sum": {}}
    assert "LS 선물(RuntimeError: t0441 거부)" in status_text(out)
    assert status_text(None).startswith("조회 전")
    assert status_text({"ok": True, "ts": "09:00:00", "errors": {}}) == "09:00:00 조회 완료"
    assert status_text({"ok": False, "errors": ["조회 중 — 잠시 뒤 다시"]}).startswith("조회 실패")


def test_total_of_and_balance_line_from_query_result() -> None:
    # 상태줄 오른쪽 '기준 N  잔고 : M'(사용자 2026-11-17): 기준 = 시동 때 실조회한 토탈잔고(응답
    # baseline), 잔고 = 이번 조회의 토탈잔고 = 합 행 주식 + 주선×10 + HL(절대값, 실조회)
    cur = {"hl": "+310", "stock": "+100", "credit": "+50", "sf": "+20", "sf_next": "+10",
           "diff": "-10"}
    base = {"hl": "+300", "stock": "+0", "credit": "+0", "sf": "+0", "sf_next": "+0", "diff": "0"}
    assert total_of(cur) == 100 + 20 * 10 + 310 and total_of(base) == 300
    assert total_of({"hl": "오류", "stock": "+1", "sf": "+0"}) is None  # 한 출처 실패 → 없음
    assert total_of({"hl": "-", "stock": "-", "sf": "-"}) is None and total_of(None) is None
    res = {"ok": True, "ts": "09:00:00", "errors": {}, "rows": {}, "sum": cur,
           "baseline": {"rows": {}, "sum": base}}
    assert balance_line(res) == "기준 300  잔고 : 610"
    assert balance_line({**res, "baseline": None}) == "기준 -  잔고 : 610"  # 시동 복사본 실패
    assert balance_line(None) == "기준 -  잔고 : -"
    assert balance_line({"ok": False, "errors": ["조회 중"]}) == "기준 -  잔고 : -"
