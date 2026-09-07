"""상태 파일 저장 공통 — 바뀔 때만 · 원자적 교체 · 세대 백업(개수 고정) (DESIGN-ui §7).

대상: ui_state.json(열린 화면), core_state.json(세트·설정), .win_state/*.json(창 위치·입력값).
- **바뀔 때만 씀**: 2초 주기 저장이 같은 내용을 되풀이해 쓰지 않게(세대가 의미 있는 변화만 담음).
- **원자적**: 임시 파일에 쓰고 이름 바꾸기로 교체 — 쓰는 도중 죽어도 반쯤 깨진 파일이 안 남는다.
- **세대 백업**: 새로 쓸 때 기존 파일을 backup/<이름>.1.json으로 밀고 1→2→… 마지막은 버린다.
  파일 하나당 세대 수가 고정이라 날짜별 백업처럼 쌓이지 않는다(사용자 확정 2026-09-07).
"""
from __future__ import annotations

import os
from pathlib import Path

BACKUP_DIR_NAME = "backup"


def backup_path(path: Path, n: int) -> Path:
    """n세대 백업 경로 — <폴더>/backup/<이름>.<n><확장자>. 1이 가장 최근."""
    return path.parent / BACKUP_DIR_NAME / f"{path.stem}.{n}{path.suffix}"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _rotate(path: Path, generations: int) -> None:
    """현재 파일을 1세대로 밀어 넣는다(1→2, …, 마지막 세대는 버림). 현재 파일이 없으면 무시."""
    if generations <= 0 or not path.exists():
        return
    backup_path(path, 1).parent.mkdir(parents=True, exist_ok=True)
    oldest = backup_path(path, generations)
    if oldest.exists():
        oldest.unlink()
    for n in range(generations - 1, 0, -1):
        src = backup_path(path, n)
        if src.exists():
            os.replace(src, backup_path(path, n + 1))
    os.replace(path, backup_path(path, 1))


def write_if_changed(path: Path, text: str, *, generations: int = 5) -> bool:
    """내용이 지금 파일과 같으면 안 쓰고 False. 다르면 세대를 밀고 원자적으로 쓴 뒤 True.

    OSError(잠김·권한)는 삼킨다(False) — 저장 실패가 화면·코어를 멈추면 안 된다(다음 주기 재시도).
    """
    try:
        if _read_text(path) == text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        _rotate(path, generations)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def list_generations(path: Path, *, generations: int = 5) -> list[tuple[int, float, str]]:
    """존재하는 세대 목록 [(n, 저장시각 epoch, 내용)] — n 오름차순(1이 최근)."""
    out: list[tuple[int, float, str]] = []
    for n in range(1, generations + 1):
        p = backup_path(path, n)
        text = _read_text(p)
        if text is None:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        out.append((n, mtime, text))
    return out


def restore_generation(path: Path, n: int, *, generations: int = 5) -> str | None:
    """n세대를 현재 파일로 되돌린다. 지금 파일은 1세대로 밀려 되돌리기도 되돌릴 수 있다.

    반환: 되돌린 내용(없거나 실패면 None).
    """
    text = _read_text(backup_path(path, n))
    if text is None:
        return None
    write_if_changed(path, text, generations=generations)
    return text
