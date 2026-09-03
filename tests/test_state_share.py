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
