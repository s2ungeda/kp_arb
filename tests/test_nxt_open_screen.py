"""NXT 개장 매매 화면 순수 부분 — 퍼센트↔소수, 설정 명령 본문, 결과·상태 글."""
from __future__ import annotations

from kp_arb.domain.enums import Underlying
from kp_arb.nxt_open_screen import (
    active_label,
    frac_of,
    merge_choices,
    monitor_text,
    pct_text,
    results_summary,
    settings_payload,
    status_text,
    tiers_payload,
)


def test_percent_text_roundtrip() -> None:
    assert pct_text(0.012) == "1.200" and pct_text(-0.004) == "-0.400" and pct_text(None) == ""
    assert frac_of("1.2") == 0.012 and frac_of(" -0.4 ") == -0.004
    assert frac_of("") is None and frac_of("x") is None
    # 모니터 수치는 체결쏴처럼 소수 2자리, 부호 없이
    assert monitor_text(0.0026) == "0.26" and monitor_text(-0.0005) == "-0.05"
    assert monitor_text(None) == "-"


def test_settings_payload_from_inputs() -> None:
    body = settings_payload(
        Underlying.SK_HYNIX, "08:00:00", "0.5", "5",
        entries=[("1.2", "100", "0.9"), ("1.0", "100", "0.7"), ("", "", "")],
        exits=[("-0.4", "100", "-0.2"), ("", "", ""), ("", "", "")])
    assert body["cmd"] == "nxt_open_settings" and body["underlying"] == "sk_hynix"
    assert body["open_hms"] == "08:00:00" and (body["start_s"], body["end_s"]) == (0.5, 5.0)
    assert body["entries"][0] == {"spread": 0.012, "qty": 100, "price_spread": 0.009}
    assert body["entries"][2] == {"spread": None, "qty": 0, "price_spread": None}
    assert body["exits"][0]["spread"] == -0.004
    body2 = settings_payload(Underlying.SAMSUNG, "", "", "", [], [])  # 빈 칸은 기본값
    assert body2["open_hms"] == "08:00:00" and (body2["start_s"], body2["end_s"]) == (0.5, 5.0)
    assert body2["entries"] == []
    assert tiers_payload([("1,2", "1,000", "0.9")])[0]["qty"] == 1000


def test_result_and_status_text() -> None:
    res = [{"block": "entry", "index": 0, "side": "sell", "qty": 100, "price": 1303.5,
            "order_id": "123", "error": None},
           {"block": "entry", "index": 1, "side": "sell", "qty": 100, "price": 0.0,
            "order_id": None, "error": "주문가 계산 불가(S·환율 없음)"}]
    assert results_summary(res) == "진입1 #123 100@1303.5, 진입2 실패 주문가 계산 불가(S·환율 없음)"
    assert results_summary([]) == ""
    assert status_text({"window": "none"}) == "대기"
    assert status_text({"window": "open", "t0": "08:00:00.512"}) == "창 열림 08:00:00.512"
    assert status_text({"window": "closed", "t0": "08:00:00.5", "fired": True,
                        "fired_at": "08:00:01.2", "note": "", "results": res[:1]}) == (
        "발주 08:00:01.2 진입1 #123 100@1303.5")
    note = "오늘 판정 없음(개장 밖)"
    assert status_text({"window": "none", "note": note}) == note


def test_merge_combo_helpers_follow_core_ticks() -> None:
    # 코어 hl_merge_ticks(일반주문창·체결쏴와 같은 모양) → 콤보 글과 (n_sig_figs, mantissa) 표
    ticks = [{"tick": "0.1", "n_sig_figs": None, "mantissa": None},
             {"tick": "0.2", "n_sig_figs": 5, "mantissa": 2},
             {"tick": "0.5", "n_sig_figs": 5, "mantissa": 5}]
    labels, table = merge_choices(ticks)
    assert labels == ["0.1", "0.2", "0.5"] and table["0.5"] == (5, 5)
    assert active_label(table, {"n_sig_figs": 5, "mantissa": 2}) == "0.2"
    assert active_label(table, None) is None and active_label(table, {"n_sig_figs": 9}) is None
