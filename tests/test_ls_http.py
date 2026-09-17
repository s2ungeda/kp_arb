"""LS 실 HTTP 전송 계약 테스트. 가짜 aiohttp 세션(라이브 호출 없음) — 요청 구성·응답 파싱."""
from __future__ import annotations

import json
from typing import Any

from kp_arb.gateways.ls_http import AiohttpRestTransport, AiohttpTokenTransport


class FakeResp:
    def __init__(self, status: int, *, json_body: Any = None, text_body: str = "") -> None:
        self.status = status
        self._json = json_body
        self._text = text_body

    async def json(self, content_type: Any = None) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> FakeResp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    def __init__(self, resp: FakeResp) -> None:
        self._resp = resp
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, data: Any = None, headers: Any = None,
             timeout: Any = None) -> FakeResp:
        self.calls.append({"method": "POST", "url": url, "data": data, "headers": headers,
                           "timeout": timeout})
        return self._resp

    def request(self, method: str, url: str, *, headers: Any = None, data: Any = None,
                timeout: Any = None) -> FakeResp:
        self.calls.append({"method": method, "url": url, "data": data, "headers": headers,
                           "timeout": timeout})
        return self._resp


async def test_token_transport_posts_form_and_parses() -> None:
    session = FakeSession(FakeResp(200, json_body={"access_token": "tok", "expires_in": 3600}))
    tx = AiohttpTokenTransport(session, "https://openapi.ls-sec.co.kr:8080", scope="oob")
    token = await tx.fetch_token("my-appkey", "my-secret")

    assert token.access_token == "tok"
    assert token.expires_in == 3600
    call = session.calls[-1]
    assert call["url"] == "https://openapi.ls-sec.co.kr:8080/oauth2/token"
    assert call["data"]["grant_type"] == "client_credentials"
    assert call["data"]["appkey"] == "my-appkey"
    assert call["data"]["appsecretkey"] == "my-secret"
    assert call["data"]["scope"] == "oob"
    assert "x-www-form-urlencoded" in call["headers"]["content-type"]


async def test_token_transport_default_scope_and_expiry() -> None:
    session = FakeSession(FakeResp(200, json_body={"access_token": "tok"}))
    tx = AiohttpTokenTransport(session, "https://x")  # 기본 scope=oob
    token = await tx.fetch_token("k", "s")
    assert token.expires_in == 86400  # 기본값
    assert session.calls[-1]["data"]["scope"] == "oob"  # LS 필수 scope


async def test_token_transport_empty_scope_omitted() -> None:
    session = FakeSession(FakeResp(200, json_body={"access_token": "tok", "expires_in": 100}))
    tx = AiohttpTokenTransport(session, "https://x", scope="")
    await tx.fetch_token("k", "s")
    assert "scope" not in session.calls[-1]["data"]


async def test_rest_transport_sends_json_and_parses() -> None:
    session = FakeSession(FakeResp(200, json_body={"rsp_cd": "00000", "x": 1}))
    tx = AiohttpRestTransport(session)
    resp = await tx.request("POST", "https://x/stock/accno", {"tr_cd": "CSPAQ22200"}, {"a": 1})

    assert resp.status_code == 200
    assert resp.body == {"rsp_cd": "00000", "x": 1}
    call = session.calls[-1]
    assert call["url"] == "https://x/stock/accno"
    assert call["headers"]["tr_cd"] == "CSPAQ22200"
    assert json.loads(call["data"]) == {"a": 1}  # body가 JSON 직렬화됨


async def test_requests_carry_10s_timeout() -> None:
    # 사용자 확정 2026-09-14: LS REST 요청 1건 최대 10초
    # (전엔 aiohttp 기본 5분 — 운영 실측 잔고 조회 72초가 시동을 붙잡음).
    from kp_arb.gateways.ls_http import REQUEST_TIMEOUT_S

    assert REQUEST_TIMEOUT_S == 10.0
    session = FakeSession(FakeResp(200, json_body={"access_token": "tok", "rsp_cd": "00000"}))
    await AiohttpTokenTransport(session, "https://x").fetch_token("k", "s")
    await AiohttpRestTransport(session).request("POST", "https://x", {}, None)
    for call in session.calls:
        assert call["timeout"] is not None and call["timeout"].total == 10.0


async def test_order_tr_requests_carry_30s_timeout() -> None:
    # 사용자 확정 2026-09-17: 주문 TR(발주·정정·취소)은 30초 — 운영 실측 LS 주문 서버 지연 10~14초가
    # 10초 상한에 걸려 세트가 멈춤. 조회 TR은 그대로 10초.
    from kp_arb.gateways.ls_http import ORDER_REQUEST_TIMEOUT_S, AiohttpRestTransport

    session = FakeSession(FakeResp(200, json_body={"rsp_cd": "00040"}))
    tx = AiohttpRestTransport(session)
    await tx.request("POST", "https://x/stock/order", {"tr_cd": "CSPAT00601"}, {"a": 1})
    await tx.request("POST", "https://x/futureoption/order", {"tr_cd": "CFOAT00300"}, {"a": 1})
    await tx.request("POST", "https://x/stock/accno", {"tr_cd": "CSPAQ22200"}, {"a": 1})
    assert session.calls[0]["timeout"].total == ORDER_REQUEST_TIMEOUT_S == 30.0
    assert session.calls[1]["timeout"].total == 30.0
    assert session.calls[2]["timeout"].total == 10.0


async def test_rest_transport_non_json_falls_back_to_raw() -> None:
    session = FakeSession(FakeResp(500, text_body="Internal Error"))
    tx = AiohttpRestTransport(session)
    resp = await tx.request("POST", "https://x", {}, None)
    assert resp.status_code == 500
    assert resp.body == {"raw": "Internal Error"}
