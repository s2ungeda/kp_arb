"""LS OAuth2 토큰 매니저 계약 테스트. 라이브 API 호출 없음(mock 전송 + 가짜 시계)."""
import asyncio

import pytest

from kp_arb.gateways.ls_auth import TokenError, TokenManager, TokenResponse


class FakeClock:
    """주입형 가짜 시계. advance로 시간을 앞으로 민다."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class StubTransport:
    """성공 응답 mock. 호출 횟수를 세고 매번 다른 토큰을 발급."""

    def __init__(self, expires_in: float = 3600.0) -> None:
        self.calls = 0
        self.expires_in = expires_in

    async def fetch_token(self, appkey: str, appsecret: str) -> TokenResponse:
        self.calls += 1
        return TokenResponse(access_token=f"tok-{self.calls}", expires_in=self.expires_in)


class FlakyTransport:
    """fail_times번 실패 후 성공하는 mock."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def fetch_token(self, appkey: str, appsecret: str) -> TokenResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("transport boom")
        return TokenResponse(access_token="tok-ok", expires_in=3600.0)


async def test_issues_and_caches_token() -> None:
    transport = StubTransport()
    tm = TokenManager("k", "s", transport, now=FakeClock(), refresh_margin_s=60.0)
    assert await tm.get_token() == "tok-1"
    assert await tm.get_token() == "tok-1"  # 캐시 재사용
    assert transport.calls == 1


async def test_refreshes_when_near_expiry() -> None:
    clock = FakeClock()
    transport = StubTransport(expires_in=3600.0)
    tm = TokenManager("k", "s", transport, now=clock, refresh_margin_s=60.0)
    assert await tm.get_token() == "tok-1"

    clock.advance(3600.0 - 60.0 - 1.0)  # 갱신 마진 직전 → 아직 유효
    assert await tm.get_token() == "tok-1"
    assert transport.calls == 1

    clock.advance(2.0)  # 갱신 마진 진입 → 재발급
    assert await tm.get_token() == "tok-2"
    assert transport.calls == 2


async def test_retries_until_success() -> None:
    transport = FlakyTransport(fail_times=2)
    tm = TokenManager("k", "s", transport, max_retries=3)
    assert await tm.get_token() == "tok-ok"
    assert transport.calls == 3


async def test_raises_after_max_retries() -> None:
    transport = FlakyTransport(fail_times=5)
    tm = TokenManager("k", "s", transport, max_retries=3)
    with pytest.raises(TokenError):
        await tm.get_token()
    assert transport.calls == 3


async def test_concurrent_calls_issue_once() -> None:
    transport = StubTransport()
    tm = TokenManager("k", "s", transport)
    tokens = await asyncio.gather(*[tm.get_token() for _ in range(5)])
    assert tokens == ["tok-1"] * 5
    assert transport.calls == 1  # single-flight


def test_secret_not_in_repr() -> None:
    tm = TokenManager("appkey-123", "supersecret", StubTransport())
    text = repr(tm)
    assert "supersecret" not in text
    assert "appkey-123" not in text
