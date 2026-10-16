"""쌍 대조 로그(exec 결정 83, 2026-10-16) — 순수 계산·글·언제 남길지."""
from __future__ import annotations

from kp_arb.auto_m import AutoMBook, book_key
from kp_arb.domain.enums import Account, Instrument, Side, Underlying, Venue
from kp_arb.domain.models import Position
from kp_arb.pair_check import PairLogger, format_row, pair_rows


def _pos(u: Underlying, inst: Instrument, qty: float, account: Account | None = None) -> Position:
    return Position(venue=inst.venue if inst is not Instrument.HL_PERP else Venue.HYPERLIQUID,
                    instrument=inst, underlying=u, side=Side.BUY if qty > 0 else Side.SELL,
                    qty=abs(qty), avg_price=1.0, account=account)


def test_pair_rows_matches_today_flow_and_flags_manual_hl() -> None:
    # 10-16 운영 실측 모양: 주식 350주 보유, 주식선물 근 −30·차근 −9(=−390), HL +40 → 실제 차이 0.
    # 세트 장부: 주식 3세트 SF −390(주 단위, 비율 1) HL +390 체결차 0 / 주식선물 1세트 SF −10
    # HL +100.
    U = Underlying.SK_HYNIX
    positions = [_pos(U, Instrument.KR_STOCK, 350, Account.KR_STOCK),
                 _pos(U, Instrument.KR_STOCK_FUTURE, -30, Account.KR_DERIV),
                 _pos(U, Instrument.KR_STOCK_FUTURE_NEXT, -9, Account.KR_DERIV),
                 _pos(U, Instrument.HL_PERP, 40)]
    stock_book = AutoMBook()
    s = stock_book.sets[2]
    s.product, s.sf_net, s.hl_net, s.fill_diff = "stock", -390, 390.0, 0.0
    sf_book = AutoMBook()
    f = sf_book.sets[0]
    f.sf_net, f.hl_net, f.fill_diff = -10, 100.0, 0.0
    books = {book_key(U, "stock"): stock_book, book_key(U): sf_book}
    rows = pair_rows(positions, books)
    assert len(rows) == 1
    r = rows[0]
    assert (r.stock, r.sf, r.hl) == (350, -39, 40)
    assert r.actual_diff == 0 and (r.book_dom, r.book_hl, r.book_diff) == (-490, 490, 0)
    assert r.unexplained == 0
    assert format_row(r) == ("[쌍대조] sk_hynix 주식 +350 선물 -39(=-390) HL +40 → 실제 차이 +0 | "
                             "장부 국내 -490 HL +490 체결차 +0 | 장부 밖 차이 +0")
    # 일반주문창으로 HL 40을 더 사면: 실제 차이 +40, 세트는 모르니 '장부 밖 차이 +40'
    positions[-1] = _pos(U, Instrument.HL_PERP, 80)
    r2 = pair_rows(positions, books)[0]
    assert r2.actual_diff == 40 and r2.book_diff == 0 and r2.unexplained == 40
    # 세트 체결차가 남아 있으면(HL선 잔차) 그만큼은 '장부' 쪽으로 설명된다
    f.hl_net, f.fill_diff = 90.0, -10.0
    r3 = pair_rows(positions, books)[0]
    assert r3.book_diff == -10 and r3.unexplained == 50


def test_pair_rows_skips_all_zero_and_unknown_books() -> None:
    books = {"samsung|stock": AutoMBook(), "what|ever": AutoMBook()}
    assert pair_rows([], books) == []
    rows = pair_rows([_pos(Underlying.SAMSUNG, Instrument.HL_PERP, -0.5)], books)
    assert len(rows) == 1 and rows[0].hl == -0.5 and rows[0].actual_diff == -0.5
    assert "HL -0.5" in format_row(rows[0])


def test_pair_logger_emits_on_change_or_heartbeat() -> None:
    U = Underlying.SAMSUNG
    lg = PairLogger(heartbeat_s=600)
    rows = pair_rows([_pos(U, Instrument.HL_PERP, 10)], {})
    assert len(lg.tick(rows, 0.0)) == 1          # 처음은 남긴다
    assert lg.tick(rows, 60.0) == []             # 같으면 조용히
    assert lg.tick(rows, 599.0) == []
    assert len(lg.tick(rows, 600.0)) == 1        # 10분마다 한 줄
    rows2 = pair_rows([_pos(U, Instrument.HL_PERP, 11)], {})
    assert len(lg.tick(rows2, 601.0)) == 1       # 값이 바뀌면 바로
    assert lg.tick([], 602.0) == ["[쌍대조] 포지션·세트 장부 모두 0"]
