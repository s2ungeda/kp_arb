"""자동M 지연·슬리피지 리포트(kp_arb.report_latency) — 작은 로그 조각으로 표·파일 생성 확인."""
# ruff: noqa: E501 — 실제 로그 줄을 그대로 쓴다(긴 줄)
from __future__ import annotations

from pathlib import Path

from kp_arb.report_latency import main, rounds

AUTOM = """\
2026-09-15 10:00:00,000 INFO 판정 정방향 1세트 진입: 통과 → 선주문 1계약 역산가 1,700,000 = 이론가 1,702,000×(1+0.300%−0.400%) 주문단위 1000 범위 1,701,000~1,690,000(호가단위 1000) 매수1 1,701,000 매도1 1,702,000 환율 1,345.00 HL 매수1 1260.7 매도1 1260.9 est 1261
2026-09-15 10:00:01,000 INFO 체결 정방향 1세트 진입: 선주문 #10 1 @ 1.7e+06 기준est 1261 현est 1260.8 → 누적 1/1, HL 대기 10 | 장부 SF 1 HL 0 체결차 10
2026-09-15 10:00:01,850 INFO 체결 정방향 1세트 진입: 후주문 #900 HL 4 @ 1260.9 기준est 1261 차이 -0.1(-0.008%) 현est 1260.6 환진입가 1344.8 S현재가 1699000.0 SF이론가 1,701,000 → RT 1 HL대기 6 | 장부 SF 1 HL -4 체결차 6
2026-09-15 10:00:01,900 INFO 체결 정방향 1세트 진입: 후주문 #900 HL 6 @ 1260.5 기준est 1261 차이 -0.5(-0.040%) 현est 1260.5 환진입가 1344.8 S현재가 1699000.0 SF이론가 1,701,000 → RT 1 HL대기 0 | 장부 SF 1 HL -10 체결차 0
"""
HL_ORDER = """\
2026-09-15 10:00:01,010 INFO 발주요청 [자동M] sk_hynix hl_perp sell 10 @ 1235.7
2026-09-15 10:00:01,010 INFO HL 요청패킷 {"type": "order", "orders": [{"a": 1, "b": false, "p": "1235.7", "s": "10", "c": "0x0abc"}]}
2026-09-15 10:00:01,800 INFO 발주 [자동M] sk_hynix hl_perp sell 10 @ 1235.7 → #900 | resp={}
2026-09-15 10:00:01,800 INFO HL 발주 왕복 790 ms #900 cloid=0x0abc
"""


def _write(tmp_path: Path) -> None:
    (tmp_path / "autom_sk_hynix_20260915.log").write_text(AUTOM, encoding="utf-8")
    (tmp_path / "hl_order_20260915.log").write_text(HL_ORDER, encoding="utf-8")


def test_rounds_pairs_pre_fill_send_response_and_fills(tmp_path: Path) -> None:
    _write(tmp_path)
    (row,) = rounds(tmp_path, "20260915")
    assert row["pre"] == "10" and row["oid"] == "900"
    assert (row["t_send"] - row["t_pre"]).total_seconds() * 1000 == 10  # ①→② 10ms
    assert (row["t_resp"] - row["t_send"]).total_seconds() * 1000 == 790  # 왕복
    assert row["t_first"] < row["t_fill"]  # 조각 2개 — 첫 조각·마지막 조각
    assert row["est"] == 1261 and abs(row["avg_px"] - 1260.66) < 1e-9  # 수량가중 평균
    assert abs(row["diff"] - (1260.66 - 1261)) < 1e-9  # 매도: 체결−est(불리 −)
    assert row["now_pre"] == 1260.8 and row["now_post"] == 1260.5  # 체결 순간 현est


def test_main_prints_table_and_writes_report_file(tmp_path: Path) -> None:
    _write(tmp_path)
    assert main(["report", "20260915", str(tmp_path)]) == 0
    out = (tmp_path / "report_20260915.md").read_text(encoding="utf-8")
    assert "| 1 | sk_hynix | 정방향1 | 진입 | #10 |" in out and "판 수 1" in out
    assert "기준est 대비 체결(유리+)" in out
    assert main(["report", "20260916", str(tmp_path)]) == 1  # 그 날짜 로그 없음
