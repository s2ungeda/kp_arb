"""자동M 정산 Sprd CSV 리포트 — 로그 줄 파싱·판 확정·Sprd 재계산(순수 로직, 결정 48)."""
from pathlib import Path

import pytest

from kp_arb.report_sprd import collect, parse_rounds, round_sprd, write_csv

# 실측 로그 줄(2026-09-28)을 그대로 — 주식선물 삼성 10:15 판(HL 부분체결 → 대기 1.954, 미완)
SF_PRE = ("2026-09-28 10:15:38,455 INFO 체결 정방향 1세트 진입: 선주문 #8293 1 @ 270000 "
          "기준est 201.41 현est 201.47 → 누적 1/1, HL 대기 10 | 장부 SF 1 HL 0 체결차 10")
SF_POST_PARTIAL = (
    "2026-09-28 10:15:39,618 INFO 체결 정방향 1세트 진입: 후주문 #558381010838 HL 8.046 "
    "@ 201.22 기준est 201.41 차이 -0.19(-0.094%) 현est 201.198 환진입가 1358.2 "
    "S현재가 274750.0 SF이론가 274,976 → RT 8 HL대기 1.954 | 장부 SF 1 HL -8.046 체결차 1.954 "
    "| 누적 HL 6.053 SF 0.6053 환평균 1356.9 HL평균 182.783 SF평균 244000.0 Sprd 1.806%")
SF_POST_REST = (
    "2026-09-28 10:15:40,100 INFO 체결 정방향 1세트 진입: 후주문 #558381010839 HL 1.954 "
    "@ 201.30 기준est 201.41 차이 -0.11(-0.055%) 현est 201.2 환진입가 1358.2 "
    "S현재가 274750.0 SF이론가 274,976 → RT 8 HL대기 0 | 장부 SF 1 HL -10 체결차 0 "
    "| 누적 HL 16.053 SF 1.6053 환평균 1357.7 HL평균 194.3 SF평균 260200.0 Sprd 1.234%")
# 주식 하이닉스 13:52 — 1판 부분체결(대기 0.073) 뒤 2판 선체결·후주문 거부 → halt(둘 다 미완)
ST_PRE1 = ("2026-09-28 13:52:28,237 INFO 체결 주식 정방향 1세트 진입: 선주문 #14092 1 "
           "@ 1.783e+06 기준est 1315.2 현est 1315.2 → 누적 1/1, HL 대기 1 | 장부 SF 1 HL 0 "
           "체결차 1")
ST_POST1 = ("2026-09-28 13:52:29,317 INFO 체결 주식 정방향 1세트 진입: 후주문 #558558412882 "
            "HL 0.927 @ 1315.2 기준est 1315.2 차이 +0(+0.000%) 현est 1315.2 환진입가 1357.85 "
            "S현재가 1783000.0 SF이론가 None → RT 1 HL대기 0.073 | 장부 SF 1 HL -0.927 "
            "체결차 0.073 | 누적 HL 0 SF 0 환평균 None HL평균 None SF평균 None Sprd -(판 미완)")
ST_PRE2 = ("2026-09-28 13:52:30,841 INFO 체결 주식 정방향 1세트 진입: 선주문 #14093 1 "
           "@ 1.784e+06 기준est 1315.2 현est 1315.2 → 누적 1/1, HL 대기 1 | 장부 SF 2 "
           "HL -0.927 체결차 1.073")
ST_HALT = ("2026-09-28 13:52:31,618 INFO 행동 주식 정방향 1세트 진입: halt    체결차 1.073 ≥ "
           "한도 1(1회주문수량 1×10) — 후주문 미체결 HL 1계약")
# 주식 삼성 10:03 청산 — 완결 판(선 1주 @277,000 → HL 매수 1 @202.63)
ST_C_PRE = ("2026-09-28 10:03:52,465 INFO 체결 주식 정방향 1세트 청산: 선주문 #4763 1 @ 277000 "
            "기준est 203 현est 203 → 누적 1/1, HL 대기 1 | 장부 SF -1 HL 0 체결차 -1")
ST_C_POST = ("2026-09-28 10:03:53,592 INFO 체결 주식 정방향 1세트 청산: 후주문 #558368313173 "
             "HL 1 @ 202.63 기준est 203 차이 +0.37(+0.182%) 현est 203 환진입가 1356.9 "
             "S현재가 276500.0 SF이론가 None → RT 1 HL대기 0 | 장부 SF -1 HL 1 체결차 0 "
             "| 누적 HL 1 SF 1 환평균 1356.9 HL평균 202.63 SF평균 277000.0 Sprd -0.741%")
# HL선(§7D) — 선주문 HL, 후주문 LS SF(엔진 로그 형식)
HF_PRE = ("2026-09-28 11:00:00,100 INFO 체결 HL선 정방향 1세트 진입: HL 선주문 #558 10 @ 201.5 "
          "(발주 때 SF호가 275,000) → 누적 10/10, 미헤지 조각 0, LS 대기 1 | 장부 SF 0 HL -10 "
          "체결차 -10")
HF_POST = ("2026-09-28 11:00:01,200 INFO 체결 HL선 정방향 1세트 진입: LS 후주문 #9001 SF 1 "
           "@ 275,000 기준 SF매도1 275,000 환진입가 1358.0 S현재가 274750.0 SF이론가 274,976 "
           "→ RT 1 LS대기 0 | 장부 SF 1 HL -10 체결차 0 | 누적 HL 10 SF 1 환평균 1358.0 "
           "HL평균 201.5 SF평균 275000.0 Sprd 0.111%")


