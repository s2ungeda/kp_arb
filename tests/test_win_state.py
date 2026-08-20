"""화면 위치·설정 저장·복원 — 순수 로직 + 키별 파일 왕복(프로세스 경합 없음)."""
from pathlib import Path

import pytest

from kp_arb import win_state


def test_position_only_extracts_position() -> None:
    assert win_state.position_only("800x600+10+20") == "+10+20"
    assert win_state.position_only("560x460-5+30") == "-5+30"   # 음수 좌표(멀티모니터)
    assert win_state.position_only("+100+200") == "+100+200"     # 크기 없는 형태
    assert win_state.position_only("800x600") is None            # 크기만 → None
    assert win_state.position_only("1x1") is None


def test_storage_key_per_instance_slot() -> None:
    # 슬롯 없으면 창 이름 그대로(단일), 슬롯 있으면 인스턴스별 키 → 서로 안 덮어씀.
    assert win_state.storage_key("order_hl", None) == "order_hl"
    assert win_state.storage_key("order_hl", "") == "order_hl"   # 빈 문자열도 단일 취급
    assert win_state.storage_key("order_hl", "0") == "order_hl#0"
    assert win_state.storage_key("order_hl", "1") == "order_hl#1"
    assert win_state.storage_key("order_hl", "0") != win_state.storage_key("order_hl", "1")


@pytest.fixture
def _statedir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(win_state, "_STATE_DIR", tmp_path / ".win_state")
    monkeypatch.delenv("KP_WIN_SLOT", raising=False)  # 슬롯 없는(단일) 키로 테스트
    return tmp_path


def test_position_roundtrip_and_key_isolation(_statedir: Path) -> None:
    win_state.save("autoT", "300x200+11+22")
    win_state.save("monitor", "760x600+33+44")   # 다른 키 — 파일이 달라 서로 안 덮어씀
    assert win_state.saved_position("autoT") == "+11+22"
    assert win_state.saved_position("monitor") == "+33+44"
    assert win_state.saved_position("none") is None
    win_state.save("autoT", "300x200")           # 크기만 → 위치 못 뽑음 → 저장 안 함
    assert win_state.saved_position("autoT") == "+11+22"  # 이전 값 보존


def test_fields_and_position_coexist_in_one_key(_statedir: Path) -> None:
    # 같은 키 파일에 위치·필드가 함께 — 서로를 지우지 않는다(한 프로세스가 순차 기록).
    win_state.save("order_hl", "300x200+5+6")
    win_state.save_fields("order_hl", {"under": "삼성", "tick": 3})
    assert win_state.saved_fields("order_hl") == {"under": "삼성", "tick": 3}
    assert win_state.saved_position("order_hl") == "+5+6"   # 필드 저장이 위치 안 지움
    win_state.save("order_hl", "300x200+9+9")  # 위치 저장이 필드 안 지움
    assert win_state.saved_fields("order_hl") == {"under": "삼성", "tick": 3}
    assert win_state.saved_position("order_hl") == "+9+9"


def test_slot_separates_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win_state, "_STATE_DIR", tmp_path / ".win_state")
    monkeypatch.setenv("KP_WIN_SLOT", "0")
    win_state.save("order_hl", "300x200+1+1")
    win_state.save_fields("order_hl", {"under": "삼성"})
    monkeypatch.setenv("KP_WIN_SLOT", "1")            # 다른 인스턴스
    win_state.save("order_hl", "300x200+2+2")
    assert win_state.saved_position("order_hl") == "+2+2"  # 슬롯1
    assert win_state.saved_fields("order_hl") == {}         # 슬롯1은 아직 필드 없음
    monkeypatch.setenv("KP_WIN_SLOT", "0")
    assert win_state.saved_position("order_hl") == "+1+1"  # 슬롯0 그대로
    assert win_state.saved_fields("order_hl") == {"under": "삼성"}


def test_missing_or_corrupt_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win_state, "_STATE_DIR", tmp_path / ".win_state")
    monkeypatch.delenv("KP_WIN_SLOT", raising=False)
    assert win_state.saved_position("autoT") is None
    assert win_state.saved_fields("autoT") == {}
    (tmp_path / ".win_state").mkdir()
    (tmp_path / ".win_state" / "autoT.json").write_text("{broken", encoding="utf-8")
    assert win_state.saved_position("autoT") is None   # 깨진 파일 → 예외 아닌 빈 결과
    assert win_state.saved_fields("autoT") == {}
