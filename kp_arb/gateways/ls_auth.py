"""LS Open API OAuth2 토큰 매니저 (DESIGN.md §5.1).

라이브 HTTP 없음: 토큰 요청은 주입된 ``TokenTransport``(Protocol) 뒤로 격리하고,
만료 판정은 주입된 시계(``now``)로 한다. 실제 전송(aiohttp) 구현은 이후 블록에서 채운다.
비밀값(appkey/appsecret)은 환경변수로만 받고, 로그·repr에 평문 노출하지 않는다.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, Field


class TokenError(RuntimeError):
    """토큰 발급 실패(재시도 소진 포함)."""


class TokenResponse(BaseModel):
    """LS OAuth2 토큰 응답에서 우리가 쓰는 필드만. 추가 필드는 무시."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: float = Field(gt=0)  # 초


class TokenTransport(Protocol):
    """토큰 요청 전송 계약. 테스트는 mock, 라이브는 aiohttp 구현(추후 블록)."""

    async def fetch_token(self, appkey: str, appsecret: str) -> TokenResponse: ...


class TokenManager:
    """OAuth2 access_token 발급·캐시·만료 전 자동 갱신.

    - ``get_token()``은 유효 토큰이 있으면 캐시를, 만료 임박/만료면 재발급한다.
    - 동시 호출은 단일 락으로 묶어 중복 발급을 막는다(single-flight).
    - 발급 실패는 ``max_retries``까지 재시도하고, 소진되면 ``TokenError``.
    """

    def __init__(
        self,
        appkey: str,
        appsecret: str,
        transport: TokenTransport,
        *,
        now: Callable[[], float] = time.monotonic,
        refresh_margin_s: float = 60.0,
        max_retries: int = 3,
        retry_backoff_s: float = 0.0,
    ) -> None:
        self._appkey = appkey
        self._appsecret = appsecret
        self._transport = transport
        self._now = now
        self._refresh_margin_s = refresh_margin_s
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(
        cls,
        transport: TokenTransport,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> TokenManager:
        """비밀값을 환경변수(LS_APPKEY/LS_APPSECRET)에서만 읽어 생성."""
        try:
            appkey = os.environ["LS_APPKEY"]
            appsecret = os.environ["LS_APPSECRET"]
        except KeyError as exc:
            raise TokenError(f"missing env var {exc}") from exc
        return cls(appkey, appsecret, transport, now=now)

    async def get_token(self) -> str:
        cached = self._cached()
        if cached is not None:
            return cached
        async with self._lock:
            # 락 대기 중 다른 호출이 이미 갱신했을 수 있다(double-check).
            cached = self._cached()
            if cached is not None:
                return cached
            return await self._refresh()

    def _cached(self) -> str | None:
        if self._token is None or self._now() >= self._expires_at:
            return None
        return self._token

    async def _refresh(self) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._transport.fetch_token(self._appkey, self._appsecret)
            except Exception as exc:  # 전송 계층의 임의 실패를 재시도
                last_exc = exc
                if attempt < self._max_retries and self._retry_backoff_s > 0:
                    await asyncio.sleep(self._retry_backoff_s)
                continue
            self._token = resp.access_token
            # 만료 직전 마진만큼 앞당겨 갱신 → 스테일 토큰 사용 방지.
            self._expires_at = self._now() + resp.expires_in - self._refresh_margin_s
            return resp.access_token
        raise TokenError(
            f"token issuance failed after {self._max_retries} attempts"
        ) from last_exc

    def __repr__(self) -> str:
        state = "valid" if self._cached() is not None else "empty"
        return f"TokenManager(appkey=***, token={state})"
