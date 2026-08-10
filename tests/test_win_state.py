"""화면 위치 저장·복원 — 순수 로직 + 파일 왕복 테스트."""
from pathlib import Path

import pytest

from kp_arb import win_state


def test_position_only_extracts_position() -> None:
    assert win_state.position_only("800x600+10+20") == "+10+20"
    assert win_state.position_only("560x460-5+30") == "-5+30"   # 음수 좌표(멀티모니터)
    assert win_state.position_only("+100+200") == "+100+200"     # 크기 없는 형태
    assert win_state.position_only("800x600") is None            # 크기만 → None
    assert win_state.position_only("1x1") is None


def test_merge_geometry_keeps_others_and_position_only() -> None:
    store = {"autoT": "+1+1", "monitor": "+2+2"}
    merged = win_state.merge_geometry(store, "autoM", "300x200+50+60")
    assert merged["autoM"] == "+50+60"
    assert merged["autoT"] == "+1+1" and merged["monitor"] == "+2+2"  # 다른 항목 보존
    # 위치를 못 뽑으면 원본 유지(항목 추가 안 함)
    assert win_state.merge_geometry(store, "x", "800x600") == store


def test_save_and_load_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win_state, "STATE_PATH", tmp_path / "win_state.json")
    win_state.save("autoT", "300x200+11+22")
    win_state.save("monitor", "760x600+33+44")   # 다른 화면 저장해도 autoT 보존
    assert win_state.saved_position("autoT") == "+11+22"
    assert win_state.saved_position("monitor") == "+33+44"
    assert win_state.saved_position("none") is None


def test_fields_save_and_load_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win_state, "FIELDS_PATH", tmp_path / "win_fields.json")
    win_state.save_fields("order_hl", {"under": "하이닉스", "reduce": True, "tick": 3})
    win_state.save_fields("order_ls", {"under": "삼성"})  # 다른 화면 보존
    assert win_state.saved_fields("order_hl") == {"under": "하이닉스", "reduce": True, "tick": 3}
    assert win_state.saved_fields("order_ls") == {"under": "삼성"}
    assert win_state.saved_fields("none") == {}


def test_fields_missing_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win_state, "FIELDS_PATH", tmp_path / "none.json")
    assert win_state.saved_fields("order_hl") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(win_state, "FIELDS_PATH", bad)
    assert win_state.saved_fields("order_hl") == {}


def test_load_missing_or_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win_state, "STATE_PATH", tmp_path / "none.json")
    assert win_state.saved_position("autoT") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(win_state, "STATE_PATH", bad)
    assert win_state.saved_position("autoT") is None
