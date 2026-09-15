"""자동M 화면(order_autom) 순수 부분 — 코어 명령 페이로드·누적 합산."""
from kp_arb.order_autom import (
    pct_to_frac,
    rt_manual_errors,
    set_inputs_sig,
    set_payload,
    settings_payload,
    sum_acc,
)


def test_rt_manual_sign_follows_direction() -> None:
    # 사용자 2026-09-15: 역방향 RT 수동 입력은 '-'도 되어야 한다 — 부호는 방향을 따른다(§7A).
    assert rt_manual_errors("fwd", "3") == [] and rt_manual_errors("fwd", "0") == []
    assert "정방향" in rt_manual_errors("fwd", "-1")[0]
    assert rt_manual_errors("rev", "-3") == [] and rt_manual_errors("rev", "0") == []
    assert "역방향" in rt_manual_errors("rev", "2")[0]
    assert "정수" in rt_manual_errors("rev", "-")[0]  # '-'만 치고 확인


def test_set_inputs_sig_changes_only_with_set_inputs() -> None:
    # 실측 2026-09-09: 두 창 중 한 창의 세트설정 저장이 다른 창에 안 보임 → 코어 책의 세트
    # 입력값 서명이 바뀔 때 다시 읽는다. 실행 상태·RT 같은 실시간 값은 서명에 안 들어간다.
    book = {"sets": [{"target_qty": 100, "per_qty": 10, "switch_delay_s": 30,
                      "en_sf": 0.005, "en_s": 0.005, "ex_sf": -0.001, "rt": 3},
                     {"target_qty": 0, "per_qty": 0, "switch_delay_s": 0,
                      "en_sf": None, "en_s": None, "ex_sf": None, "rt": 0},
                     {"target_qty": 0, "per_qty": 0, "switch_delay_s": 0,
                      "en_sf": None, "en_s": None, "ex_sf": None, "rt": 0}]}
    base = set_inputs_sig(book)
    book["sets"][0]["rt"] = 7                     # 실시간 값 변화 → 서명 그대로
    assert set_inputs_sig(book) == base
    book["sets"][0]["en_sf"] = 0.006              # 세트설정 변경 → 서명 달라짐
    assert set_inputs_sig(book) != base
    assert set_inputs_sig({}) == "" and set_inputs_sig({"sets": None}) == ""


def test_set_payload_converts_percent_to_fraction() -> None:
    w = {"target": 100, "per": 10, "delay": 30, "en_sf": 0.5, "en_s": 0.5, "ex_sf": -0.1,
         "rt_manual": None, "clear_diff": True}
    p = set_payload(1, w, "samsung")
    assert p["cmd"] == "autom_set" and p["set"] == 1 and p["underlying"] == "samsung"
    assert (p["target_qty"], p["per_qty"], p["switch_delay_s"]) == (100, 10, 30)
    assert (p["en_sf"], p["en_s"], p["ex_sf"]) == (0.005, 0.005, -0.001)
    assert p["rt_manual"] is None and p["clear_diff"] is True
    assert p["direction"] == "fwd"                                # 기본 정방향
    assert set_payload(0, w, "samsung", "rev")["direction"] == "rev"  # 역방향 세트(2026-09-14)
    assert pct_to_frac(None) is None


def test_settings_payload_shape() -> None:
    common = {"windows": ["08:30:10", "08:46:20", "15:35:30", "15:46:55"],
              "pre_tick": {"sk_hynix": 3000, "samsung": 500, "hyundai": 1000},
              "pre_delay": 1000, "resume_delay": 10, "pre_range": 0.4,
              "rel_buy": 2, "rel_sell": 1,
              "risk": {"fwd_en": 0.0, "fwd_ex": 0.5, "fwd_gap": 0.1}}
    p = settings_payload(common)
    assert p["windows"] == [["08:30:10", "08:46:20"], ["15:35:30", "15:46:55"]]
    assert p["pre_range"] == 0.004 and p["risk_fwd_ex"] == 0.005 and p["rel_buy"] == 2
    assert p["pre_delay_ms"] == 1000 and p["resume_delay_s"] == 10
    # 역방향 리스크방지(2026-09-14) — 화면 상태에 키가 없으면 기본값(0.5/0/0.1%)
    assert (p["risk_rev_en"], p["risk_rev_ex"], p["risk_rev_gap"]) == (0.005, 0.0, 0.001)
    common["risk"].update({"rev_en": 0.4, "rev_ex": 0.05, "rev_gap": 0.2})
    p2 = settings_payload(common)
    assert (p2["risk_rev_en"], p2["risk_rev_ex"], p2["risk_rev_gap"]) == (0.004, 0.0005, 0.002)


