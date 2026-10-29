"""동시호가 주문 화면 — 코어 명령 본문·표시 글(순수)."""
from kp_arb.auction_order_screen import (
    book_texts,
    hl_buttons_enabled,
    hl_fill_text,
    hl_line,
    num_text,
    pct2,
    px_style,
    px_text,
    result_line,
    row_texts,
    rows_payload,
    settings_payload,
)


def test_rows_and_settings_payload_from_entry_texts() -> None:
    # 스프레드 칸은 %(체결쏴와 같음) → 코어엔 소수(2026-10-26 시험: 그대로 보내 10%로 역산됐었음)
    body = rows_payload("sk_hynix", [("0.3", "20", True), ("", "", False)], [("-0.1", "5", False)])
    assert body["cmd"] == "auction_rows" and body["buy"][0] == {"spread": 0.003, "qty": 20,
                                                                  "on": True}
    assert body["buy"][1] == {"spread": None, "qty": 0, "on": False}
    assert body["sell"][0]["spread"] == -0.001 and body["sell"][0]["qty"] == 5
    texts = {"ls_hms": "15:44:55", "ls_ms": "100", "ls_itv": "200", "fx_hms": "15:44:58",
             "fx_ms": "200", "fx_itv": "", "tick": "20", "hedge_pct": "100", "fx_unit": "10"}
    st = settings_payload("samsung", texts, "next", {"a": "0.5", "b": ""}, {"a": "0.7", "b": ""})
    assert st["cmd"] == "auction_settings" and st["month"] == "next"
    assert (st["ls_ms"], st["ls_itv_ms"], st["fx_itv_ms"]) == (100, 200, 0)
    assert st["hl_off"] == {"a": 0.5, "b": 0.0} and st["hl_pt"]["a"] == 0.7
    # 틱 음수 허용(사용자 2026-10-27) — '-'만 친 상태는 기본값 0
    assert settings_payload("samsung", {**texts, "tick": "-15"}, "near", {}, {})["tick"] == -15
    assert settings_payload("samsung", {**texts, "tick": "-"}, "near", {}, {})["tick"] == 0


def test_book_to_entry_texts_and_status_lines() -> None:
    book = {"settings": {"ls_hms": "15:44:50", "ls_ms": 150, "fx_itv_ms": 300, "tick": 25},
            "month": "next"}
    t = book_texts(book)
    assert (t["ls_hms"], t["ls_ms"], t["fx_itv"], t["tick"]) == ("15:44:50", "150", "300", "25")
    assert t["fx_hms"] == "15:44:58" and t["hedge_pct"] == "100"   # 없는 키는 기본값
    assert row_texts({"spread": 0.003, "qty": 20, "on": True}) == ("0.3", "20", True)
    assert row_texts({"spread": -0.001, "qty": 5}) == ("-0.1", "5", False)
    assert row_texts({}) == ("", "", False)
    assert num_text(0.0) == "" and num_text(-0.5) == "-0.5" and px_text(1_767_000.0) == "1,767,000"
    assert pct2(-0.000775) == "-0.08" and pct2(0.0021) == "0.21" and pct2(None) == "-"
    assert result_line("buy", 0, {"order_id": "123", "qty": 20, "price": 1_767_000.0}) == (
        "매수1 발주 #123 20@1,767,000")
    assert result_line("sell", 2, {"error": "LS 거부"}) == "매도3 실패 LS 거부"
    assert result_line("buy", 1, {"skipped": "1,765,000 ≤ 예상체결가 1,766,000"}) == (
        "매수2 안 냄 — 1,765,000 ≤ 예상체결가 1,766,000")
    assert hl_line("a", {"status": "접수", "order_id": "9", "qty": 7.5, "price": 1333.0,
                         "remaining": 7.5}) == "HL a 접수 #9 7.5@1333.0 잔량 7.5"
    assert hl_line("b", {"status": "실패", "error": "x"}) == "HL b 실패 x"
    assert hl_buttons_enabled({"open": True}) and not hl_buttons_enabled({"open": False})
    # HL체결 한 줄(§5 체결 표시, 2026-10-29): 낸 주문 없음 "-", 체결 전 "매도 0/30", 전량이면 '완료'
    assert hl_fill_text(None) == "-" and hl_fill_text({"total": 0.0}) == "-"
    assert hl_fill_text({"side": "sell", "qty": 0.0, "total": 30.0, "avg": None}) == "매도 0/30"
    assert hl_fill_text({"side": "sell", "qty": 10.0, "total": 30.0, "avg": 1332.2}) == (
        "매도 10/30 평균 1332.2")
    assert hl_fill_text({"side": "buy", "qty": 30.0, "total": 30.0, "avg": 1332.25}) == (
        "매수 30/30 평균 1332.25 완료")


def test_px_style_marks_rows_expected_to_fire() -> None:
    # 사용자 2026-10-26: 예약 걸어 둔 상태에서 지금 값으로 나갈 행은 연두 바탕, 발주/실패/안 냄은
    # 글자색
    assert px_style(None, True) == ("black", "white")   # 나갈 행 = 흰 바탕(2026-10-27, 연두에서)
    assert px_style(None, False) == ("gray25", "SystemButtonFace")
    assert px_style({"order_id": "1"}, True)[0] == "dark green"
    assert px_style({"error": "x"}, False)[0] == "red"
    assert px_style({"skipped": "y"}, False)[0] == "dark orange"

