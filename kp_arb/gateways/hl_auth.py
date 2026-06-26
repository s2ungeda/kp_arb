"""Hyperliquid 에이전트 지갑 서명 (DESIGN.md §5.2, §11).

EIP-712 L1 액션 서명 경계를 정의한다.
- 비밀키는 env에서만 읽고(``from_env``), 키는 signer 내부에만 둔다(게이트웨이/로그에 평문 금지).
- 서명 자체는 주입형 ``HLSigner``(Protocol) 뒤로 격리한다(라이브는 eth-account, 테스트는 mock).
- 정확한 connectionId(msgpack+keccak) 계산·chainId·source는 라이브 구현 시 hyperliquid-sdk로 확인.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel


class HLAuthError(RuntimeError):
    """HL 인증/서명 구성 실패."""


class Signature(BaseModel):
    """secp256k1 서명 컴포넌트 (r, s, v)."""

    r: str
    s: str
    v: int


class HLSigner(Protocol):
    """에이전트 지갑 서명 계약. 라이브는 eth-account 구현, 테스트는 mock."""

    @property
    def address(self) -> str: ...
    def sign_l1_action(self, action: dict[str, Any], nonce: int) -> Signature: ...


class HLAuth:
    """HL exchange 액션 서명 + 요청 봉투/헤더 구성. 키는 보유하지 않는다(signer가 보유)."""

    def __init__(self, signer: HLSigner) -> None:
        self._signer = signer

    @property
    def agent_address(self) -> str:
        return self._signer.address

    @classmethod
    def from_env(
        cls,
        signer_factory: Callable[[str], HLSigner],
        *,
        key_var: str = "HL_AGENT_KEY",
    ) -> HLAuth:
        """env에서 에이전트 비밀키를 읽어 signer를 생성. 키는 signer 내부에만 남는다."""
        try:
            secret = os.environ[key_var]
        except KeyError as exc:
            raise HLAuthError(f"missing env var {exc}") from exc
        return cls(signer_factory(secret))

    def signed_request(self, action: dict[str, Any], nonce: int) -> dict[str, Any]:
        """서명된 exchange 요청 봉투 구성: {action, nonce, signature}."""
        sig = self._signer.sign_l1_action(action, nonce)
        return {
            "action": action,
            "nonce": nonce,
            "signature": {"r": sig.r, "s": sig.s, "v": sig.v},
        }

    def headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}

    def __repr__(self) -> str:
        # 키는 보유하지 않으므로 노출 위험 없음(에이전트 주소만 표기).
        return f"HLAuth(agent={self.agent_address})"
