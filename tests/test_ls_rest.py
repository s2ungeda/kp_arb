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
    mask_secrets,
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


# --- 로그용 비밀 마스킹 (순수 함수) ---


def test_mask_secrets_masks_password_keeps_account() -> None:
    body = {"CSPAQ22200InBlock1": {"AcntNo": "20142871001", "Pwd": "1004"}}
    masked = mask_secrets(body)
    inner = masked["CSPAQ22200InBlock1"]
    assert inner["AcntNo"] == "20142871001"   # 계좌번호는 그대로(대조용)
    assert inner["Pwd"] == "1**4(len=4)"      # 비번은 평문 아님
    assert "1004" not in repr(masked)         # 평문 유출 없음


def test_mask_secrets_flat_short_and_nonstr() -> None:
    flat = mask_secrets({"AcntNo": "20142871001", "InptPwd": "12", "qty": 5})
    assert flat["AcntNo"] == "20142871001"
    assert flat["InptPwd"] == "**(len=2)"     # 2자 이하는 앞뒤 노출 안 함
    assert flat["qty"] == 5                     # 비문자열은 그대로
    assert mask_secrets({"passwd": ""})["passwd"] == "<빈값>"


class RejectingTransport:
    """거부 rsp_cd를 돌려주는 mock — 계좌비번 오류(03669) 재현."""

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        return RestResponse(
            status_code=200,
            body={"rsp_cd": "03669", "rsp_msg": "비밀번호 오류입니다."},
        )


async def test_rejection_logs_masked_body(caplog: pytest.LogCaptureFixture) -> None:
    client = _client(RejectingTransport(), FakeClock())
    body = {"CSPAQ22200InBlock1": {"AcntNo": "20142871001", "Pwd": "1004"}}
    with caplog.at_level("WARNING"):
        resp = await client.request("CSPAQ22200", body, path="/stock/accno")
    assert resp.body["rsp_cd"] == "03669"
    text = caplog.text
    assert "20142871001" in text and "03669" in text  # 계좌번호·코드는 보임
    assert "1004" not in text                          # 비번 평문은 로그에 없음
    assert "1**4(len=4)" in text                        # 마스킹된 형태로만
