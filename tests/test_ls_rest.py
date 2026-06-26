"""LS REST 공통 계층 계약 테스트. 라이브 네트워크 없음(mock 전송 + 가짜 시계)."""
from typing import Any

import pytest

from kp_arb.gateways.ls_auth import TokenManager, TokenResponse
from kp_arb.gateways.ls_rest import (
    LSRestClient,
    RateLimiter,
    RateLimitError,
    RestError,
    RestResponse,
    build_headers,
)

BASE_URL = "https://openapi.ls-sec.co.kr:8080"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TokenStub:
    async def fetch_token(self, appkey: str, appsecret: str) -> TokenResponse:
        return TokenResponse(access_token="tok", expires_in=3600.0)


class RecordingTransport:
    """200을 돌려주고 마지막 요청을 기록하는 mock."""

    def __init__(self) -> None:
        self.calls = 0
        self.last: dict[str, Any] = {}

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        self.calls += 1
        self.last = {"method": method, "url": url, "headers": headers, "body": body}
        return RestResponse(status_code=200, body={"rsp_cd": "00000"})


class FlakyTransport:
    """fail_times번 예외 후 200."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("transport boom")
        return RestResponse(status_code=200, body={})


class ServerErrorTransport:
    """error_times번 503 후 200."""

    def __init__(self, error_times: int) -> None:
        self.error_times = error_times
        self.calls = 0

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        self.calls += 1
        if self.calls <= self.error_times:
            return RestResponse(status_code=503, body={})
        return RestResponse(status_code=200, body={})


def _client(
    transport: Any,
    clock: FakeClock,
    *,
    limiter: RateLimiter | None = None,
    max_retries: int = 3,
) -> LSRestClient:
    tm = TokenManager("k", "s", TokenStub(), now=clock)
    rl = limiter or RateLimiter(now=clock)
    return LSRestClient(BASE_URL, tm, transport, rl, max_retries=max_retries)


# --- 헤더 구성 (순수 함수) ---


def test_build_headers() -> None:
    h = build_headers("CSPAT00601", "tok123")
    assert h["authorization"] == "Bearer tok123"
    assert h["tr_cd"] == "CSPAT00601"
    assert h["tr_cont"] == "N"
    assert h["content-type"].startswith("application/json")


async def test_request_injects_bearer_and_tr_cd() -> None:
    transport = RecordingTransport()
    client = _client(transport, FakeClock())
    resp = await client.request("CSPAQ12300", {"foo": 1}, path="/stock/balance")
    assert resp.status_code == 200
    assert transport.last["url"] == f"{BASE_URL}/stock/balance"
    assert transport.last["headers"]["authorization"] == "Bearer tok"
    assert transport.last["headers"]["tr_cd"] == "CSPAQ12300"
    assert transport.last["body"] == {"foo": 1}


# --- 레이트리밋 ---


async def test_per_second_limit_blocks() -> None:
    clock = FakeClock()
    transport = RecordingTransport()
    limiter = RateLimiter(now=clock, default_per_second=2)
    client = _client(transport, clock, limiter=limiter)

    await client.request("Q1")
    await client.request("Q1")
    with pytest.raises(RateLimitError):
        await client.request("Q1")  # 같은 초 3번째 → 차단
    assert transport.calls == 2  # 차단된 호출은 전송되지 않음

    clock.advance(1.0)  # 1초 경과 → 윈도우 비워짐
    await client.request("Q1")
    assert transport.calls == 3


async def test_per_second_limit_is_per_tr() -> None:
    clock = FakeClock()
    transport = RecordingTransport()
    limiter = RateLimiter(now=clock, default_per_second=1)
    client = _client(transport, clock, limiter=limiter)

    await client.request("A")
    await client.request("B")  # 다른 tr_cd → 독립 한도
    assert transport.calls == 2


async def test_daily_cap_blocks_and_resets_next_day() -> None:
    clock = FakeClock()
    transport = RecordingTransport()
    limiter = RateLimiter(now=clock, daily_cap=2, default_per_second=100)
    client = _client(transport, clock, limiter=limiter)

    await client.request("Q")
    await client.request("Q")
    with pytest.raises(RateLimitError):
        await client.request("Q")  # 일 한도 2 초과

    clock.advance(86_400.0)  # 다음 날 → 일 카운트 리셋
    await client.request("Q")
    assert transport.calls == 3


# --- 재시도 ---


async def test_retries_transport_error_then_succeeds() -> None:
    clock = FakeClock()
    transport = FlakyTransport(fail_times=2)
    client = _client(transport, clock, max_retries=3)
    resp = await client.request("Q")
    assert resp.status_code == 200
    assert transport.calls == 3


async def test_retries_on_server_error() -> None:
    clock = FakeClock()
    transport = ServerErrorTransport(error_times=1)
    client = _client(transport, clock, max_retries=3)
    resp = await client.request("Q")
    assert resp.status_code == 200
    assert transport.calls == 2


async def test_raises_after_retries_exhausted() -> None:
    clock = FakeClock()
    transport = FlakyTransport(fail_times=5)
    client = _client(transport, clock, max_retries=3)
    with pytest.raises(RestError):
        await client.request("Q")
    assert transport.calls == 3
