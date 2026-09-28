"""xingAPI 게이트웨이 — LSApiGateway의 전송 단(REST 클라이언트)만 XAQuery로 바꾼다
(DESIGN-ls-xing.md §2.2).

- ``XingQueryClient``: ``LSRestClient``와 같은 ``request(tr_cd, body, path=…) → RestResponse``
  계약(``TrRequester``). 본문 ``{"<TR>InBlock": {...}}``을 XAQuery 필드로 채우고, OutBlock을 REST
  응답과 같은 모양의 dict로 돌려준다. 주문 TR은 주문 차선(직렬, 조회 뒤에 줄 서지 않음).
- ``XingGateway(LSApiGateway)``: 계좌별 클라이언트만 XingQueryClient — 본문 만들기·응답 파싱·주문
  문맥·계좌 라우팅은 **상속 그대로**. 기존 테스트(test_ls_order/account)가 그 부분을 보증한다.
- 주문 TR 응답 없음(XingTimeout)은 ``RestTimeoutError``(결과 모름, 재전송 금지) — REST와 같은 뜻.
- 초당 한도는 xing이 알려 주는 값(GetTRCountPerSec/Limit/Request)으로 판단 — 표를 코드에 두지
  않는다.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..config import LSAccounts
from ..domain.enums import Account, Underlying
from .ls import _LS_ACCOUNTS, LSApiGateway
from .ls_rest import (
    RateLimitError,
    RestError,
    RestResponse,
    RestTimeoutError,
    TrRequester,
    is_order_tr,
    mask_secrets,
)
from .xing_com import LANE_ORDER, LANE_QUERY, XingSession, XingTimeout
from .xing_res import ResError, load_res

_log = logging.getLogger("kp_arb.xing")


def in_block_name(tr_cd: str, body: Mapping[str, Any] | None,
                  res_dir: Path | str | None) -> str:
    """REST 본문이 블록으로 안 싸여 있을 때(flat ``{AcntNo, Pwd}`` — CSPAQ22200 등) 넣을 InBlock
    이름. Res가 있으면 첫 입력 블록 이름, 없으면 관례(C계열 ``InBlock1``, t계열 ``InBlock``).
    순수."""
    if res_dir is not None:
        try:
            spec = load_res(res_dir, tr_cd)
            if spec.in_blocks:
                return spec.in_blocks[0].name
        except ResError:
            pass
    return f"{tr_cd}InBlock1" if tr_cd[:1].isupper() else f"{tr_cd}InBlock"


# REST 본문 필드명 ↔ xing Res 필드명이 다른 것(실측 2026-09-23, C:\meme\Res): 선물 주문 TR
# (CFOAT00100/00200/00300)의 비밀번호는 REST가 InptPwd, Res는 Pwd. Res에 없는 이름이면 이 표로
# 바꾼다.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {"InptPwd": ("Pwd",), "Pwd": ("InptPwd",)}
# REST TR 코드 ↔ xing TR 코드(실측 2026-09-23): 주식 주문은 OpenAPI가 CSPAT00601/00701/00801,
# xing Res는 CSPAT00600/00700/00800(같은 InBlock1 필드 — MbrNo·MgntrnCode·LoanDt 포함). 요청 블록·
# 응답 블록 이름의 접두도 같이 바꿔 게이트웨이(REST 이름으로 본문을 만들고 응답을 읽음)는 모르게
# 한다.
TR_ALIASES: dict[str, str] = {
    "CSPAT00601": "CSPAT00600", "CSPAT00701": "CSPAT00700", "CSPAT00801": "CSPAT00800"}


def rename_tr_prefix(blocks: Mapping[str, Any], src: str, dst: str) -> dict[str, Any]:
    """블록 이름의 TR 접두를 바꾼다(``CSPAT00601InBlock1`` → ``CSPAT00600InBlock1``). 순수."""
    return {(dst + k[len(src):] if k.startswith(src) else k): v for k, v in blocks.items()}


def to_blocks(tr_cd: str, body: Mapping[str, Any] | None,
              res_dir: Path | str | None = None) -> dict[str, Any]:
    """REST 본문 → XAQuery 블록 dict. 이미 ``…InBlock`` 키로 싸여 있으면 그대로. Res가 있으면
    블록마다 Res에 없는 필드명을 별칭(FIELD_ALIASES)으로 바꾸고, 그래도 없으면 뺀다(XAQuery가
    모르는 필드는 거부되므로) — 뺀 필드는 경고 로그. 순수(파일 읽기 제외)."""
    if not body:
        return {}
    blocks = (dict(body) if any("InBlock" in str(k) for k in body)
              else {in_block_name(tr_cd, body, res_dir): dict(body)})
    if res_dir is None:
        return blocks
    try:
        spec = load_res(res_dir, tr_cd)
    except ResError:
        return blocks
    out: dict[str, Any] = {}
    for name, rows in blocks.items():
        block = spec.blocks.get(name)
        if block is None:
            out[name] = rows
            continue
        known = set(block.fields)
        rows_list = rows if isinstance(rows, list) else [rows]
        fixed = [_alias_row(tr_cd, name, r, known, block.fields) for r in rows_list]
        out[name] = fixed if isinstance(rows, list) else fixed[0]
    return out


# Res InBlock에는 있는데 본문이 안 준 필드의 기본값 — REST 서버는 비워도 채워 주지만 xing은
# 고정길이 레코드를 그대로 보내 실서버가 09604 "입력 데이터 포맷이 맞지않습니다"로 거부(운영 PC 실측
# 2026-09-29 08:42 CSPAQ22200). 값은 LS OpenAPI 포털의 TR별 요청 예시(RecCnt 1, 구분코드 "0" =
# 전체/기본). "*"는 모든 TR 공통. 본문이 준 값이 우선.
INPUT_DEFAULTS: dict[str, dict[str, str]] = {
    "*": {"RecCnt": "1"},
    "CSPAQ22200": {"MgmtBrnNo": "", "BalCreTp": "0"},
    "CSPAQ12300": {"BalCreTp": "0", "CmsnAppTpCode": "0", "D2balBaseQryTp": "0",
                   "UprcTpCode": "0"},
}


def input_defaults(tr_cd: str, block_fields: Sequence[str],
                   row: Mapping[str, Any]) -> dict[str, Any]:
    """row에 없고 Res 블록엔 있는 필드에 INPUT_DEFAULTS를 채운 새 dict. 순수."""
    out = dict(row)
    defaults = {**INPUT_DEFAULTS.get("*", {}), **INPUT_DEFAULTS.get(tr_cd, {})}
    for name in block_fields:
        if name not in out and name in defaults:
            out[name] = defaults[name]
    return out


def _alias_row(tr_cd: str, block: str, row: Mapping[str, Any], known: set[str],
               fields: Sequence[str] = ()) -> dict[str, Any]:
    fixed: dict[str, Any] = {}
    for key, value in row.items():
        if key in known:
            fixed[key] = value
            continue
        alias = next((a for a in FIELD_ALIASES.get(key, ()) if a in known), None)
        if alias is not None:
            fixed[alias] = value
        else:
            _log.warning("xing %s %s: Res에 없는 필드 %s 뺌", tr_cd, block, key)
    return input_defaults(tr_cd, fields, fixed)


class XingQueryClient:
    """계좌 하나 몫의 TR 요청 클라이언트(TrRequester) — 실제 전송은 공유 XingSession."""

    def __init__(self, session: XingSession, *, res_dir: Path | str | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._session = session
        self._res_dir = res_dir
        self._now = now
        self._sent: dict[str, deque[float]] = {}  # TR별 최근 전송 시각(초당 한도 판정)

    async def _check_limit(self, tr_cd: str) -> None:
        """xing이 알려 주는 한도로 판정 — 초당 건수(우리가 센 창) + 누적 한도(xing 카운터)."""
        per_sec, _base, limit, requested = await self._session.limits(tr_cd)
        if limit > 0 and requested >= limit:
            raise RateLimitError(f"{tr_cd} xing 요청 한도 {requested}/{limit}")
        if per_sec > 0:
            now = self._now()
            window = self._sent.setdefault(tr_cd, deque())
            while window and now - window[0] >= 1.0:
                window.popleft()
            if len(window) >= per_sec:
                raise RateLimitError(f"{tr_cd} 초당 한도 {per_sec} 초과")
            window.append(now)

    async def request(
        self, tr_cd: str, body: dict[str, Any] | None = None, *,
        path: str = "/", method: str = "POST", tr_cont: str = "N",
    ) -> RestResponse:
        xing_tr = TR_ALIASES.get(tr_cd, tr_cd)
        await self._check_limit(xing_tr)
        lane = LANE_ORDER if is_order_tr(tr_cd) else LANE_QUERY
        blocks = to_blocks(xing_tr, rename_tr_prefix(body or {}, tr_cd, xing_tr), self._res_dir)
        try:
            result = await self._session.query(xing_tr, blocks, lane=lane, next_=tr_cont == "Y")
        except XingTimeout as exc:
            if is_order_tr(tr_cd):
                # 응답 없음은 거부가 아니다 — 접수됐을 수 있어 재전송 금지(CLAUDE.md §7)
                _log.warning("xing %s 응답 없음 — 재전송 안 함, 주문이 접수됐을 수 있음: %s | "
                             "보낸본문=%s", tr_cd, exc, mask_secrets(body))
                raise RestTimeoutError(f"xing {tr_cd} 응답 없음: {exc}") from exc
            raise RestError(f"xing {tr_cd} 응답 없음: {exc}") from exc
        if result.rsp_cd and not result.rsp_cd.startswith("00"):
            _log.warning("xing %s 거부(rsp_cd=%s): %s | 보낸본문=%s", tr_cd, result.rsp_cd,
                         result.rsp_msg, mask_secrets(body))
        out = rename_tr_prefix(result.as_body(), xing_tr, tr_cd)  # 응답 블록도 REST 이름으로
        return RestResponse(status_code=200, body=out)


class XingGateway(LSApiGateway):
    """LS 접근을 xingAPI로 — 공개 메서드는 LSApiGateway 상속."""

    @classmethod
    def from_session(
        cls, session: XingSession, accounts: LSAccounts, *,
        res_dir: Path | str | None = None,
        futures_symbols: Mapping[Underlying, str] | None = None,
        etf_symbols: Mapping[Underlying, str] | None = None,
        next_futures_symbols: Mapping[Underlying, str] | None = None,
    ) -> XingGateway:
        clients: dict[Account, TrRequester] = {}
        load = list(_LS_ACCOUNTS)
        if accounts.has(Account.KR_FX):
            load.append(Account.KR_FX)
        for account in load:
            clients[account] = XingQueryClient(session, res_dir=res_dir)
        return cls(clients, accounts=accounts, futures_symbols=futures_symbols,
                   etf_symbols=etf_symbols, next_futures_symbols=next_futures_symbols)
