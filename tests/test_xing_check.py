"""xing 전수 점검 리포트 — 표 만들기·실시간 수신 판정(순수 로직)."""
from kp_arb.xing_check import CheckResult, realtime_results, render_report


def test_render_report_lists_items_and_counts_failures() -> None:
    rows = [CheckResult("로그인", True, "780ms 계좌 9개"),
            CheckResult("CSPAQ22200 주식 예수금", False, "rejected (09604): 입력 데이터 포맷")]
    text = render_report(rows)
    lines = text.splitlines()
    assert lines[0].startswith("OK   로그인") and "780ms" in lines[0]
    assert lines[1].startswith("FAIL CSPAQ22200") and "09604" in lines[1]
    assert lines[-1] == "— 2항목 중 실패 1건"
    assert render_report([rows[0]]).splitlines()[-1] == "— 1항목 중 실패 0건 (전부 정상)"


def test_realtime_results_never_fail_on_zero_but_say_so() -> None:
    # 0건은 장중이 아니거나 이벤트(주문 통보)가 없어서일 수 있어 FAIL이 아니다 — 문구로만 알린다.
    out = realtime_results(["H1_", "SC1", "CUR"], {"H1_": 42}, 10.0)
    assert [(r.name, r.ok) for r in out] == [("실시간 H1_", True), ("실시간 SC1", True),
                                            ("실시간 CUR", True)]
    assert out[0].detail == "42건/10초"
    assert out[1].detail.startswith("0건/10초 (0건")
