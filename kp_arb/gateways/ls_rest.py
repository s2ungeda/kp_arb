"""LS Open API REST 공통 계층 (DESIGN.md §5.1).

라이브 네트워크 없음: 실제 HTTP는 주입된 ``RestTransport``(Protocol) 뒤로 격리한다.
공통 책임:
- ``base_url`` + Bearer 토큰 주입(``TokenManager``) + ``tr_cd`` 헤더 구성.
- 레이트리밋 가드: 일 5,000회 + TR별 초당 한도(예: 조회 초당 2회). 초과 시 ``RateLimitError``.
- 지수 백오프 재시도(전송 예외/5xx). 소진 시 ``RestError``.

실제 전송(aiohttp) 구현과 TR별 path/한도 표는 이후 블록·config에서 채운다.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel

from .ls_auth import TokenManager

DEFAULT_DAILY_CAP = 5_000
DEFAULT_PER_SECOND = 2  # 조회 TR 기본 초당 한도


class RateLimitError(RuntimeError):
    """레이트리밋(일 한도 또는 TR별 초당 한도) 초과."""


class RestError(RuntimeError):
    """REST 호출 실패(재시도 소진 포함)."""


class RestResponse(BaseModel):
    """LS REST 응답(상태코드 + JSON 본문). 본문 스키마는 TR별로 다양."""

    status_code: int
    body: dict[str, Any] = {}


class RestTransport(Protocol):
    """실제 HTTP 전송 계약. 테스트는 mock, 라이브는 aiohttp 구현(추후 블록)."""

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse: ...


def build_headers(tr_cd: str, token: str, *, tr_cont: str = "N") -> dict[str, str]:
    """LS TR 호출 헤더 구성. Bearer 토큰 + tr_cd. (순수 함수)"""
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "tr_cd": tr_cd,
        "tr_cont": tr_cont,
    }


class RateLimiter:
    """일 한도 + TR별 초당 한도 가드. 주입형 시계(epoch 초)로 판정.

    초과 시 대기하지 않고 ``RateLimitError``를 던진다(호출자가 페이싱).
    """

    def __init__(
        self,
        *,
        now: Callable[[], float],
        daily_cap: int = DEFAULT_DAILY_CAP,
        default_per_second: int = DEFAULT_PER_SECOND,
        per_tr_per_second: dict[str, int] | None = None,
    ) -> None:
        self._now = now
        self._daily_cap = daily_cap
        self._default_per_second = default_per_second
        self._per_tr = dict(per_tr_per_second or {})
        self._day = -1
        self._daily_count = 0
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def check(self, tr_cd: str) -> None:
        """tr_cd 호출 1건을 허용 가능한지 판정하고, 가능하면 카운트에 반영."""
        t = self._now()

        day = int(t // 86_400)
        if day != self._day:
            self._day = day
            self._daily_count = 0
        if self._daily_count >= self._daily_cap:
            raise RateLimitError(f"daily cap {self._daily_cap} exceeded")

        recent = self._recent[tr_cd]
        cutoff = t - 1.0
        while recent and recent[0] <= cutoff:
            recent.popleft()
        limit = self._per_tr.get(tr_cd, self._default_per_second)
        if len(recent) >= limit:
            raise RateLimitError(f"per-second limit {limit} for {tr_cd} exceeded")

        recent.append(t)
        self._daily_count += 1


class LSRestClient:
    """LS REST 공통 클라이언트. 토큰 주입 + 레이트리밋 + 지수 백오프 재시도."""

    def __init__(
        self,
        base_url: str,
        token_manager: TokenManager,
        transport: RestTransport,
        rate_limiter: RateLimiter,
        *,
        max_retries: int = 3,
        backoff_base_s: float = 0.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tokens = token_manager
        self._transport = transport
        self._limiter = rate_limiter
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s

    async def request(
        self,
        tr_cd: str,
        body: dict[str, Any] | None = None,
        *,
        path: str = "/",
        method: str = "POST",
        tr_cont: str = "N",
    ) -> RestResponse:
        # 레이트리밋은 전송 전에 1회 판정(초과 시 전송하지 않고 즉시 차단).
        self._limiter.check(tr_cd)

        token = await self._tokens.get_token()
        headers = build_headers(tr_cd, token, tr_cont=tr_cont)
        url = f"{self._base_url}/{path.lstrip('/')}"

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._transport.request(method, url, headers, body)
            except Exception as exc:  # 전송 계층의 임의 실패를 재시도
                last_exc = exc
            else:
                if resp.status_code < 500:
                    return resp
                last_exc = RestError(f"server error {resp.status_code} for {tr_cd}")

            if attempt < self._max_retries:
                await self._sleep_backoff(attempt)

        raise RestError(
            f"REST {tr_cd} failed after {self._max_retries} attempts"
        ) from last_exc

    async def _sleep_backoff(self, attempt: int) -> None:
        if self._backoff_base_s > 0:
            await asyncio.sleep(self._backoff_base_s * (2 ** (attempt - 1)))
