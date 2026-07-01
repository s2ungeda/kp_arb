"""secrets_cli 테스트. 실 keyring 대신 sys.modules에 가짜 keyring 주입."""
import sys
from typing import Any

import pytest

from kp_arb import secrets_cli


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def delete_password(self, service: str, name: str) -> None:
        del self.store[(service, name)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


def test_set_uses_getpass_not_argv(fake_keyring: FakeKeyring) -> None:
    # 값은 인자가 아니라 prompt(getpass)로 받는다 → 히스토리 평문 노출 방지.
    def prompt(_: str) -> str:
        return "s3cret"

    rc = secrets_cli.main(["prog", "set", "LS_STOCK_APPKEY"], prompt=prompt)
    assert rc == 0
    assert fake_keyring.get_password("kp-arb", "LS_STOCK_APPKEY") == "s3cret"


def test_has_reports_presence(fake_keyring: FakeKeyring) -> None:
    fake_keyring.set_password("kp-arb", "X", "v")
    assert secrets_cli.main(["prog", "has", "X"]) == 0
    assert secrets_cli.main(["prog", "has", "Y"]) == 0  # 없어도 정상 종료(no)


def test_del_removes(fake_keyring: FakeKeyring) -> None:
    fake_keyring.set_password("kp-arb", "X", "v")
    assert secrets_cli.main(["prog", "del", "X"]) == 0
    assert fake_keyring.get_password("kp-arb", "X") is None


def test_bad_usage_returns_2(fake_keyring: FakeKeyring) -> None:
    assert secrets_cli.main(["prog"]) == 2
    assert secrets_cli.main(["prog", "bogus", "X"]) == 2


def test_default_prompt_is_getpass() -> None:
    import getpass

    # main의 prompt 기본값이 getpass.getpass(화면 비표시 입력)인지 확인.
    kwdefaults: dict[str, Any] = secrets_cli.main.__kwdefaults__
    assert kwdefaults["prompt"] is getpass.getpass
