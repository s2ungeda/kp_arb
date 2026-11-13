"""잔고 조회(LS·HL 실조회 → 문자열 표) 테스트 — DESIGN-balance-query.md(사용자 2026-11-13).

장부와 섞이지 않게 문자열만 만드는지, 출처별 실패가 그 열만 '오류'로 가는지, 신용이 따로 가는지.
"""
from __future__ import annotations

from typing import Any

from kp_arb.balance_query import (
    COLUMNS,
    build_cells,
    empty_cells,
    fmt_qty,
    query_balance,
    status_text,
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
    # 자동조회 주기 콤보(사용자 2026-11-13: 10·30·60초) — 목록 밖·빈 값은 첫 항목
    from kp_arb.balance_query import AUTO_INTERVALS_S, auto_interval_s

    assert AUTO_INTERVALS_S == (10, 30, 60)
    assert auto_interval_s("30") == 30 and auto_interval_s(" 60 ") == 60
    assert auto_interval_s("") == 10 and auto_interval_s("5") == 10 and auto_interval_s(None) == 10


def test_fmt_qty_signed_with_sparse_decimals() -> None:
    assert fmt_qty(0) == "0" and fmt_qty(1e-12) == "0"
    assert fmt_qty(1200) == "+1,200" and fmt_qty(-20) == "-20"
    assert fmt_qty(12.5) == "+12.5" and fmt_qty(-0.25) == "-0.25"


def test_build_cells_places_each_instrument_and_sums_signed_qty() -> None:
    lots = [
        (_pos(Underlying.SK_HYNIX, Instrument.HL_PERP, Side.BUY, 100, None), False),
        (_pos(Underlying.SK_HYNIX, Instrument.KR_STOCK, Side.BUY, 30, Account.KR_STOCK), False),
        (_pos(Underlying.SK_HYNIX, Instrument.KR_STOCK, Side.BUY, 20, Account.KR_STOCK), True),
        (_pos(Underlying.SK_HYNIX, Instrument.KR_STOCK, Side.BUY, 5, Account.KR_STOCK), True),
        (_pos(Underlying.SK_HYNIX, Instrument.KR_STOCK_FUTURE, Side.SELL, 10,
              Account.KR_DERIV), False),
        (_pos(Underlying.SAMSUNG, Instrument.KR_STOCK_FUTURE_NEXT, Side.BUY, 3,
              Account.KR_DERIV), False),
        (_pos(Underlying.SAMSUNG, Instrument.KR_ETF, Side.BUY, 7, Account.KR_STOCK), False),
        # 원달러선물은 표에 없음
        (_pos(Underlying.SAMSUNG, Instrument.KR_FX_FUTURE, Side.BUY, 1, Account.KR_FX), False),
    ]
    cells = build_cells(lots)
    assert list(cells) == ["sk_hynix", "samsung", "hyundai"]      # 목업 행 순서
    assert cells["sk_hynix"] == {"hl": "+100", "stock": "+30", "credit": "+25",
                                 "sf_near": "-10", "sf_next": "0"}
    assert cells["samsung"] == {"hl": "0", "stock": "+7", "credit": "0", "sf_near": "0",
                                "sf_next": "+3"}
    assert cells["hyundai"] == dict.fromkeys(COLUMNS, "0")
    assert all(isinstance(v, str) for row in cells.values() for v in row.values())


def test_build_cells_marks_failed_source_columns_only() -> None:
    lots = [(_pos(Underlying.SAMSUNG, Instrument.HL_PERP, Side.SELL, 2, None), False)]
    cells = build_cells(lots, failed=["ls_stock"])
    assert cells["samsung"]["stock"] == "오류" and cells["samsung"]["credit"] == "오류"
    assert cells["samsung"]["hl"] == "-2" and cells["samsung"]["sf_near"] == "0"
    assert empty_cells()["hyundai"] == dict.fromkeys(COLUMNS, "-")


async def test_live_system_balance_query_leaves_ledger_untouched() -> None:
    # DESIGN-balance-query §0: 실조회는 보기 전용 — 장부(OrderBook)·hl_detail은 조회 전후 그대로,
    # 결과는 장부가 아니라 거래소(게이트웨이) 값이다. 둘을 일부러 다르게 심어 확인.
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

    out = await system.balance_query()

    assert out["cells"]["samsung"]["stock"] == "+300" and out["cells"]["samsung"]["hl"] == "-300"
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
    assert out["ok"] and out["errors"] == {}
    assert out["cells"]["samsung"] == {"hl": "-140", "stock": "+100", "credit": "+40",
                                       "sf_near": "0", "sf_next": "0"}
    assert out["cells"]["hyundai"]["sf_near"] == "-4"

    # 선물 계좌 조회가 터져도 주식·HL은 채우고, 선물 열만 '오류' + 사유
    async def _boom(account: Account) -> Any:
        if account is Account.KR_DERIV:
            raise RuntimeError("t0441 거부")
        return [(p, False) for p in ls._positions[account]]

    ls.get_positions_detail = _boom  # type: ignore[method-assign]
    out = await query_balance(ls, None, now=0.0)
    assert set(out["errors"]) == {"ls_deriv", "hl"}
    assert out["errors"]["ls_deriv"] == "RuntimeError: t0441 거부"
    assert out["cells"]["hyundai"]["sf_near"] == "오류"
    assert out["cells"]["samsung"]["stock"] == "+140" and out["cells"]["samsung"]["hl"] == "오류"
    assert "LS 선물(RuntimeError: t0441 거부)" in status_text(out)
    assert status_text(None).startswith("조회 전")
    assert status_text({"ok": True, "ts": "09:00:00", "errors": {}}) == "09:00:00 조회 완료"
    assert status_text({"ok": False, "errors": ["조회 중 — 잠시 뒤 다시"]}).startswith("조회 실패")
