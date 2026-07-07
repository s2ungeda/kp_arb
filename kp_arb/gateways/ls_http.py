"""LS Open API 실 HTTP 전송 (aiohttp). 라이브 결선용.

- ``AiohttpTokenTransport``: OAuth2 토큰 발급(POST /oauth2/token, form-encoded).
- ``AiohttpRestTransport``: TR 요청(POST JSON + 헤더).

주입형 ``ClientSession`` 뒤에서 동작하며, 테스트는 가짜 세션으로 요청 구성·응답 파싱만 검증한다
(pytest에서 실 네트워크 호출 금지). 실제 접속은 ``paper_check`` 등 수동 실행에서만.
"""
from __future__ import annotations

import json
from typing import Any

from .ls_auth import TokenResponse
from .ls_rest import RestResponse


class AiohttpTokenTransport:
    """LS OAuth2 토큰 발급. ``TokenTransport`` 구현."""

    def __init__(self, session: Any, base_url: str, *, scope: str = "oob") -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._scope = scope

    async def fetch_token(self, appkey: str, appsecret: str) -> TokenResponse:
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "appkey": appkey,
            "appsecretkey": appsecret,
        }
        if self._scope:
            data["scope"] = self._scope
        headers = {"content-type": "application/x-www-form-urlencoded"}
        async with self._session.post(
            f"{self._base}/oauth2/token", data=data, headers=headers
        ) as resp:
            body = await resp.json(content_type=None)
        if "access_token" not in body:
            # 서버 거부 사유를 그대로 노출 (appkey/시크릿 오류, 모의/운영 불일치 등)
            raise RuntimeError(f"LS 토큰 발급 거부 (appkey …{appkey[-4:]}): {body}")
        return TokenResponse(
            access_token=str(body["access_token"]),
            token_type=str(body.get("token_type", "Bearer")),
            expires_in=float(body.get("expires_in", 86400)),
        )


class AiohttpRestTransport:
    """LS TR REST 요청. ``RestTransport`` 구현. 헤더(Bearer·tr_cd)는 호출측에서 구성."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> RestResponse:
        payload = json.dumps(body) if body is not None else None
        async with self._session.request(method, url, headers=headers, data=payload) as resp:
            status = int(resp.status)
            try:
                parsed = await resp.json(content_type=None)
            except Exception:  # JSON 아님 → 원문 보존
                parsed = {"raw": await resp.text()}
        data = parsed if isinstance(parsed, dict) else {"data": parsed}
        return RestResponse(status_code=status, body=data)
