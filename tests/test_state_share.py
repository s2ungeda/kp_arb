"""공유메모리(state_share) — 메인이 쓰고 화면이 읽는 mmap 파일의 일관성 규칙."""
import struct
from pathlib import Path

import pytest

from kp_arb.state_share import BODY_MAX, HEADER, ShareReader, ShareWriter


def test_round_trip_and_version_even(tmp_path: Path) -> None:
    path = str(tmp_path / "share.bin")
    w = ShareWriter(path)
    r = ShareReader(path)
    try:
        assert r.read() is None  # 아직 빈 상태(버전 0)
        v = w.write(b'{"a":1}', 1_700_000_000_000)
        assert v == 2  # 첫 기록 마감 버전은 짝수
        got = r.read()
        assert got == (2, 1_700_000_000_000, b'{"a":1}')
    finally:
        r.close()
        w.close()


def test_touch_updates_time_keeps_body(tmp_path: Path) -> None:
    # 하트비트 — 본문은 그대로, 수신시각·버전만 올라간다.
    path = str(tmp_path / "share.bin")
    w = ShareWriter(path)
    r = ShareReader(path)
    try:
        w.write(b'{"a":1}', 100)
        w.touch(200)
        assert r.read() == (4, 200, b'{"a":1}')
    finally:
        r.close()
        w.close()


def test_reader_rejects_in_progress_write(tmp_path: Path) -> None:
    # 쓰는 중(버전 홀수)에 읽으면 채택하지 않는다 — 찢어진 데이터 방지.
    path = str(tmp_path / "share.bin")
    w = ShareWriter(path)
    r = ShareReader(path)
    try:
        w.write(b"ok", 1)
        w._mm[0:8] = struct.pack("<Q", 3)  # 쓰기 시작 상태를 흉내
        assert r.read() is None
        w._mm[0:8] = struct.pack("<Q", 2)  # 마감 복구
        assert r.read() == (2, 1, b"ok")
    finally:
        r.close()
        w.close()


def test_body_too_large_rejected(tmp_path: Path) -> None:
    w = ShareWriter(str(tmp_path / "share.bin"))
    try:
        with pytest.raises(ValueError):
            w.write(b"x" * (BODY_MAX + 1), 1)
    finally:
        w.close()


def test_reader_missing_file_raises(tmp_path: Path) -> None:
    # 메인이 안 떠서 파일이 없으면 예외 → 화면은 HTTP 폴링으로 폴백.
    with pytest.raises(FileNotFoundError):
        ShareReader(str(tmp_path / "none.bin"))


def test_header_size() -> None:
    assert HEADER == 24


def test_share_path_for_channels() -> None:
    # manual은 기본 경로 그대로(구버전 호환), state는 `_state` 접미 — 채널별 파일(§12.1).
    from kp_arb.state_share import share_path_for

    base = str(Path("/tmp") / "kp_arb_share_123.bin")
    assert share_path_for(base, "manual") == base
    assert Path(share_path_for(base, "state")).name == "kp_arb_share_123_state.bin"


def test_stale_share_files_by_owner_pid() -> None:
    # 주인 메인(pid)이 죽은 공유 파일만 청소 대상 — 다른 이름·살아있는 pid는 남긴다.
    from kp_arb.state_share import stale_share_files

    names = ["kp_arb_share_111.bin", "kp_arb_share_111_state.bin", "kp_arb_share_222.bin",
             "other.bin", "kp_arb_share_x.bin"]
    got = stale_share_files(names, pid_alive=lambda pid: pid == 222)
    assert got == ["kp_arb_share_111.bin", "kp_arb_share_111_state.bin"]


def test_cleanup_stale_shares_removes_only_dead(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import kp_arb.state_share as ss

    for name in ("kp_arb_share_111.bin", "kp_arb_share_222_state.bin", "keep.txt"):
        (tmp_path / name).write_bytes(b"x")
    monkeypatch.setattr(ss, "pid_alive", lambda pid: pid == 222)
    assert ss.cleanup_stale_shares(str(tmp_path)) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt", "kp_arb_share_222_state.bin"]