def test_set_inputs_sig_includes_reverse_sets() -> None:
    book = {"sets": [{"target_qty": 1, "per_qty": 1, "switch_delay_s": 0,
                      "en_sf": 0.005, "en_s": 0.005, "ex_sf": -0.001}],
            "rev_sets": [{"target_qty": 0, "per_qty": 0, "switch_delay_s": 0,
                          "en_sf": None, "en_s": None, "ex_sf": None}]}
    base = set_inputs_sig(book)
    book["rev_sets"][0]["target_qty"] = 5           # 역방향 세트설정 변경 → 서명 달라짐
    assert set_inputs_sig(book) != base


def test_fx_caption() -> None:
    from kp_arb.order_autom import fx_caption

    assert fx_caption(1349.6, "현물") == "환율 1,349.60 (현물)"
    assert fx_caption(1352.25, "선물이론") == "환율 1,352.25 (선물이론)"
    assert fx_caption(None, "현물") == "환율 -" and fx_caption(0, "") == "환율 -"
    assert fx_caption(1349.6, "") == "환율 1,349.60"


def test_sum_acc_weights_by_hl_qty() -> None:
    rows = [
        {"entry": {"hl_qty": 40, "sf_qty": 4, "fx_avg": 1350.0, "sprd": 0.01,
                   "hl_avg": 190.0, "sf_avg": 250_000.0}},
        {"entry": {"hl_qty": 60, "sf_qty": 6, "fx_avg": 1360.0, "sprd": 0.02,
                   "hl_avg": 200.0, "sf_avg": 260_000.0}},
        {"entry": {"hl_qty": 0, "sf_qty": 0, "fx_avg": None, "sprd": None}},
    ]
    agg = sum_acc(rows, "entry")
    assert agg["hl_qty"] == 100 and agg["sf_qty"] == 10
    assert agg["fx_avg"] == 1356.0 and abs(agg["sprd"] - 0.016) < 1e-12
    # -HP/+SF 칸은 평균 체결가(수량 아님, 사용자 2026-09-11) — HL 수량 가중
    assert agg["hl_avg"] == 196.0 and agg["sf_avg"] == 256_000.0
    assert sum_acc([], "exit")["fx_avg"] is None and sum_acc([], "exit")["hl_avg"] is None
    # 옛 스냅샷(평균가 키 없음)이면 평균가만 None, 나머지는 그대로
    old = sum_acc([{"entry": {"hl_qty": 40, "sf_qty": 4, "fx_avg": 1350.0}}], "entry")
    assert old["fx_avg"] == 1350.0 and old["hl_avg"] is None and old["sf_avg"] is None


def test_sum_acc_uses_matched_smaller_side() -> None:
    # 사용자 확정 2026-09-08: LS·HL 누적 체결량이 다르면 적은 쪽 기준. SF 1계약 체결 + HL 0.588
    # 체결 → HL 0.588 / SF 0.0588 로 표시(코어가 matched_* 로 줌, 없으면 화면이 min으로 계산).
    rows = [{"entry": {"hl_qty": 0.588, "sf_qty": 1, "matched_hl": 0.588, "matched_sf": 0.0588,
                       "fx_avg": 1340.0, "sprd": 0.001}}]
    agg = sum_acc(rows, "entry")
    assert abs(agg["hl_qty"] - 0.588) < 1e-9 and abs(agg["sf_qty"] - 0.0588) < 1e-9
    old = sum_acc([{"entry": {"hl_qty": 25, "sf_qty": 4}}], "entry")  # matched 키 없는 옛 스냅샷
    assert old["hl_qty"] == 25 and old["sf_qty"] == 2.5
