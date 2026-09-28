"""xingAPI 설치 폴더 → DLL 탐색 경로 등록(실서버 공동인증 모듈, 운영 PC 2006 실측 2026-09-28)."""
import os
from pathlib import Path

from kp_arb.gateways.xing_com import Win32ComFactory, register_install_dir


def test_register_install_dir_prepends_path_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PATH", r"C:\Windows\system32")
    register_install_dir(tmp_path)
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[0] == str(tmp_path) and parts[1] == r"C:\Windows\system32"
    register_install_dir(str(tmp_path))  # 두 번 불러도 중복 없음
    assert os.environ["PATH"].split(os.pathsep).count(str(tmp_path)) == 1


def test_factory_registers_install_dir_when_given(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PATH", "")
    Win32ComFactory()  # 폴더를 안 주면 아무것도 안 함(모의·테스트)
    assert os.environ["PATH"] == ""
    Win32ComFactory(tmp_path)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)
