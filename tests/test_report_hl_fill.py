"""HL 후주문 체결 즉시성 리포트(kp_arb.report_hl_fill) — 작은 로그 조각으로 분류·표 확인."""
from __future__ import annotations

from pathlib import Path

from kp_arb.report_hl_fill import OUTCOMES, expand_days, load_day, main, parse_day, render

_HL = [
    # ① 즉시 전량(단건 발주 → 왕복 줄)
    "2026-11-06 09:34:04,619 INFO 발주요청 [자동M] sk_hynix hl_perp buy 40 @ 859.47",
    "2026-11-06 09:34:05,556 INFO 발주 [자동M] sk_hynix hl_perp buy 40 @ 859.47 → #596423798622 | "
    "resp={'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': [{'filled': "
    "{'totalSz': '40.0', 'avgPx': '846.965', 'oid': 596423798622, 'cloid': '0x01'}}]}}}",
    "2026-11-06 09:34:05,556 INFO HL 발주 왕복 937 ms #596423798622 cloid=0x01",
    "2026-11-06 09:34:05,556 INFO 체결(전량) [자동M] sk_hynix hl_perp buy 40 40 @ 846.965 "
    "#596423798622 [체결#place-596423798622] 누적 40/40",
    # ② 묶음 발주(요청 둘 먼저, 응답 둘 뒤): 하나는 IOC 부분 체결(잔량은 core 로그), 하나는 걸림
    #    → 뒤에 전량
    "2026-11-06 09:40:00,000 INFO 발주요청 [자동M] samsung hl_perp sell 10 @ 147.16 ioc p=1000",
    "2026-11-06 09:40:00,001 INFO 발주요청 [자동M] samsung hl_perp sell 10 @ 147.2",
    "2026-11-06 09:40:00,080 INFO HL 묶음 발주 왕복 80 ms — 2건 중 접수 2건",
    "2026-11-06 09:40:00,081 INFO 발주 [자동M] samsung hl_perp sell 10 @ 147.16 ioc p=1000 → #11 | "
    "resp={'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': [{'filled': "
    "{'totalSz': '4.0', 'avgPx': '147.17', 'oid': 11, 'cloid': '0x02'}}]}}}",
    "2026-11-06 09:40:00,082 INFO 발주 [자동M] samsung hl_perp sell 10 @ 147.2 → #12 | "
    "resp={'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': [{'resting': "
    "{'oid': 12, 'cloid': '0x03'}}]}}}",
    "2026-11-06 09:40:03,500 INFO 체결(부분) [자동M] samsung hl_perp sell 10 6 @ 147.2 #12 "
    "[체결#a] 누적 6/10",
    "2026-11-06 09:40:04,000 INFO 체결(전량) [자동M] samsung hl_perp sell 10 4 @ 147.2 #12 "
    "[체결#b] 누적 10/10",
    # ③ 거부 / ④ 응답 없음 / 다른 출처는 안 센다
    "2026-11-06 14:46:02,000 INFO 발주요청 [자동M] sk_hynix hl_perp sell 20 @ 1029.6",
    "2026-11-06 14:46:02,072 WARNING 발주거부 [자동M] sk_hynix hl_perp sell 20 @ 1029.6 — HL "
    "rejected: Insufficient delegatable balance for priority order",
    "2026-11-06 15:00:00,000 INFO 발주요청 [자동M] sk_hynix hl_perp buy 1 @ 900",
    "2026-11-06 15:01:00,000 INFO 발주요청 [일반주문창] sk_hynix hl_perp buy 1 @ 900",
]
_CORE = ["2026-11-06 09:40:00,083 INFO kp_arb.bootstrap IOC 남은 수량 6 장부 닫음 #11(체결 4) — "
         "거래소가 즉시 취소"]


def test_parse_day_classifies_each_order() -> None:
    d = parse_day(_HL, _CORE, "20261106")
    assert [o.outcome for o in d.orders] == ["즉시전량", "즉시부분", "걸림", "거부", "응답없음"]
    assert all(o.outcome in OUTCOMES for o in d.orders)
    full, part, rest, rej, none = d.orders
    assert (full.oid, full.filled_now, full.avg_px, full.rtt_ms) == (
        "596423798622", 40.0, 846.965, 937)
    assert (part.ioc, part.p, part.filled_now, part.ioc_left, part.rtt_ms) == (
        True, 1000, 4.0, 6.0, 80)
    assert rest.oid == "12" and rest.done_how == "체결" and rest.t_done is not None
    assert (rest.t_done - rest.t_req).total_seconds() == 4.0 - 0.001
    assert rej.error is not None and "Insufficient delegatable" in rej.error
    assert none.oid is None
    assert d.counts == {"즉시전량": 1, "즉시부분": 1, "걸림": 1, "거부": 1, "응답없음": 1}


def test_render_table_and_odd_list() -> None:
    text = render([parse_day(_HL, _CORE, "20261106")])
    assert "20261106      5      20%      20%   20%   20%      20%      1         1" in text
    assert "## 즉시 전량이 아니었던 건 4건" in text
    assert "samsung sell 10 @ 147.16 ioc p=1000 #11: 즉시부분 즉시 4/10 IOC잔량 6" in text
    assert "samsung sell 10 @ 147.2 #12: 걸림 → 체결 09:40:04.000 (+4.0s)" in text
    assert "#-: 거부 — HL rejected: Insufficient delegatable" in text
    assert "buy 1 @ 900 #-: 응답없음" in text
    assert "(없음" not in text
    assert "(없음 — 수수료 없이도" in render([parse_day(_HL[:4], [], "20261106")])


def test_load_day_and_expand_days_with_core_folders(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    for day in ("20261103", "20261106"):
        folder = tmp_path / f"core_{day}"
        folder.mkdir()
        (folder / f"hl_order_{day}.log").write_text("\n".join(_HL), encoding="utf-8")
        (folder / f"core_{day}.log").write_text("\n".join(_CORE), encoding="utf-8")
    assert expand_days("all", tmp_path) == ["20261103", "20261106"]
    assert expand_days("20261104..20261130", tmp_path) == ["20261106"]
    assert expand_days("20261106", tmp_path) == ["20261106"]
    d = load_day(tmp_path, "20261103", "자동M")
    assert d is not None and len(d.orders) == 5
    assert load_day(tmp_path / "core_20261106", "20261106", "자동M") is not None  # 그날 폴더 직접
    assert load_day(tmp_path, "20261105", "자동M") is None
    assert main(["x", "all", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "20261103      5" in out and "20261106      5" in out
    assert (tmp_path / "report_hl_fill_20261103-20261106.md").exists()
    assert main(["x", "20261105", str(tmp_path)]) == 1
