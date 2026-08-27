"""자동M 리스크방지 입력 검증(check_risk) 단위 테스트 (DESIGN-auto-m §10)."""
from __future__ import annotations

from kp_arb.order_autom import check_risk

# 공통설정 기본값 (목업): 진입 0, 청산 0.5, gap 0.1
EN, EX, GAP = 0.0, 0.5, 0.1


def test_fwd_valid() -> None:
    # 진입 0.5(>0)·0.5(>0), 청산 -0.1(<0.5), 진입−청산 0.6(>0.1)
    assert check_risk("fwd", 0.5, 0.5, -0.1, EN, EX, GAP) == []


def test_fwd_entry_not_above_zero() -> None:
    errs = check_risk("fwd", 0.0, -0.2, -0.1, EN, EX, GAP)
    assert any("진입SF" in e for e in errs)
    assert any("진입S" in e for e in errs)


def test_fwd_exit_not_below_half() -> None:
    errs = check_risk("fwd", 0.5, 0.5, 0.5, EN, EX, GAP)
    assert any("청산" in e for e in errs)


def test_fwd_gap_too_small() -> None:
    # 진입 0.15, 청산 0.1 → 차이 0.05 ≤ 0.1
    errs = check_risk("fwd", 0.15, 0.15, 0.1, EN, EX, GAP)
    assert any("진입SF−청산" in e for e in errs)


def test_rev_valid() -> None:
    # 진입 -1.5(<0)·-1.5(<0), 청산 0.9(>0.5), 청산−진입 2.4(>0.1)
    assert check_risk("rev", -1.5, -1.5, 0.9, EN, EX, GAP) == []


def test_rev_entry_not_below_zero() -> None:
    errs = check_risk("rev", 0.0, 0.1, 0.9, EN, EX, GAP)
    assert any("진입SF" in e for e in errs)
    assert any("진입S" in e for e in errs)


def test_rev_exit_not_above_half() -> None:
    errs = check_risk("rev", -1.5, -1.5, 0.5, EN, EX, GAP)
    assert any("청산" in e for e in errs)


def test_rev_gap_too_small() -> None:
    # 청산 -0.9, 진입 -0.95 → 차이 0.05 ≤ 0.1
    errs = check_risk("rev", -0.95, -0.95, -0.9, EN, EX, GAP)
    assert any("청산−진입SF" in e for e in errs)


def test_none_values_skipped() -> None:
    # 미입력(None)은 검증 건너뜀 — 빈 화면은 통과
    assert check_risk("fwd", None, None, None, EN, EX, GAP) == []
    # 청산만 있고 진입 없으면 gap 검증 안 함
    assert check_risk("fwd", None, None, -0.1, EN, EX, GAP) == []
