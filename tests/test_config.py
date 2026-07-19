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


def test_env_secrets_empty_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # 빈 .env 값(LS_X=)이 keyring 폴백을 가리지 않도록.
    monkeypatch.setenv("EMPTY_XYZ", "")
    assert EnvSecrets().get("EMPTY_XYZ") is None


def test_load_treats_empty_as_missing() -> None:
    from kp_arb.config import LSAccounts

    class Blanks:
        def get(self, name: str) -> str | None:
            return ""  # 전부 빈 값

    with pytest.raises(ConfigError):
        LSAccounts.load(Blanks())


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


# --- 취급 종목 설정 (config.yaml) ---


def test_load_config_real_file() -> None:
    """저장소의 config.yaml이 실제로 로드·검증을 통과하는지 (오타 방지 스모크)."""
    from kp_arb.config import load_config
    from kp_arb.domain.enums import Underlying

    config = load_config()
    assert config.hl_symbols()[Underlying.SAMSUNG] == "xyz:SMSN"
    assert config.etf_symbols() == {}  # ETF 미취급 (사용자 확정 2026-07-13)
    assert config.etf_leverage == 2.0


def test_load_config_rejects_wrong_stock_code(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from kp_arb.config import load_config

    bad = tmp_path / "config.yaml"
    bad.write_text(
        "symbols:\n  samsung: {stock: '999999', hl: 'xyz:SMSN'}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(str(bad))  # 도메인 enum(005930)과 불일치 → 에러


def test_load_config_missing_file() -> None:
    from kp_arb.config import load_config

    with pytest.raises(ConfigError):
        load_config("no_such_config.yaml")


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
