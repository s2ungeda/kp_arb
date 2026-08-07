"""HL 일반주문창 순수 표시 헬퍼 테스트 (tkinter 미로드 — 함수만)."""
from kp_arb.order_hl import (
    _fmt,
    _fmt_px,
    _fmt_qty,
    _hl_decimals,
    _hoga_signature,
    _merge_ticks,
    _order_confirm_text,
)


def test_order_confirm_text() -> None:
    txt = _order_confirm_text("하이닉스", "매수", 10.0, "1106.4")
    assert "하이닉스 매수" in txt
    assert "수량 10" in txt and "단가 1106.4" in txt
    assert "주문하시겠습니까?" in txt
    # 소수 수량은 :g로 깔끔하게 (10.0 → 10, 0.5 → 0.5)
    assert "수량 0.5" in _order_confirm_text("삼성", "매도", 0.5, "167.5")


def test_fmt() -> None:
    assert _fmt(None) == "-"
    assert _fmt(1234567) == "1,234,567"          # 천단위 콤마
    assert _fmt(80000.5, 1) == "80,000.5"
    assert _fmt("N/A") == "N/A"                    # 숫자 아니면 그대로


def test_fmt_qty() -> None:
    assert _fmt_qty(None) == "-"
    assert _fmt_qty(100) == "100"
    assert _fmt_qty(0.179) == "0.179"            # HL 잔량 소수
    assert _fmt_qty(29.0) == "29"                 # 소수부 없으면 정수
    assert _fmt_qty(1234.5) == "1,234.5"


def test_fmt_px_shows_decimals_for_fractional() -> None:
    assert _fmt_px(None) == "-"
    assert _fmt_px(80000) == "80,000"            # 정수
    assert _fmt_px(167.05) == "167.05"           # 소수 — 100 넘어도 안 뭉갬
    assert _fmt_px(167.10) == "167.1"            # 끝 0 제거(자동)
    assert _fmt_px(1.0683) == "1.0683"


def test_fmt_px_fixed_decimals() -> None:
    # decimals 지정 시 그 자리수로 통일(호가 정렬)
    assert _fmt_px(167.1, 2) == "167.10"
    assert _fmt_px(167, 2) == "167.00"


def test_hl_decimals_from_price_magnitude() -> None:
    # 유효숫자 5자리 규칙 — 정수부 자리수 기준(종목별 사실상 고정, 호가 바뀌어도 안 흔들림)
    assert _hl_decimals(1121.6) == 1      # 정수부 4자리 → 5-4
    assert _hl_decimals(16.5) == 3        # 2자리 → 3
    assert _hl_decimals(1.0683) == 4      # <10 → 1자리 → 4
    assert _hl_decimals(12345) == 0       # 5자리 → 0
    assert _hl_decimals(None) == 2        # 이상값 기본 2


def test_merge_ticks_from_price() -> None:
    # 기준틱 = 10^(floor(log10 가격)-4), 배수 [1,2,5,10,100,1000] (유효숫자 5자리 규칙)
    hynix = _merge_ticks(1121.6)   # 기준틱 0.1
    assert [s for s, _, _ in hynix] == ["0.1", "0.2", "0.5", "1", "10", "100"]
    assert hynix[0][1:] == (None, None)   # 원시
    assert hynix[1][1:] == (5, 2)          # ×2
    assert hynix[3][1:] == (4, None)       # ×10
    low = _merge_ticks(600.0)      # 기준틱 0.01
    assert [s for s, _, _ in low] == ["0.01", "0.02", "0.05", "0.1", "1", "10"]
    assert _merge_ticks(0) == []           # 가격 0/음수는 빈 목록


def test_hoga_signature_detects_change() -> None:
    rows = [("ask", 80000, 20), ("cur", 79950, ""), ("bid", 79900, 10)]
    assert _hoga_signature(rows) == (
        ("ask", 80000, 20), ("cur", 79950, ""), ("bid", 79900, 10))
    rows2 = [("ask", 80000, 21), ("cur", 79950, ""), ("bid", 79900, 10)]
    assert _hoga_signature(rows) != _hoga_signature(rows2)
