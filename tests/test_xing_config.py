"""xingAPI 설정 — 전환 스위치(KP_LS_API)와 로그인 자격 로드(마스킹). 라이브 없음."""
from __future__ import annotations

import pytest

from kp_arb.config import (
    SECRET_NAMES,
    ConfigError,
    LsApi,
    XingCredentials,
    ls_api,
)


class _Secrets:
    def __init__(self, **values: str) -> None:
        self._v = values

    def get(self, name: str) -> str | None:
        return self._v.get(name)


def test_ls_api_switch_defaults_to_openapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KP_LS_API", raising=False)
    monkeypatch.setattr("kp_arb.config.KeyringSecrets.get", lambda self, name: None)
    assert ls_api() is LsApi.OPENAPI
    monkeypatch.setenv("KP_LS_API", "XING")
    assert ls_api() is LsApi.XING
    monkeypatch.setenv("KP_LS_API", "com")
    with pytest.raises(ConfigError):
        ls_api()


def test_xing_credentials_load_and_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("KP_XING_HOST", "KP_XING_PORT", "KP_XING_SERVER", "KP_XING_PATH"):
        monkeypatch.delenv(name, raising=False)
    secrets = _Secrets(LS_XING_ID="user", LS_XING_PW="S3cr3tLogin", LS_XING_CERT_PW="C3rtS3cr3t",
                       KP_XING_HOST="hts.example.co.kr")
    c = XingCredentials.load(secrets)
    assert (c.user_id, c.host, c.port, c.server_type, c.path) == (
        "user", "hts.example.co.kr", 20001, 0, r"C:\meme")
    assert "S3cr3tLogin" not in repr(c) and "C3rtS3cr3t" not in repr(c) and "***" in repr(c)
    # 환경변수가 우선, 모의서버·경로·포트
    monkeypatch.setenv("KP_XING_SERVER", "demo")
    monkeypatch.setenv("KP_XING_PORT", "20002")
    monkeypatch.setenv("KP_XING_PATH", r"D:\xing")
    c2 = XingCredentials.load(secrets)
    assert (c2.port, c2.server_type, c2.path) == (20002, 1, r"D:\xing")
    # 모의 서버는 인증서 비번 없이 됨(사용자 2026-09-23), 실서버는 필수
    no_cert = _Secrets(LS_XING_ID="u", LS_XING_PW="p", KP_XING_HOST="h")
    assert XingCredentials.load(no_cert).cert_password == "" and \
        XingCredentials.load(no_cert).server_type == 1
    monkeypatch.setenv("KP_XING_SERVER", "real")
    with pytest.raises(ConfigError, match="LS_XING_CERT_PW"):
        XingCredentials.load(no_cert)
    # 호스트·아이디 없으면 ConfigError(시동 실패 사유)
    with pytest.raises(ConfigError, match="KP_XING_HOST"):
        XingCredentials.load(_Secrets(LS_XING_ID="u", LS_XING_PW="p", LS_XING_CERT_PW="c"))
    with pytest.raises(ConfigError, match="LS_XING_ID"):
        XingCredentials.load(_Secrets(KP_XING_HOST="h", LS_XING_CERT_PW="c"))
    # 키 등록 창 목록에 xing 자격 3개가 있다
    names = [n for n, _label in SECRET_NAMES]
    assert {"LS_XING_ID", "LS_XING_PW", "LS_XING_CERT_PW"} <= set(names)
