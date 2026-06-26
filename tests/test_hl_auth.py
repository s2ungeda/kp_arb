"""HL 에이전트 서명/연결 계약 테스트. 실제 키 없음(주입형 mock 서명자)."""
from typing import Any

import pytest

from kp_arb.gateways.hl import HLApiGateway
from kp_arb.gateways.hl_auth import HLAuth, HLAuthError, Signature


class MockSigner:
    """주입형 mock 서명자. 실제 키 없이 고정 서명을 반환하고 호출을 기록."""

    def __init__(self, address: str = "0xAGENT", *, sig: Signature | None = None) -> None:
        self._address = address
        self._sig = sig or Signature(r="0xrr", s="0xss", v=27)
        self.signed: list[tuple[dict[str, Any], int]] = []

    @property
    def address(self) -> str:
        return self._address

    def sign_l1_action(self, action: dict[str, Any], nonce: int) -> Signature:
        self.signed.append((action, nonce))
        return self._sig


# --- 서명 페이로드 구성 ---


def test_signed_request_envelope() -> None:
    signer = MockSigner()
    auth = HLAuth(signer)
    action = {"type": "order", "coin": "SAMSUNG", "is_buy": True, "sz": 1}
    req = auth.signed_request(action, nonce=1700000000123)

    assert req["action"] == action
    assert req["nonce"] == 1700000000123
    assert req["signature"] == {"r": "0xrr", "s": "0xss", "v": 27}
    assert signer.signed == [(action, 1700000000123)]  # 서명자에 action+nonce 전달


def test_headers_content_type() -> None:
    auth = HLAuth(MockSigner())
    assert auth.headers()["content-type"] == "application/json"


def test_agent_address_from_signer() -> None:
    auth = HLAuth(MockSigner(address="0xABCDEF"))
    assert auth.agent_address == "0xABCDEF"


# --- env에서만 키 읽기 + 평문 비노출 ---


def test_from_env_reads_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def factory(secret: str) -> MockSigner:
        captured["secret"] = secret
        return MockSigner(address="0xFROMENV")

    monkeypatch.setenv("HL_AGENT_KEY", "supersecret-key")
    auth = HLAuth.from_env(factory)
    assert captured["secret"] == "supersecret-key"  # 키는 factory(signer)에만 전달
    assert auth.agent_address == "0xFROMENV"


def test_from_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HL_AGENT_KEY", raising=False)
    with pytest.raises(HLAuthError):
        HLAuth.from_env(lambda secret: MockSigner())


def test_repr_has_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HL_AGENT_KEY", "supersecret-key")
    auth = HLAuth.from_env(lambda secret: MockSigner(address="0xAGENT"))
    assert "supersecret-key" not in repr(auth)


# --- connect ---


async def test_connect_sets_connected() -> None:
    gw = HLApiGateway(MockSigner())
    await gw.connect()
    assert gw.connected
    assert gw.auth.agent_address == "0xAGENT"


async def test_connect_requires_agent_address() -> None:
    gw = HLApiGateway(MockSigner(address=""))
    with pytest.raises(HLAuthError):
        await gw.connect()