def _sf_formula(fx: float, hl: float, s: float, sf: float, th: float) -> float:
    return (fx * hl - s) / s - (sf - th) / th


def test_round_sprd_formulas_per_product() -> None:
    # 주식선물/HL선: HL 항 − SF 항. 주식: HL 항만, 분모는 주식 체결 평균가(결정 48).
    sf = round_sprd("sf", 270_000.0, 201.24, 1358.2, 274_750.0, 274_976.0)
    assert sf == pytest.approx(_sf_formula(1358.2, 201.24, 274_750, 270_000, 274_976))
    hf = round_sprd("sf_hl_first", 201.5, 275_000.0, 1358.0, 274_750.0, 274_976.0)
    assert hf == pytest.approx(_sf_formula(1358.0, 201.5, 274_750, 275_000, 274_976))
    st = round_sprd("stock", 277_000.0, 202.63, 1356.9, 276_500.0, None)
    assert st == pytest.approx((1356.9 * 202.63 - 277_000) / 277_000)  # S현재가는 안 씀
    assert round_sprd("stock", None, 202.63, 1356.9, None, None) is None
    assert round_sprd("sf", 270_000.0, 201.24, 1358.2, None, 274_976.0) is None


def test_parse_rounds_closes_on_zero_wait_and_weights_partials() -> None:
    rows = parse_rounds([SF_PRE, SF_POST_PARTIAL, SF_POST_REST], underlying="samsung",
                        product="sf", source="a.log")
    assert len(rows) == 1
    r = rows[0]
    assert r["완료시각"] == "2026-09-28 10:15:40" and r["상품"] == "주식선물"
    assert r["방향"] == "정방향" and r["세트"] == 1 and r["다리"] == "진입" and r["RT"] == 8
    assert r["선주문수량"] == 1 and r["선주문평균가"] == 270_000.0
    assert r["후주문수량"] == pytest.approx(10.0)
    assert r["후주문평균가"] == pytest.approx((8.046 * 201.22 + 1.954 * 201.30) / 10.0)
    assert r["환진입가"] == pytest.approx(1358.2) and r["SF이론가"] == pytest.approx(274_976.0)
    assert r["누적Sprd(%)"] == 1.234  # 로그의 누적값(마지막 줄)
    want = round_sprd("sf", 270_000.0, r["후주문평균가"], 1358.2, 274_750.0, 274_976.0)
    assert want is not None and r["판Sprd(%)"] == pytest.approx(round(want * 100, 4))


def test_parse_rounds_skips_halted_partial_round_and_reads_stock_and_hl_first() -> None:
    # 주식 하이닉스: 부분체결 판 + 중지 → 뽑지 않음. 삼성 청산 완결 판은 지수 표기·None 이론가도
    # 처리. HL선은 선주문 HL·후주문 LS를 바꿔 읽는다.
    rows = parse_rounds([ST_PRE1, ST_POST1, ST_PRE2, ST_HALT, ST_C_PRE, ST_C_POST],
                        underlying="samsung", product="stock")
    assert len(rows) == 1
    r = rows[0]
    assert r["다리"] == "청산" and r["선주문평균가"] == 277_000.0 and r["SF이론가"] is None
    assert r["판Sprd(%)"] == pytest.approx(round((1356.9 * 202.63 - 277_000) / 277_000 * 100, 4))
    assert r["누적Sprd(%)"] == -0.741
    hf = parse_rounds([HF_PRE, HF_POST], underlying="samsung", product="sf_hl_first")
    assert len(hf) == 1 and hf[0]["상품"] == "HL선"
    assert hf[0]["선주문평균가"] == 201.5 and hf[0]["후주문평균가"] == 275_000.0
    assert hf[0]["판Sprd(%)"] == pytest.approx(round(
        _sf_formula(1358.0, 201.5, 274_750, 275_000, 274_976) * 100, 4))


def test_collect_reads_three_products_by_filename_and_writes_csv(tmp_path: Path) -> None:
    (tmp_path / "autom_samsung_20260928.log").write_text(
        "\n".join([SF_PRE, SF_POST_PARTIAL, SF_POST_REST]), encoding="utf-8")
    (tmp_path / "autom_samsung_stock_20260928.log").write_text(
        "\n".join([ST_C_PRE, ST_C_POST]), encoding="utf-8")
    (tmp_path / "autom_samsung_sf_hl_first_20260928.log").write_text(
        "\n".join([HF_PRE, HF_POST]), encoding="utf-8")
    (tmp_path / "autom_samsung_20260927.log").write_text(SF_POST_REST, encoding="utf-8")
    rows = collect(tmp_path, "20260928")
    assert [(r["완료시각"], r["상품"]) for r in rows] == [
        ("2026-09-28 10:03:53", "주식"), ("2026-09-28 10:15:40", "주식선물"),
        ("2026-09-28 11:00:01", "HL선")]                      # 파일 무관, 완료시각순
    out = tmp_path / "sprd.csv"
    write_csv(rows, out)
    text = out.read_text(encoding="utf-8-sig").splitlines()
    assert text[0].startswith("완료시각,종목,상품,방향,세트,다리,RT,")
    assert len(text) == 4 and "주식선물" in text[2]
    assert text[1].endswith("autom_samsung_stock_20260928.log")
