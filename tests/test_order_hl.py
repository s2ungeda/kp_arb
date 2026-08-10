"""HL 일반주문창 순수 표시 헬퍼 테스트 (tkinter 미로드 — 함수만)."""
from kp_arb.order_hl import (
    _fmt,
    _fmt_px,
    _fmt_qty,
    _hl_decimals,
    _hoga_signature,
)


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


# 호가단위(틱) 계산은 코어 순수모듈로 이전 → tests/test_hl_merge.py에서 검증.


def test_hoga_signature_detects_change() -> None:
    # (구분, 가격, 건수, 잔량) — 건수 칼럼 분리 후 4-튜플
    rows = [("ask", 80000, "", 20), ("cur", 79950, "(1)", 5), ("bid", 79900, "", 10)]
    assert _hoga_signature(rows) == (
        ("ask", 80000, "", 20), ("cur", 79950, "(1)", 5), ("bid", 79900, "", 10))
    rows2 = [("ask", 80000, "", 21), ("cur", 79950, "(1)", 5), ("bid", 79900, "", 10)]
    assert _hoga_signature(rows) != _hoga_signature(rows2)
    # 건수만 달라져도(잔량 동일) 다시그리기 감지
    rows3 = [("ask", 80000, "(2)", 20), ("cur", 79950, "(1)", 5), ("bid", 79900, "", 10)]
    assert _hoga_signature(rows) != _hoga_signature(rows3)
