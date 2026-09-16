"""LS Open API REST 공통 계층 (DESIGN.md §5.1).

라이브 네트워크 없음: 실제 HTTP는 주입된 ``RestTransport``(Protocol) 뒤로 격리한다.
공통 책임:
- ``base_url`` + Bearer 토큰 주입(``TokenManager``) + ``tr_cd`` 헤더 구성.
- 레이트리밋 가드: TR별 초당 한도(LS 공식 표 ``LS_PER_SECOND``) 초과 시 ``RateLimitError``.
  일 호출수는 참고값(5,000)을 넘어도 막지 않고 경고만(2026-09-14 정정).
- 지수 백오프 재시도(전송 예외/5xx). 소진 시 ``RestError``.

실제 전송(aiohttp)은 ``ls_http``, TR별 path는 ``ls.LSApiGateway``.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel

from .ls_auth import TokenManager

DEFAULT_DAILY_CAP = 5_000  # 참고값(출처 없음) — 막지 않고 경고만(RateLimiter 참고)
DEFAULT_PER_SECOND = 2  # 표에 없는 TR의 기본 초당 한도(보수적). 쓰는 TR은 전부 표에 넣는다.
# TR별 초당 한도 — LS OpenAPI 공식 값 그대로. 출처: openapi.ls-sec.co.kr 서비스 목록
# (`/api/apis/public/api-list/<그룹id>` 응답의 extraParam.ThroughputQuotaRule[].requestLimit,
# 주식 그룹 73142d9f…, 선물/옵션 그룹 2f1eea77…; 2026-09-03 수집 → 2026-09-14 전 TR 재대조).
# 실측 2026-09-08: 기본값 2를 전 TR에 적용해 선물 취소(공식 10)를 우리 쪽에서 막았고,
# 그 사이 종료 취소가 실패해 선주문이 LS에 남았다. 재대조 2026-09-14: t0441·t0434는 공식 1인데
# 2로 적혀 있었음(정정), t2111(원달러선물 현재가)은 표에 없어 기본 2로 막히고 있었음(공식 10).
LS_PER_SECOND: dict[str, int] = {
    "CFOAT00100": 10, "CFOAT00200": 10, "CFOAT00300": 10,  # 선물옵션 주문·정정·취소
    "CSPAT00601": 10, "CSPAT00701": 3, "CSPAT00801": 3,    # 현물 주문·정정·취소
    "t1102": 10, "t8402": 10, "t2111": 10,                 # 주식·주식선물·선물옵션(원달러) 현재가
    "t8401": 2, "t0441": 1, "t0434": 1,                    # 주식선물 마스터·선물 잔고·선물 미체결
    "CFOBQ10500": 1, "CSPAQ12300": 1, "CSPAQ13700": 1, "CSPAQ22200": 1,  # 증거금·예수금·체결
    "t1901": 1, "t8426": 1,                                # ETF 현재가·상품선물 마스터
}

_log = logging.getLogger(__name__)

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
    """레이트리밋(일 한도 또는 TR별 초당 한도) 초과."""


class RestError(RuntimeError):
    """REST 호출 실패(재시도 소진 포함)."""


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
    """TR별 초당 한도 가드 + 일 호출수 감시. 주입형 시계(epoch 초)로 판정.

    초당 한도 초과 시 대기하지 않고 ``RateLimitError``를 던진다(호출자가 페이싱).
    **일 한도(daily_cap)는 막지 않고 경고만** — 운영 실측 2026-09-14 13:11: 초기 골격의 가정값
    5,000(LS 공식 안내엔 초당 한도뿐)에 하이닉스 자동M의 재발주 왕복이 닿자 그날 남은 시간 동안
    선물 계좌의 **취소까지 전부 차단**돼 걸린 선주문(#8903)을 못 지웠다. 자체 가드가 취소를 막는
    쪽이 더 위험하므로 넘어가면 로그로만 알린다(넘긴 뒤 1,000건마다 한 번 더). 진짜 한도는 LS가
    rsp_cd로 거부하고, 그 거부는 request()가 남긴다.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float],
        daily_cap: int = DEFAULT_DAILY_CAP,
        default_per_second: int = DEFAULT_PER_SECOND,
        per_tr_per_second: dict[str, int] | None = None,
        today: Callable[[], str] | None = None,
    ) -> None:
        self._now = now
        self._daily_cap = daily_cap
        self._default_per_second = default_per_second
        self._per_tr = dict(per_tr_per_second or {})
        # 일 카운트의 날짜 경계는 **실제 달력 날짜**(로컬) — ``now``는 초당 판정용 단조시계라
        # (운영은 time.monotonic) 거기서 나눈 "날"은 자정과 무관한 임의 경계였다(정정 2026-09-14).
        self._today: Callable[[], str] = today or (lambda: date.today().isoformat())
        self._day = ""
        self._daily_count = 0
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def check(self, tr_cd: str) -> None:
        """tr_cd 호출 1건을 허용 가능한지 판정하고, 가능하면 카운트에 반영."""
        t = self._now()

        day = self._today()
        if day != self._day:
            self._day = day
            self._daily_count = 0
        if self._daily_count >= self._daily_cap and (
                self._daily_count - self._daily_cap) % 1_000 == 0:
            _log.warning("LS REST 일 호출수 %d — 참고 한도 %d 초과(차단 안 함, %s)",
                         self._daily_count, self._daily_cap, tr_cd)

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
        # 주문 TR은 1회만(재전송 금지, 2026-09-16) — 나머지(조회)는 지수 백오프 재시도
        tries = 1 if is_order_tr(tr_cd) else self._max_retries
        for attempt in range(1, tries + 1):
            try:
                resp = await self._transport.request(method, url, headers, body)
            except Exception as exc:  # 전송 계층의 임의 실패(연결·시간 초과)
                last_exc = exc
                if is_order_tr(tr_cd):
                    _log.warning("LS %s 응답 없음(전송 실패·시간 초과) — 재전송 안 함, 주문이 "
                                 "접수됐을 수 있음: %s | 보낸본문=%s", tr_cd, exc,
                                 mask_secrets(body))
                    raise RestTimeoutError(f"REST {tr_cd} 응답 없음: {exc}") from exc
                # 실증 2026-09-16: 재시도가 조용히 지나가 로그로 알 수 없었다 → 매번 남긴다
                _log.warning("LS %s 전송 실패 %d/%d — 재시도: %s", tr_cd, attempt, tries, exc)
            else:
                if resp.status_code < 500:
                    # 거부 응답(rsp_cd가 "00"으로 시작 안 함)이면 보낸 본문을 남긴다
                    # — 계좌번호·비번(마스킹)을 눈으로 대조하려는 목적.
                    rsp_cd = resp.body.get("rsp_cd")
                    if rsp_cd is not None and not str(rsp_cd).startswith("00"):
                        _log.warning(
                            "LS %s 거부(rsp_cd=%s): %s | 보낸본문=%s",
                            tr_cd, rsp_cd, resp.body.get("rsp_msg"),
                            mask_secrets(body),
                        )
                    return resp
                last_exc = RestError(f"server error {resp.status_code} for {tr_cd}")

            if attempt < tries:
                await self._sleep_backoff(attempt)

        raise RestError(
            f"REST {tr_cd} failed after {tries} attempts"
        ) from last_exc

    async def _sleep_backoff(self, attempt: int) -> None:
        if self._backoff_base_s > 0:
            await asyncio.sleep(self._backoff_base_s * (2 ** (attempt - 1)))
