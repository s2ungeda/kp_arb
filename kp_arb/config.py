"""설정·비밀 로드 (DESIGN.md §11, CLAUDE.md §5).

비밀 저장은 **Windows 자격증명관리자(DPAPI, keyring)** 우선, OS 환경변수는 오버라이드/폴백.
평문 파일 저장 금지. 값은 실행 중에만 메모리에 있고 repr은 마스킹.

실행 모드는 ``KP_MODE`` 환경변수로 사용자가 전환한다(기본 ``paper``=모의, 안전). 모드는
엔드포인트/안전 게이트 선택용 플래그이며, 비밀 이름은 모드와 무관하게 동일하다.

LS 키는 **계좌별로 존재**한다. 이름: LS_STOCK_APPKEY/APPSECRET/ACCT/ACCT_PW, LS_DERIV_...,
HL_AGENT_KEY. 등록: ``python -m kp_arb.secrets_cli set <NAME>``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .domain.enums import Account

KEYRING_SERVICE = "kp-arb"


class ConfigError(RuntimeError):
    """설정/비밀 누락."""


class RunMode(StrEnum):
    PAPER = "paper"  # 모의투자 (기본 — 안전)
    LIVE = "live"    # 운영 (실거래)


def current_mode() -> RunMode:
    """실행 모드. ``KP_MODE`` 미설정 시 안전 기본값 paper(모의)."""
    raw = os.environ.get("KP_MODE", RunMode.PAPER.value).lower()
    try:
        return RunMode(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid KP_MODE {raw!r} (paper|live)") from exc


class SecretProvider(Protocol):
    """비밀 조회 계약. 없으면 None."""

    def get(self, name: str) -> str | None: ...


class EnvSecrets:
    """OS 환경변수에서 조회."""

    def get(self, name: str) -> str | None:
        return os.environ.get(name)


class KeyringSecrets:
    """Windows 자격증명관리자(DPAPI) 등 keyring 백엔드에서 조회. 백엔드 없으면 None."""

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self._service = service

    def get(self, name: str) -> str | None:
        try:
            import keyring
        except ImportError:
            return None
        try:
            value = keyring.get_password(self._service, name)
        except Exception:  # 백엔드 미가용 등 → 폴백
            return None
        return value if isinstance(value, str) else None


class ChainedSecrets:
    """여러 provider를 순서대로 조회, 처음 발견값 반환."""

    def __init__(self, *providers: SecretProvider) -> None:
        self._providers = providers

    def get(self, name: str) -> str | None:
        for provider in self._providers:
            value = provider.get(name)
            if value is not None:
                return value
        return None


def default_secrets() -> SecretProvider:
    """env 오버라이드 → Windows 자격증명관리자 순."""
    return ChainedSecrets(EnvSecrets(), KeyringSecrets())


@dataclass(frozen=True, repr=False)
class LSAccount:
    """계좌별 자격 — 번호·비번·appkey·appsecret. appsecret/password는 repr 마스킹."""

    number: str
    password: str
    appkey: str
    appsecret: str

    def __repr__(self) -> str:
        return (
            f"LSAccount(number={self.number!r}, appkey={self.appkey!r}, "
            "appsecret=***, password=***)"
        )


class LSAccounts:
    """LS 2계좌(주식/선물옵션)의 자격."""

    def __init__(self, stock: LSAccount, deriv: LSAccount) -> None:
        self._by_account = {Account.KR_STOCK: stock, Account.KR_DERIV: deriv}

    @classmethod
    def load(cls, secrets: SecretProvider | None = None) -> LSAccounts:
        provider = secrets if secrets is not None else default_secrets()

        def req(name: str) -> str:
            value = provider.get(name)
            if value is None:
                raise ConfigError(f"missing secret {name}")
            return value

        return cls(
            stock=LSAccount(
                number=req("LS_STOCK_ACCT"),
                password=req("LS_STOCK_ACCT_PW"),
                appkey=req("LS_STOCK_APPKEY"),
                appsecret=req("LS_STOCK_APPSECRET"),
            ),
            deriv=LSAccount(
                number=req("LS_DERIV_ACCT"),
                password=req("LS_DERIV_ACCT_PW"),
                appkey=req("LS_DERIV_APPKEY"),
                appsecret=req("LS_DERIV_APPSECRET"),
            ),
        )

    @classmethod
    def from_env(cls) -> LSAccounts:
        """환경변수만 사용(폴백/CI). = ``load(EnvSecrets())``."""
        return cls.load(EnvSecrets())

    def for_account(self, account: Account) -> LSAccount:
        return self._by_account[account]

    def __repr__(self) -> str:
        return f"LSAccounts({list(self._by_account)})"  # 자격값 비노출
