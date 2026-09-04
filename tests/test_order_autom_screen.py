"""자동M 화면(order_autom) 순수 부분 — 코어 명령 페이로드·누적 합산."""
from kp_arb.order_autom import pct_to_frac, set_payload, settings_payload, sum_acc


def test_set_payload_converts_percent_to_fraction() -> None:
    w = {"target": 100, "per": 10, "delay": 30, "en_sf": 0.5, "en_s": 0.5, "ex_sf": -0.1,
         "rt_manual": None, "clear_diff": True}
    p = set_payload(1, w)
    assert p["cmd"] == "autom_set" and p["set"] == 1
    assert (p["target_qty"], p["per_qty"], p["switch_delay_s"]) == (100, 10, 30)
    assert (p["en_sf"], p["en_s"], p["ex_sf"]) == (0.005, 0.005, -0.001)
    assert p["rt_manual"] is None and p["clear_diff"] is True
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


def test_sum_acc_weights_by_hl_qty() -> None:
    rows = [
        {"entry": {"hl_qty": 40, "sf_qty": 4, "fx_avg": 1350.0, "sprd": 0.01}},
        {"entry": {"hl_qty": 60, "sf_qty": 6, "fx_avg": 1360.0, "sprd": 0.02}},
        {"entry": {"hl_qty": 0, "sf_qty": 0, "fx_avg": None, "sprd": None}},
    ]
    agg = sum_acc(rows, "entry")
    assert agg["hl_qty"] == 100 and agg["sf_qty"] == 10
    assert agg["fx_avg"] == 1356.0 and abs(agg["sprd"] - 0.016) < 1e-12
    assert sum_acc([], "exit")["fx_avg"] is None
