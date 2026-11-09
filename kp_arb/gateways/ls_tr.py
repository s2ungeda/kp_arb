"""LS TR 요청 공통 조각 — 전송 방식과 무관한 계약·오류·응답 꼴 (DESIGN-ls-xing.md §2).

2026-11-09 LS OpenAPI REST 경로(`ls_rest.LSRestClient`·`RateLimiter`·`ls_auth`·`ls_http`)를
지우면서(사용자 "REST 쓰는 곳 없으면 제거해") xing 경로(`xing.XingQueryClient`)와 게이트웨이
(`ls.LSApiGateway`)가 같이 쓰던 것만 여기 남겼다. 이름의 "Rest"는 옛 REST 시절 그대로 두어
호출처를 안 바꿨다(응답 꼴이 REST 본문과 같은 모양이라는 뜻으로 읽으면 된다).
"""
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

# 로그에 남길 때 마스킹할 비밀 필드(요청 본문 안의 비번). 계좌번호는 마스킹 안 함(대조용).
_SECRET_BODY_KEYS = frozenset({"Pwd", "InptPwd", "passwd"})


def _mask_secret(value: str) -> str:
    """비번을 로그용으로 마스킹 — 평문 금지(CLAUDE.md §5). 길이·앞뒤 1글자만 노출해
    "1004인지 다른 값인지" 대조만 가능하게 한다. (순수 함수)"""
    if not value:
        return "<빈값>"
    if len(value) <= 2:
        return "*" * len(value) + f"(len={len(value)})"
    return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]}(len={len(value)})"


def mask_secrets(body: Any) -> Any:
    """요청 본문을 로그에 남기기 안전한 형태로 — 비번 필드만 마스킹하고 계좌번호 등
    나머지는 그대로 둔다. 중첩된 ``{TR}InBlock`` 구조도 재귀 처리. (순수 함수)"""
    if isinstance(body, dict):
        return {
            k: (_mask_secret(v) if k in _SECRET_BODY_KEYS and isinstance(v, str)
                else mask_secrets(v))
            for k, v in body.items()
        }
    if isinstance(body, list):
        return [mask_secrets(x) for x in body]
    return body


class RateLimitError(RuntimeError):
    """요청 한도 초과 — xing이 알려 주는 TR별 한도(`XingQueryClient._check_limit`)."""


class RestError(RuntimeError):
    """TR 호출 실패(응답 거부·전송 실패 포함)."""


class RestTimeoutError(RestError):
    """주문 TR의 응답 없음(전송 실패·시간 초과) — 거부가 아니다. LS가 첫 요청을 이미 접수했을
    수 있어 재전송하지 않는다(실증 2026-09-16 09:20: 선물 발주 10초 시간 초과 → 재전송이 #3330,
    첫 요청은 #3326으로 접수돼 체결됐는데 코어가 몰라 헤지 없이 SF 1계약이 팔림)."""


# 주문 TR(신규·정정·취소) — LS 이름 규칙: 주식 CSPAT*, 선물옵션 CFOAT*
# ("AT" = 주문, "AQ/BQ" = 조회).
# 이 TR은 전송 실패·시간 초과에도 **재전송 금지**(주문이 두 장 될 수 있다) → RestTimeoutError.
_NO_RETRY_TR_PREFIXES = ("CSPAT", "CFOAT")


def is_order_tr(tr_cd: str) -> bool:
    """주문 TR인가(재전송 금지 대상) — 순수."""
    return tr_cd.startswith(_NO_RETRY_TR_PREFIXES)


class RestResponse(BaseModel):
    """TR 응답(상태코드 + 본문). 본문은 ``rsp_cd``·``{TR}OutBlockN`` — TR별로 다양."""

    status_code: int
    body: dict[str, Any] = {}


class TrRequester(Protocol):
    """TR 요청 계약 — LSApiGateway가 계좌별로 하나씩 든다. xingAPI(`XingQueryClient`,
    DESIGN-ls-xing.md §2)가 구현: ``{TR}InBlock`` 본문 → ``rsp_cd``·``{TR}OutBlockN``이 든
    RestResponse. 게이트웨이의 본문 만들기·응답 파싱은 전송과 무관."""

    async def request(
        self, tr_cd: str, body: dict[str, Any] | None = None, *,
        path: str = "/", method: str = "POST", tr_cont: str = "N",
    ) -> RestResponse: ...
