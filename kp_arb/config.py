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

from pydantic import BaseModel

from .domain.enums import Account, Underlying

KEYRING_SERVICE = "kp-arb"

# 전체 비밀 이름 목록 (메인 화면 '키 등록' 창·secrets_cli 공용)
SECRET_NAMES: tuple[tuple[str, str], ...] = (
    ("LS_STOCK_APPKEY", "LS 주식계좌 AppKey"),
    ("LS_STOCK_APPSECRET", "LS 주식계좌 AppSecret"),
    ("LS_STOCK_ACCT", "LS 주식계좌 번호"),
    ("LS_STOCK_ACCT_PW", "LS 주식계좌 비밀번호"),
    ("LS_DERIV_APPKEY", "LS 선물계좌 AppKey"),
    ("LS_DERIV_APPSECRET", "LS 선물계좌 AppSecret"),
    ("LS_DERIV_ACCT", "LS 선물계좌 번호"),
    ("LS_DERIV_ACCT_PW", "LS 선물계좌 비밀번호"),
    ("HL_AGENT_KEY", "HL 에이전트 키"),
    ("HL_ACCOUNT_ADDRESS", "HL 메인 주소"),
)


class ConfigError(RuntimeError):
    """설정/비밀 누락."""


class RunMode(StrEnum):
    PAPER = "paper"  # 모의투자 (기본 — 안전)
    LIVE = "live"    # 운영 (실거래)


def current_mode() -> RunMode:
    """실행 모드. 환경변수 ``KP_MODE`` 우선 → 자격증명관리자(키 등록 창에서 저장)
    → 안전 기본값 paper(모의)."""
    raw = os.environ.get("KP_MODE")
    if not raw:
        raw = KeyringSecrets().get("KP_MODE") or RunMode.PAPER.value
    raw = raw.lower()
    try:
        return RunMode(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid KP_MODE {raw!r} (paper|live)") from exc


class SecretProvider(Protocol):
    """비밀 조회 계약. 없으면 None."""

    def get(self, name: str) -> str | None: ...


class EnvSecrets:
    """OS 환경변수에서 조회. 빈 문자열은 '없음'으로 취급(빈 .env 값이 keyring을 가리지 않게)."""

    def get(self, name: str) -> str | None:
        return os.environ.get(name) or None


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
        return value if isinstance(value, str) and value else None


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
            if not value:
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


# --- 취급 종목 설정 (config.yaml — 비밀 아님, git 이력 관리. DESIGN §11) ---

DEFAULT_CONFIG_PATH = "config.yaml"


class SymbolConfig(BaseModel):
    """underlying 1종의 취급 종목. etf가 없으면 그 underlying은 ETF 미취급."""

    stock: str
    hl: str
    etf: str | None = None


class CarryRates(BaseModel):
    """캐리 이론가 연이자율 (DESIGN §6.1 — 전략 조정 대상이라 설정으로 노출)."""

    stock_futures: float = 0.035  # 주식선물 이론가 (배당 무시 비용캐리)
    fx: float = 0.015             # 원달러선물 → 현물환율 환산 (금리차)


class FxSpotWindow(BaseModel):
    """외환현물 사용 시간대 (DESIGN §6.1 — 창 안은 현물, 밖은 선물이론가로 HL 환산)."""

    start: str = "07:50"
    end: str = "18:10"


class FeeRates(BaseModel):
    """왕복 수수료·세금 (명목 대비 비율) — 순진입 계산용 (DESIGN §6.1)."""

    stock: float = 0.0007          # HL+주식 쌍 왕복 (거래세 반영 여부는 계좌 기준 조정)
    etf: float = 0.0007            # HL+ETF 쌍 왕복 (미취급 — 보존)
    stock_future: float = 0.00042  # HL+주식선물 쌍 왕복


class AppConfig(BaseModel):
    """config.yaml 전체. 종목 매핑 + ETF 승수 + 이론가·수수료 인자."""

    symbols: dict[Underlying, SymbolConfig]
    etf_leverage: float = 2.0
    carry_rates: CarryRates = CarryRates()
    fees: FeeRates = FeeRates()
    fx_spot_window: FxSpotWindow = FxSpotWindow()

    def etf_symbols(self) -> dict[Underlying, str]:
        return {u: s.etf for u, s in self.symbols.items() if s.etf is not None}

    def hl_symbols(self) -> dict[Underlying, str]:
        return {u: s.hl for u, s in self.symbols.items()}


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """config.yaml 로드 + 검증. stock 코드가 도메인 enum과 다르면 에러(오타 방지)."""
    import yaml

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    config = AppConfig.model_validate(raw)
    for underlying, symbol in config.symbols.items():
        if symbol.stock != underlying.krx_code:
            raise ConfigError(
                f"{underlying}: stock {symbol.stock!r} != krx_code {underlying.krx_code!r}"
            )
    return config
