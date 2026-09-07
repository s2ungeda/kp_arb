"""상태 파일 저장 공통(state_backup) — 바뀔 때만·원자적·세대 백업 (DESIGN-ui §7)."""
from __future__ import annotations

from pathlib import Path

from kp_arb.state_backup import (
    backup_path,
    list_generations,
    restore_generation,
    write_if_changed,
)


def test_same_content_is_not_rewritten(tmp_path: Path) -> None:
    p = tmp_path / "ui_state.json"
    assert write_if_changed(p, '{"a": 1}')
    assert not write_if_changed(p, '{"a": 1}')       # 같은 내용 — 안 씀
    assert not backup_path(p, 1).exists()             # 세대도 안 생김
    assert not (tmp_path / "ui_state.json.tmp").exists()  # 임시 파일 안 남음


def test_generations_are_rotated_and_capped(tmp_path: Path) -> None:
    p = tmp_path / "core_state.json"
    for i in range(7):
        assert write_if_changed(p, f"v{i}", generations=5)
    assert p.read_text(encoding="utf-8") == "v6"
    gens = list_generations(p, generations=5)
    assert [n for n, _t, _x in gens] == [1, 2, 3, 4, 5]        # 5개로 고정
    assert [x for _n, _t, x in gens] == ["v5", "v4", "v3", "v2", "v1"]  # 1이 가장 최근
    assert not backup_path(p, 6).exists()


def test_restore_generation_keeps_current_as_first_backup(tmp_path: Path) -> None:
    p = tmp_path / "ui_state.json"
    write_if_changed(p, "old")
    write_if_changed(p, "new")
    assert restore_generation(p, 1) == "old"
    assert p.read_text(encoding="utf-8") == "old"
    gens = list_generations(p)
    assert gens[0][2] == "new"      # 되돌리기 직전 내용이 1세대 — 되돌리기도 되돌릴 수 있음
    assert restore_generation(p, 9) is None  # 없는 세대


def test_zero_generations_means_no_backup(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    write_if_changed(p, "1", generations=0)
    write_if_changed(p, "2", generations=0)
    assert p.read_text(encoding="utf-8") == "2"
    assert not (tmp_path / "backup").exists()
