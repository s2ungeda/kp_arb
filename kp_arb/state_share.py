"""코어→화면 공유메모리 (DESIGN §12.1) — 메인창이 쓰고 화면들이 읽는 mmap 파일.

소켓은 코어↔메인 하나뿐(사용자 결정). 메인의 수신 스레드가 코어 /ws에서 받은 manual
스냅샷을 이 파일에 기록하고, 일반주문창·주문리스트창은 소켓 없이 이 파일만 읽는다.
델파이의 MMF(메모리 맵 파일)와 같은 방식. 순수 파일 I/O — tk·네트워크 없음.

파일 배치(고정 4MB):
  머리 24바이트 = 버전(u64) · 수신시각 epoch ms(u64) · 본문 길이(u32) · 예비(u32)
  본문 = manual 스냅샷 JSON(utf-8)
찢어진 읽기 방지: 쓰기 전 버전을 홀수로 올림 → 본문·머리 기록 → 버전을 짝수로 마감.
읽는 쪽은 "앞뒤 버전이 같고 짝수"일 때만 채택(아니면 다음 0.1초에 다시).
경로는 메인이 환경변수 KP_SHARE_PATH로 자식 창에 넘긴다(KP_PARENT_PID와 같은 방식).
"""
from __future__ import annotations

import mmap
import os
import struct
import tempfile
from pathlib import Path

SHARE_PATH_ENV = "KP_SHARE_PATH"
SIZE = 4 * 1024 * 1024
_HDR = struct.Struct("<QQII")  # version, ts_ms, length, reserved
HEADER = _HDR.size            # 24
BODY_MAX = SIZE - HEADER


def default_share_path(pid: int) -> str:
    """메인 프로세스별 공유 파일 경로(임시 폴더) — 메인이 여러 개여도 안 겹친다."""
    return str(Path(tempfile.gettempdir()) / f"kp_arb_share_{pid}.bin")


def share_path_from_env() -> str | None:
    """자식 창이 읽을 경로 — 메인이 안 넘겼으면 None(→ HTTP 폴링 폴백)."""
    raw = os.environ.get(SHARE_PATH_ENV, "").strip()
    return raw or None


class ShareWriter:
    """메인창 쪽. 파일을 만들고(4MB) 스냅샷을 버전·시각과 함께 기록한다."""

    def __init__(self, path: str) -> None:
        p = Path(path)
        if not p.exists() or p.stat().st_size < SIZE:
            with open(p, "wb") as f:
                f.truncate(SIZE)
        self._f = open(p, "r+b")  # noqa: SIM115 - mmap 수명과 함께 닫는다
        self._mm = mmap.mmap(self._f.fileno(), SIZE, access=mmap.ACCESS_WRITE)
        self._version = 0
        self._length = 0
        self._mm[0:HEADER] = _HDR.pack(0, 0, 0, 0)  # 빈 상태 표시

    def write(self, body: bytes, ts_ms: int) -> int:
        """본문 교체 기록. 돌려주는 값은 마감 버전(짝수)."""
        if len(body) > BODY_MAX:
            raise ValueError(f"공유메모리 본문 초과: {len(body)} > {BODY_MAX}")
        v = self._version + 1  # 홀수 = 쓰는 중
        self._mm[0:8] = struct.pack("<Q", v)
        self._mm[HEADER:HEADER + len(body)] = body
        self._length = len(body)
        self._version = v + 1
        self._mm[0:HEADER] = _HDR.pack(self._version, ts_ms, self._length, 0)
        return self._version

    def touch(self, ts_ms: int) -> int:
        """본문은 그대로, 수신시각만 갱신(하트비트) — 화면의 지연 판정용."""
        v = self._version + 1
        self._mm[0:8] = struct.pack("<Q", v)
        self._version = v + 1
        self._mm[0:HEADER] = _HDR.pack(self._version, ts_ms, self._length, 0)
        return self._version

    def close(self) -> None:
        self._mm.close()
        self._f.close()


class ShareReader:
    """화면 쪽. 읽기 전용으로 열고, 일관된 스냅샷(버전·시각·본문)만 돌려준다."""

    def __init__(self, path: str) -> None:
        self._f = open(path, "rb")  # noqa: SIM115 - 파일 없으면 FileNotFoundError → 폴백
        self._mm = mmap.mmap(self._f.fileno(), SIZE, access=mmap.ACCESS_READ)

    def read(self) -> tuple[int, int, bytes] | None:
        """(버전, 수신시각 ms, 본문). 쓰는 중이거나 아직 빈 상태면 None."""
        for _ in range(3):  # 쓰기와 겹치면 잠깐 뒤 재시도
            v1, ts, length, _r = _HDR.unpack(self._mm[0:HEADER])
            if v1 == 0 or v1 % 2 == 1 or length > BODY_MAX:
                continue
            body = bytes(self._mm[HEADER:HEADER + length])
            (v2,) = struct.unpack("<Q", self._mm[0:8])
            if v1 == v2:
                return v1, ts, body
        return None

    def close(self) -> None:
        self._mm.close()
        self._f.close()
