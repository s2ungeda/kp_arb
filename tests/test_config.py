"""LSAccounts·SecretProvider 테스트. 더미 provider 사용(실 계좌·keyring 미사용), 비밀 비노출."""
import pytest

from kp_arb.config import (
    ChainedSecrets,
    ConfigError,
    EnvSecrets,
    LSAccount,
    LSAccounts,
    RunMode,
    current_mode,
)
from kp_arb.domain.enums import Account

_SECRETS = {
    "LS_STOCK_ACCT": "111-01",
    "LS_STOCK_ACCT_PW": "0000",
    "LS_STOCK_APPKEY": "stock-ak",
    "LS_STOCK_APPSECRET": "stock-as",
    "LS_DERIV_ACCT": "222-51",
    "LS_DERIV_ACCT_PW": "1234",
    "LS_DERIV_APPKEY": "deriv-ak",
    "LS_DERIV_APPSECRET": "deriv-as",
}


class MockSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


# --- 실행 모드 (KP_MODE) ---


def test_current_mode_default_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KP_MODE", raising=False)
    assert current_mode() is RunMode.PAPER  # 안전 기본값


def test_current_mode_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KP_MODE", "live")
    assert current_mode() is RunMode.LIVE


def test_current_mode_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KP_MODE", "bogus")
    with pytest.raises(ConfigError):
        current_mode()


# --- SecretProvider ---


def test_env_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO_XYZ", "bar")
    assert EnvSecrets().get("FOO_XYZ") == "bar"
    assert EnvSecrets().get("NOPE_XYZ") is None


def test_chained_prefers_first_then_falls_back() -> None:
    chain = ChainedSecrets(MockSecrets({"X": "a"}), MockSecrets({"X": "b", "Y": "c"}))
    assert chain.get("X") == "a"   # 앞 provider 우선
    assert chain.get("Y") == "c"   # 폴백
    assert chain.get("Z") is None


# --- LSAccounts.load ---


def test_load_with_provider() -> None:
    accounts = LSAccounts.load(MockSecrets(_SECRETS))
    stock = accounts.for_account(Account.KR_STOCK)
    deriv = accounts.for_account(Account.KR_DERIV)
    assert stock.number == "111-01" and stock.appkey == "stock-ak"
    assert stock.appsecret == "stock-as" and stock.password == "0000"
    assert deriv.appkey == "deriv-ak"  # 계좌별 키


def test_load_missing_raises() -> None:
    with pytest.raises(ConfigError):
        LSAccounts.load(MockSecrets({}))


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    accounts = LSAccounts.from_env()
    assert accounts.for_account(Account.KR_STOCK).appkey == "stock-ak"


# --- 비밀 비노출 ---


def test_secrets_masked_in_repr() -> None:
    acct = LSAccount(number="111-01", password="supersecret", appkey="AK", appsecret="topsecret")
    text = repr(acct)
    assert "supersecret" not in text and "topsecret" not in text
    assert "***" in text


def test_accounts_repr_has_no_secrets() -> None:
    accounts = LSAccounts(
        LSAccount("111-01", "pw1", "ak1", "as1"),
        LSAccount("222-51", "pw2", "ak2", "as2"),
    )
    text = repr(accounts)
    assert all(s not in text for s in ("pw1", "pw2", "as1", "as2", "111-01"))
