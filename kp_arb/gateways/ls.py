"""LS Open API 게이트웨이 — 현물 주문 (DESIGN.md §5.1).

블록 1-3 범위: 현물(주식/ETF) 주문/정정/취소를 REST(CSPAT006/007/008 계열)로.
- 계좌 라우팅은 ``routing.account_for``로 결정한다(불변식, 깨지 않음).
- 선물 주문 TR은 ``[OPEN §13 #3]`` 미정 → 추측하지 않고 ``NotImplementedError``로 가드.
- 잔고/포지션 조회는 블록 1-5에서 채운다(여기선 미구현 가드).
- 실제 계좌상품코드(계좌번호) 매핑은 config ``[OPEN §13 #3]``. 여기선 Account enum으로만 라우팅.

라이브 없음: 실제 전송은 ``LSRestClient`` → ``RestTransport``(Protocol) 뒤로 격리.
테스트는 녹화 픽스처만 사용.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .. import order_log
from ..config import LSAccounts
from ..domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from ..domain.models import OrderIntent, Position
from ..etf_theory import EtfTheoryInputs
from ..order_book import OrderStatus, TrackedOrder
from ..routing import account_for
from .base import LSGateway, placed_at_from_hhmmss, placed_epoch_from_hhmmss
from .ls_auth import TokenManager, TokenTransport
from .ls_rest import (
    LS_PER_SECOND,
    LSRestClient,
    RateLimiter,
    RateLimitError,
    RestError,
    RestResponse,
    RestTransport,
)

LIVE_BASE_URL = "https://openapi.ls-sec.co.kr:8080"
_LS_ACCOUNTS: tuple[Account, ...] = (Account.KR_STOCK, Account.KR_DERIV)


class OrderGoneError(RestError):
    """정정/취소할 잔량이 없음(이미 체결·취소된 주문). 오류가 아니라 정상 흐름의 거부 —
    체결과의 경합에서 종종 발생하므로 호출부는 체결 확인으로 이어가면 된다."""


# "잔량 없음" 거부 코드 — 취소·정정 대상이 이미 체결/취소된 경우(정상 흐름의 경합).
#   01433 "모의투자 정정/취소할 수량이 없습니다" (모의 실측 v6.15)
#   03416 "정정취소가능수량이 없습니다" (운영 실측 2026-09-14: 발주 55ms 뒤 체결과 취소가 교차,
#         실행 끔의 강제 취소가 앞선 취소와 겹침 — 하루 4건, 매번 3회 재시도로 경고 20줄)
_ORDER_GONE_RSP_CDS = frozenset({"01433", "03416"})

# 모의 미제공 TR 거부 코드 (실측 01900: CFOAQ50600 선물잔고) — 조회는 빈 결과로 대체.
_PAPER_UNSUPPORTED_RSP_CDS = frozenset({"01900"})


@dataclass
class OrderContext:
    """원주문 컨텍스트. 정정/취소가 원주문 정보(계좌·종목·요청본문)를 참조하도록 보존."""

    order_id: str
    intent: OrderIntent
    account: Account
    request_body: dict[str, Any]
    replaces: str | None = None  # 정정으로 생성된 주문이면 원주문 id


class LSApiGateway(LSGateway):
    """LS REST 기반 현물 주문 게이트웨이. ``LSRestClient`` 위에 TR 매핑을 얹는다."""

    SPOT_ORDER_TR = "CSPAT00601"
    SPOT_AMEND_TR = "CSPAT00701"
    SPOT_CANCEL_TR = "CSPAT00801"
    SPOT_PATH = "/stock/order"

    # 선물옵션 주문 TR (LS: 정상/정정/취소, POST /futureoption/order).
    FUTURE_ORDER_TR = "CFOAT00100"
    FUTURE_AMEND_TR = "CFOAT00200"
    FUTURE_CANCEL_TR = "CFOAT00300"
    FUTURE_PATH = "/futureoption/order"

    # 잔고·예수금·증거금 조회 (계좌별). 정확한 TR 필드명은 라이브 구현 시 확인.
    STOCK_DEPOSIT_TR = "CSPAQ22200"     # 주식 예수금 (대안 주문가능 CDPCQ04700)
    STOCK_POSITIONS_TR = "CSPAQ12300"   # 주식 잔고 (대안 t0424)
    STOCK_OPEN_ORDERS_TR = "CSPAQ13700" # 주식 체결/미체결 (InBlock1 래핑, 실측 v6.5)
    DERIV_DEPOSIT_TR = "CFOBQ10500"     # 선물옵션 예탁금·증거금 (get_balance)
    DERIV_POSITIONS_TR = "t0441"        # 선물옵션 잔고평가 (운영 실측 — CFOAQ50600은 거부)
    DERIV_OPEN_ORDERS_TR = "t0434"      # 선물옵션 체결/미체결 (문서 확인·실측 대기 2026-09-09)
    STOCK_ACC_PATH = "/stock/accno"
    DERIV_ACC_PATH = "/futureoption/accno"
    FUTURES_MASTER_TR = "t8401"         # 주식선물 마스터 (종목코드 조회, 실측 v6.7)
    FUTURES_MARKET_PATH = "/futureoption/market-data"

    _SPOT: frozenset[Instrument] = frozenset({Instrument.KR_STOCK, Instrument.KR_ETF})

    def __init__(
        self,
        rest_by_account: Mapping[Account, LSRestClient],
        *,
        accounts: LSAccounts | None = None,
        futures_symbols: Mapping[Underlying, str] | None = None,
        etf_symbols: Mapping[Underlying, str] | None = None,
        next_futures_symbols: Mapping[Underlying, str] | None = None,
    ) -> None:
        self._rest_by_account = dict(rest_by_account)
        self._accounts = accounts
        # 주식선물 코드: (종목, 근|차근) → 코드 (§5.11). futures_symbols=근월물, next_=차근월물.
        self._futures_codes: dict[tuple[Underlying, Instrument], str] = {}
        for u, code in (futures_symbols or {}).items():
            self._futures_codes[(u, Instrument.KR_STOCK_FUTURE)] = code
        for u, code in (next_futures_symbols or {}).items():
            self._futures_codes[(u, Instrument.KR_STOCK_FUTURE_NEXT)] = code
        self._futures_key = {v: k for k, v in self._futures_codes.items()}  # 코드 → (종목, 상품)
        # 단일종목 레버리지 ETF 코드(config.yaml에서 주입). 없으면 ETF 미취급.
        self._etf_symbols: dict[Underlying, str] = dict(etf_symbols or {})
        self._etf_underlying = {v: k for k, v in self._etf_symbols.items()}
        self._orders: dict[str, OrderContext] = {}
        # 선물 미체결 조회(t0434)가 실제로 성공한 적이 있는가 — 성공 전엔 재동기 유령 정리 대상에서
        # 제외(빈 결과를 "조회 성공"으로 봐 걸린 선주문을 지운 사고, 실측 2026-09-09 #20851).
        self._deriv_open_orders_ok = False
        self.connected = False

    @classmethod
    def from_accounts(
        cls,
        accounts: LSAccounts,
        *,
        token_transport: TokenTransport,
        rest_transport: RestTransport,
        base_url: str = LIVE_BASE_URL,
        futures_symbols: Mapping[Underlying, str] | None = None,
        etf_symbols: Mapping[Underlying, str] | None = None,
        next_futures_symbols: Mapping[Underlying, str] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> LSApiGateway:
        """계좌별 키로 계좌별 토큰·REST 클라이언트를 조립. (레이트리밋은 계좌별 독립.)"""
        rest_by_account: dict[Account, LSRestClient] = {}
        load = list(_LS_ACCOUNTS)
        if accounts.has(Account.KR_FX):  # 원달러선물 헤지 계좌(선택 §9.1) — 있으면 로드
            load.append(Account.KR_FX)
        for account in load:
            cred = accounts.for_account(account)
            tokens = TokenManager(cred.appkey, cred.appsecret, token_transport, now=now)
            limiter = RateLimiter(now=now, per_tr_per_second=LS_PER_SECOND)  # 공식 TR별 한도
            # 재시도에 지수 백오프 — LS 서버가 가끔 뱉는 일시적 500을 텀 없는 3연속
            # 재시도로는 못 벗어난다. 0.3s→0.6s 간격을 줘 순간 500이 지나가면 회복.
            rest_by_account[account] = LSRestClient(
                base_url, tokens, rest_transport, limiter, backoff_base_s=0.3)
        return cls(rest_by_account, accounts=accounts,
                   futures_symbols=futures_symbols, etf_symbols=etf_symbols,
                   next_futures_symbols=next_futures_symbols)

    def _rest_for(self, account: Account) -> LSRestClient:
        return self._rest_by_account[account]

    def _account_fields(self, account: Account) -> dict[str, Any]:
        """조회 요청용 계좌 필드(실측: AcntNo+Pwd). 실 계좌번호·비번은 env(LSAccounts)."""
        if self._accounts is None:
            return {"account": account.value}  # 플레이스홀더(테스트/드라이런)
        acct = self._accounts.for_account(account)
        return {"AcntNo": acct.number.replace("-", ""), "Pwd": acct.password}

    def _order_account_fields(self, account: Account) -> dict[str, Any]:
        """주문 요청용 계좌 필드(실측: 주문 InBlock은 Pwd가 아니라 InptPwd)."""
        if self._accounts is None:
            return {"account": account.value}  # 플레이스홀더(테스트/드라이런)
        acct = self._accounts.for_account(account)
        return {"AcntNo": acct.number.replace("-", ""), "InptPwd": acct.password}

    async def connect(self) -> None:
        # 토큰은 LSRestClient.request 시점에 lazy 발급. 여기선 연결 플래그만.
        self.connected = True

    async def place_order(self, intent: OrderIntent) -> str:
        if intent.venue is not Venue.LS:
            raise ValueError("LSApiGateway only handles LS orders")
        account = account_for(intent.instrument)  # 라우팅 계약(불변식)
        if intent.instrument in self._SPOT:
            tr_cd, path = self.SPOT_ORDER_TR, self.SPOT_PATH
            body = self._spot_order_body(intent, account)
        elif intent.instrument.is_stock_future:  # 근·차근 — 코드만 다르고 TR 동일
            tr_cd, path = self.FUTURE_ORDER_TR, self.FUTURE_PATH
            body = self._future_order_body(intent, account)
        else:
            raise NotImplementedError(f"{intent.instrument} 주문 TR 미정 [OPEN §13 #3]")
        order_log.order_requested(intent)  # 보내기 직전(응답 전) — 단계 추적
        try:
            resp = await self._rest_for(account).request(tr_cd, body, path=path)
            order_id = self._parse_order_id(resp, tr_cd)
        except Exception as exc:  # 거부·오류도 거래소별 파일에 남긴다(발주거부)
            order_log.order_rejected(intent, exc)
            raise
        self._orders[order_id] = OrderContext(order_id, intent, account, body)
        order_log.order_placed(intent, order_id, getattr(resp, "body", None))
        return order_id

    async def place_fx_futures(self, code: str, side: Side, qty: int,
                               price: float) -> str:
        """원달러선물 지정가 발주 (KR_FX 계좌, CFOAT00100). 종목코드는 화면 콤보(근/차근).

        3주식 Underlying 모델 밖의 §9.1 헤지 전용 경로 — OrderBook/OrderIntent를 안 거친다.
        """
        account = Account.KR_FX
        if account not in self._rest_by_account:
            raise RestError("KR_FX(원달러선물 헤지) 계좌 미등록 — 자격 확인 필요")
        body = self._fx_order_body(code, side, qty, price, account)
        _fxlog = order_log.logger_for(Venue.LS)
        _fxlog.info(  # 발주요청 — 보내기 직전(응답 전). OrderIntent 밖이라 직접 남긴다.
            "발주요청 [동시호가] 원달러선물 %s %s %d @ %s", code, side.value, qty, price)
        resp = await self._rest_for(account).request(
            self.FUTURE_ORDER_TR, body, path=self.FUTURE_PATH)
        order_id = self._parse_order_id(resp, self.FUTURE_ORDER_TR)
        _fxlog.info(
            "발주 [동시호가] 원달러선물 %s %s %d @ %s → #%s | acct=%s resp=%r",  # 응답 원문
            code, side.value, qty, price, order_id,
            self._order_account_fields(account).get("AcntNo", "?"),
            getattr(resp, "body", None))
        return order_id

    async def amend_order(
        self,
        order_id: str,
        *,
        qty: float | None = None,
        price: float | None = None,
    ) -> str:
        """원주문을 정정. 원주문 컨텍스트(계좌·종목)를 참조해 새 주문 id 반환."""
        ctx = self._require(order_id)
        if ctx.intent.instrument in self._SPOT:
            tr_cd, path = self.SPOT_AMEND_TR, self.SPOT_PATH
            body = self._amend_body(ctx, qty, price)
        else:
            tr_cd, path = self.FUTURE_AMEND_TR, self.FUTURE_PATH
            body = self._future_amend_body(ctx, qty, price)
        resp = await self._rest_for(ctx.account).request(tr_cd, body, path=path)
        new_id = self._parse_order_id(resp, tr_cd)
        self._orders[new_id] = OrderContext(
            new_id, ctx.intent, ctx.account, body, replaces=order_id
        )
        order_log.order_amended(Venue.LS, order_id, new_id, qty, price)
        return new_id

    def open_orders_supported(self, account: Account) -> bool:
        """주식 계좌는 항상 실제 조회(CSPAQ13700). 선물 계좌는 t0434가 **실제로 성공한 뒤**에만
        참 — 모의 미제공(01900)·형식 거부면 빈 결과라 유령 정리에 쓰면 안 된다."""
        if account is Account.KR_STOCK:
            return True
        return account is Account.KR_DERIV and self._deriv_open_orders_ok

    async def cancel_order(self, order_id: str, qty: float | None = None) -> None:
        ctx = self._require(order_id)
        if ctx.intent.instrument in self._SPOT:
            tr_cd, path = self.SPOT_CANCEL_TR, self.SPOT_PATH
            body = self._cancel_body(ctx, qty)
        else:
            tr_cd, path = self.FUTURE_CANCEL_TR, self.FUTURE_PATH
            body = self._future_cancel_body(ctx, qty)
        resp = await self._rest_for(ctx.account).request(tr_cd, body, path=path)
        self._check_ok(resp, tr_cd)
        order_log.order_canceled(Venue.LS, order_id)

    async def get_positions(self, account: Account) -> Sequence[Position]:
        """계좌별 잔고(포지션) 조회. 주식 CSPAQ12300 / 선물 t0441(운영 실측)."""
        if account is Account.KR_STOCK:
            resp = await self._rest_for(account).request(
                self.STOCK_POSITIONS_TR, self._account_fields(account), path=self.STOCK_ACC_PATH
            )
            rows = self._rows(resp, self.STOCK_POSITIONS_TR)
            return [p for r in rows if (p := self._stock_position(r)) is not None]
        # 운영 실측: CFOAQ50600은 형식을 맞춰도 거부(09604/08001) → t0441 사용.
        # 공식 초당 1회(재대조 2026-09-14) — 재동기가 겹치면 한 박자 쉬고 재시도(_request_paced).
        fields = self._account_fields(account)
        resp = await self._request_paced(
            account,
            self.DERIV_POSITIONS_TR,
            {f"{self.DERIV_POSITIONS_TR}InBlock": {
                "accno": fields.get("AcntNo", ""), "passwd": fields.get("Pwd", ""),
            }},
            self.DERIV_ACC_PATH,
        )
        if str(resp.body.get("rsp_cd", "")) in _PAPER_UNSUPPORTED_RSP_CDS:
            return []  # 모의 미제공이면 빈 결과
        self._check_ok(resp, self.DERIV_POSITIONS_TR)
        raw_rows = resp.body.get(f"{self.DERIV_POSITIONS_TR}OutBlock1")
        rows = list(raw_rows) if isinstance(raw_rows, list) else []
        return [p for r in rows if (p := self._deriv_position(r)) is not None]

    async def get_balance(self, account: Account) -> float:
        """계좌별 가용자금(현금주문가능). 주식 CSPAQ22200 / 선물 CFOBQ10500."""
        if account is Account.KR_STOCK:
            resp = await self._rest_for(account).request(
                self.STOCK_DEPOSIT_TR, self._account_fields(account), path=self.STOCK_ACC_PATH
            )
            return self._amount(resp, self.STOCK_DEPOSIT_TR, "MnyOrdAbleAmt")  # 현금주문가능
        resp = await self._rest_for(account).request(
            self.DERIV_DEPOSIT_TR, self._account_fields(account), path=self.DERIV_ACC_PATH
        )
        return self._amount(resp, self.DERIV_DEPOSIT_TR, "MnyOrdAbleAmt")

    async def get_open_orders(self, account: Account) -> Sequence[TrackedOrder]:
        """미체결 주문 스냅샷 — 주식 CSPAQ13700 / 선물 t0434(취급 종목코드마다 1회)."""
        if account is Account.KR_DERIV:
            return await self._deriv_open_orders()
        if account is not Account.KR_STOCK:
            return []
        body = {
            f"{self.STOCK_OPEN_ORDERS_TR}InBlock1": {
                **self._order_account_fields(account),  # 실측: 13700도 InptPwd 스타일
                "OrdMktCode": "00",
                "BnsTpCode": "0",   # 전체
                "IsuNo": "",
                "ExecYn": "2",      # 미체결
                "OrdDt": "",
                "SrtOrdNo2": 999_999_999,
                "BkseqTpCode": "0",
                "OrdPtnCode": "00",
            }
        }
        resp = await self._rest_for(account).request(
            self.STOCK_OPEN_ORDERS_TR, body, path=self.STOCK_ACC_PATH
        )
        rows = self._rows(resp, self.STOCK_OPEN_ORDERS_TR)
        return [
            self._adopt(o) for row in rows
            if float(row.get("MrcAbleQty", 0)) > 0
            and (o := self._open_order(row)) is not None
        ]

    def _adopt(self, order: TrackedOrder) -> TrackedOrder:
        """스냅샷으로 알게 된 미체결 주문의 취소·정정 문맥을 만들어 둔다(2026-09-09).

        주문 문맥(_orders)은 메모리라 코어 재시동 전에 낸 주문은 "unknown order"로 취소가 안 됐다.
        취소·정정 본문에 필요한 것은 계좌·종목(의도)·주문번호뿐이라 스냅샷 행으로 충분하다.
        이미 아는 주문(이 프로세스가 낸 것)은 원래 문맥을 유지한다.
        """
        if order.order_id not in self._orders:
            account = order.intent.account or account_for(order.intent.instrument)
            self._orders[order.order_id] = OrderContext(
                order.order_id, order.intent, account, request_body={})
        return order

    def _open_order(self, row: dict[str, Any]) -> TrackedOrder | None:
        # 실측 행: IsuNo "A005930", BnsTpCode 1매도/2매수, OrdPrc 문자열, ExecQty 체결누계.
        resolved = self._resolve_spot(str(row["IsuNo"]))
        if resolved is None:
            return None  # 취급 외 종목(실계좌의 다른 주문 등) — 추적 대상 아님
        instrument, underlying = resolved
        intent = OrderIntent(
            venue=Venue.LS,
            underlying=underlying,
            instrument=instrument,
            side=Side.BUY if str(row["BnsTpCode"]) == "2" else Side.SELL,
            qty=float(row["OrdQty"]),
            order_type=(
                OrderType.LIMIT if str(row.get("OrdprcPtnCode", "00")) == "00"
                else OrderType.MARKET
            ),
            price=float(row["OrdPrc"]) if float(row["OrdPrc"]) > 0 else None,
        )
        exec_qty = float(row.get("ExecQty", 0))
        return TrackedOrder(
            order_id=str(row["OrdNo"]),
            intent=intent,
            status=OrderStatus.PARTIAL if exec_qty > 0 else OrderStatus.ACCEPTED,
            filled_qty=exec_qty,
            avg_fill_price=float(row.get("ExecPrc", 0) or 0),
            # 접수시각 — OutBlock3 OrdTime(HHMMSSmmm, 공식 문서). 없으면 빈 칸(실측 확인 대기).
            placed_at=placed_at_from_hhmmss(row.get("OrdTime")),
            placed_epoch=placed_epoch_from_hhmmss(row.get("OrdTime")),  # 정렬용(오늘 날짜)
        )

    async def _deriv_open_orders(self) -> list[TrackedOrder]:
        """선물 미체결(t0434, 공식 문서 확인 2026-09-09) — 취급 중인 선물 코드(근·차근)마다
        미체결(chegb "2")을 받고, 안전하게 잔량(ordrem) > 0만 남긴다.
        요청 InBlock: expcode(8)·chegb(0전체/1체결/2미체결)·sortgb(1역순/2순)·cts_ordno(처음 Space).
        응답 OutBlock1: ordno·orgordno·medosu·ordgb·qty·price(9.2)·cheqty·cheprice·ordrem·status·
        ordtime·expcode·hogatype. 연속조회는 응답 헤더 tr_cont_key가 필요한데 RestResponse가 헤더를
        안 담아 첫 페이지만(초과 시 경고 로그). 모의 미제공(01900)이면 빈 결과이고
        open_orders_supported도 거짓으로 남는다(유령 정리 제외). price 단위는 실측으로 확인."""
        fields = self._account_fields(Account.KR_DERIV)
        out: list[TrackedOrder] = []
        seen: set[str] = set()
        codes = list(dict.fromkeys(self._futures_codes.values()))  # 중복 제거·순서 유지
        for code in codes:
            body = {f"{self.DERIV_OPEN_ORDERS_TR}InBlock": {
                "accno": fields.get("AcntNo", ""), "passwd": fields.get("Pwd", ""),
                "expcode": code, "chegb": "2", "sortgb": "2", "cts_ordno": " ",
            }}
            resp = await self._request_paced(
                Account.KR_DERIV, self.DERIV_OPEN_ORDERS_TR, body, self.DERIV_ACC_PATH)
            if str(resp.body.get("rsp_cd", "")) in _PAPER_UNSUPPORTED_RSP_CDS:
                self._deriv_open_orders_ok = False
                return []  # 모의 미제공 → 빈 결과, 유령 정리 제외
            self._check_ok(resp, self.DERIV_OPEN_ORDERS_TR)
            raw_rows = resp.body.get(f"{self.DERIV_OPEN_ORDERS_TR}OutBlock1")
            rows = list(raw_rows) if isinstance(raw_rows, list) else []
            for row in rows:
                if float(row.get("ordrem") or 0) <= 0:
                    continue  # 전량 체결·취소 완료 행
                o = self._deriv_open_order(row)
                if o is not None and o.order_id not in seen:
                    seen.add(o.order_id)
                    out.append(self._adopt(o))
            tail = resp.body.get(f"{self.DERIV_OPEN_ORDERS_TR}OutBlock") or {}
            if str(tail.get("cts_ordno") or "").strip():
                # 연속 조회 키가 왔다 — 미체결이 한 페이지를 넘는 경우(자동M은 세트당 1건이라 드묾).
                order_log.logger_for(Venue.LS).warning(
                    "t0434 %s 미체결이 한 페이지를 넘음(cts_ordno=%s) — 다음 페이지는 미조회",
                    code, tail.get("cts_ordno"))
        self._deriv_open_orders_ok = True
        return out

    def _deriv_open_order(self, row: dict[str, Any]) -> TrackedOrder | None:
        # t0434 행(공식 문서): ordno(Number 7)·qty·cheqty·ordrem(미체결잔량)·price(Number 9.2)·
        # medosu(구분 "매수"/"매도")·ordgb(유형 "지정가")·status·expcode. 예시의 price는 문자열.
        key = self._futures_key.get(str(row.get("expcode", "")))
        if key is None:
            return None  # 취급 외 종목(원달러선물 등) — 추적 대상 아님
        underlying, instrument = key
        try:
            order_id = str(int(str(row["ordno"]).strip()))  # WS 통보와 같게 zero-pad 제거
        except (KeyError, ValueError):
            return None
        price = float(row.get("price") or 0)
        intent = OrderIntent(
            venue=Venue.LS, underlying=underlying, instrument=instrument,
            side=Side.BUY if "매수" in str(row.get("medosu", "")) else Side.SELL,
            qty=float(row.get("qty") or 0),
            order_type=OrderType.LIMIT if "지정" in str(row.get("ordgb", "지정가"))
            else OrderType.MARKET,
            price=price if price > 0 else None,
            account=Account.KR_DERIV,
        )
        exec_qty = float(row.get("cheqty") or 0)
        return TrackedOrder(
            order_id=order_id, intent=intent,
            status=OrderStatus.PARTIAL if exec_qty > 0 else OrderStatus.ACCEPTED,
            filled_qty=exec_qty, avg_fill_price=float(row.get("cheprice") or 0),
            placed_at=placed_at_from_hhmmss(row.get("ordtime")),  # 접수시각(공식 문서 ordtime)
            placed_epoch=placed_epoch_from_hhmmss(row.get("ordtime")),  # 정렬용(오늘 날짜)
        )

    STOCK_PRICE_TR = "t1102"    # 주식/ETF 현재가 (실측: OutBlock.price)
    FUTURES_PRICE_TR = "t8402"  # 주식선물 현재가 (실측: OutBlock.price)
    STOCK_MARKET_PATH = "/stock/market-data"

    async def _request_paced(
        self, account: Account, tr_cd: str, body: dict[str, Any], path: str
    ) -> RestResponse:
        """초당 한도(RateLimitError)에 걸리면 잠깐 기다렸다 재시도 — 시동 일괄 조회용.

        시동 시 ETF 입력·초기 가격 조회가 같은 TR(t1102)을 동시에 써서 한도에
        걸리면 조용히 실패하던 문제의 해결책(운영 실측).
        """
        import asyncio as _asyncio

        for _ in range(10):
            try:
                return await self._rest_for(account).request(tr_cd, body, path=path)
            except RateLimitError:
                await _asyncio.sleep(0.6)
        raise RateLimitError(f"{tr_cd} 초당 한도 지속")

    async def get_last_price(self, code: str, *, futures: bool = False) -> float | None:
        """종목 1개의 현재가(마감 후엔 종가) 조회. 모니터 초기 표시용."""
        if futures:
            tr, path, key = self.FUTURES_PRICE_TR, self.FUTURES_MARKET_PATH, "focode"
            account = Account.KR_DERIV
        else:
            tr, path, key = self.STOCK_PRICE_TR, self.STOCK_MARKET_PATH, "shcode"
            account = Account.KR_STOCK
        resp = await self._request_paced(
            account, tr, {f"{tr}InBlock": {key: code}}, path
        )
        self._check_ok(resp, tr)
        block = resp.body.get(f"{tr}OutBlock", {})
        try:
            return float(block["price"])
        except (KeyError, TypeError, ValueError):
            return None

    async def get_price_snapshots(
        self, *, pause_s: float = 0.0
    ) -> dict[tuple[Underlying, Instrument], float]:
        """취급 전 종목(주식/ETF/선물)의 현재가 일괄 조회 — 시동 초기값·창 오픈 시 1회용.

        순차 호출이되 사이에 쉬지 않는다(정정 2026-09-14). 옛 0.6초 쉼은 근거 없던 기본 한도
        "초당 2회" 시절 것 — 공식 한도는 t1102/t8402 모두 초당 10회라 9건이 한 초에 들어가고,
        넘치면 _request_paced가 0.6초 쉬고 재시도한다. 0.6초×9 = 시동 초기값 5초가 그냥 대기였다.
        """
        import asyncio as _asyncio

        out: dict[tuple[Underlying, Instrument], float] = {}
        targets: list[tuple[Underlying, Instrument, str, bool]] = []
        for u in Underlying:
            targets.append((u, Instrument.KR_STOCK, u.krx_code, False))
        for u, code in self._etf_symbols.items():
            targets.append((u, Instrument.KR_ETF, code, False))
        for (u, instrument), code in self._futures_codes.items():  # 근·차근 모두
            targets.append((u, instrument, code, True))
        for u, instrument, code, futures in targets:
            price = await self.get_last_price(code, futures=futures)
            if price is not None and price > 0:
                out[(u, instrument)] = price
            await _asyncio.sleep(pause_s)
        return out

    ETF_INFO_TR = "t1901"       # ETF 현재가/NAV/배율 (ETF 이론가.md §2)
    ETF_INFO_PATH = "/stock/etf"

    async def get_etf_refs(self, *, pause_s: float = 0.6) -> dict[Underlying, EtfTheoryInputs]:
        """ETF 이론가 계산의 고정 입력 일괄 조회 — 시작 시 1회, ETF별 t1901.

        전일NAV(jnilnav)·배율(leverage)·거래소 공식 iNAV(nav — 대체용). 기초 등락률은
        실시간 체결(drate)로 받으므로 여기서 조회하지 않는다(ETF 이론가.md §2).
        실패한 종목은 건너뛴다(모니터·전략은 없는 값이면 빈값 표시).
        """
        import asyncio as _asyncio
        import logging

        out: dict[Underlying, EtfTheoryInputs] = {}
        for u, etf_code in self._etf_symbols.items():
            try:
                resp = await self._request_paced(
                    Account.KR_STOCK,
                    self.ETF_INFO_TR,
                    {f"{self.ETF_INFO_TR}InBlock": {"shcode": etf_code}},
                    self.ETF_INFO_PATH,
                )
                self._check_ok(resp, self.ETF_INFO_TR)
                etf = resp.body.get(f"{self.ETF_INFO_TR}OutBlock", {})
                inav = float(etf.get("nav") or 0)
                out[u] = EtfTheoryInputs(
                    prev_nav=float(etf["jnilnav"]),
                    leverage=float(etf["leverage"]),  # 취급 ETF는 +2배(인버스 없음)
                    exchange_inav=inav if inav > 0 else None,  # "0.00"이면 대체 불가
                )
            except (RestError, KeyError, TypeError, ValueError):
                # 이 ETF는 이론가 없이 표시 — 원인은 로그로 남김(운영 실측용)
                logging.getLogger("kp_arb.ls").warning(
                    "ETF 이론가 입력 조회 실패: %s(%s)", u.value, etf_code, exc_info=True
                )
                continue
            finally:
                await _asyncio.sleep(pause_s)
        return out

    async def fetch_futures_master(self) -> list[dict[str, Any]]:
        """주식선물 마스터(t8401) 전 종목. 행: {hname, shcode, expcode, basecode}."""
        resp = await self._rest_for(Account.KR_DERIV).request(
            self.FUTURES_MASTER_TR,
            {f"{self.FUTURES_MASTER_TR}InBlock": {"dummy": "0"}},
            path=self.FUTURES_MARKET_PATH,
        )
        self._check_ok(resp, self.FUTURES_MASTER_TR)
        rows = resp.body.get(f"{self.FUTURES_MASTER_TR}OutBlock")
        return list(rows) if isinstance(rows, list) else []

    COMMODITY_MASTER_TR = "t8426"  # 상품선물 마스터 (미국달러선물 월물 — RTD 실측)
    FX_PRICE_TR = "t2111"          # 선물옵션 현재가 (통화선물 주간은 WS 미제공 → 폴링)

    async def fetch_commodity_master(self) -> list[dict[str, Any]]:
        """상품선물 마스터(t8426) 전 종목. 행: {hname, shcode, ...} — 원달러선물 찾기용."""
        resp = await self._rest_for(Account.KR_DERIV).request(
            self.COMMODITY_MASTER_TR,
            {f"{self.COMMODITY_MASTER_TR}InBlock": {"dummy": ""}},
            path=self.FUTURES_MARKET_PATH,
        )
        self._check_ok(resp, self.COMMODITY_MASTER_TR)
        rows = resp.body.get(f"{self.COMMODITY_MASTER_TR}OutBlock")
        return list(rows) if isinstance(rows, list) else []

    async def get_fx_futures_price(self, shcode: str) -> float | None:
        """원달러선물 현재가(t2111). 주간엔 WS 미제공(실측)이라 이 조회를 주기 반복."""
        resp = await self._rest_for(Account.KR_DERIV).request(
            self.FX_PRICE_TR,
            {f"{self.FX_PRICE_TR}InBlock": {"focode": shcode}},
            path=self.FUTURES_MARKET_PATH,
        )
        self._check_ok(resp, self.FX_PRICE_TR)
        block = resp.body.get(f"{self.FX_PRICE_TR}OutBlock", {})
        try:
            price = float(block["price"])
        except (KeyError, TypeError, ValueError):
            return None
        return price if price > 0 else None

    async def raw_request(
        self, account: Account, tr_cd: str, path: str, *, method: str = "POST"
    ) -> RestResponse:
        """진단용 원시 TR 요청(계좌 자격 주입, 파싱 없음). 실 응답 필드 확인용."""
        return await self._rest_for(account).request(
            tr_cd, self._account_fields(account), path=path, method=method
        )

    # --- 잔고/포지션 파싱 ---

    def _rows(self, resp: RestResponse, tr_cd: str) -> list[dict[str, Any]]:
        if str(resp.body.get("rsp_cd", "")) in _PAPER_UNSUPPORTED_RSP_CDS:
            return []  # 모의 미제공 TR(01900) → 빈 결과 (v6.1)
        self._check_ok(resp, tr_cd)
        block = resp.body.get(f"{tr_cd}OutBlock3")
        return list(block) if isinstance(block, list) else []

    def _amount(self, resp: RestResponse, tr_cd: str, field: str) -> float:
        self._check_ok(resp, tr_cd)
        block = resp.body.get(f"{tr_cd}OutBlock2")
        if not isinstance(block, dict) or field not in block:
            raise RestError(f"{field} missing in {tr_cd} response")
        return float(block[field])

    def _stock_position(self, row: dict[str, Any]) -> Position | None:
        # 주식/ETF 잔고는 롱 전용(공매도 미사용). 종목코드로 주식 vs ETF 판별.
        # 취급 외 종목(실계좌의 기존 보유 등)은 건너뛴다 — 시스템 추적 대상 아님.
        resolved = self._resolve_spot(str(row["IsuNo"]))
        if resolved is None:
            return None
        instrument, underlying = resolved
        return Position(
            venue=Venue.LS,
            instrument=instrument,
            underlying=underlying,
            side=Side.BUY,
            # 실측: 당일 매수는 T+2 미결제라 BalQty=0 → 매매기준잔고(BnsBaseBalQty) 사용.
            qty=float(row["BnsBaseBalQty"]),
            avg_price=float(row["AvrUprc"]),  # 실측 필드(AvrPrc 아님)
            account=Account.KR_STOCK,
        )

    def _deriv_position(self, row: dict[str, Any]) -> Position | None:
        # t0441 실측 행: expcode(선물코드 "A5067000"), medocd(1매도/2매수),
        # jqty(잔고수량), pamt(평균단가 문자열). 취급 외 종목(실계좌 보유)은 건너뜀.
        code = str(row.get("expcode", ""))
        key = self._futures_key.get(code)  # 코드 → (종목, 근|차근)
        if key is None:
            return None  # 취급 외 종목(예: 원달러선물·지난 월물 보유분) — 추적 대상 아님
        underlying, instrument = key
        qty = float(row.get("jqty") or 0)
        if qty <= 0:
            return None
        side = Side.BUY if str(row.get("medocd")) == "2" else Side.SELL  # 1매도 2매수
        return Position(
            venue=Venue.LS,
            instrument=instrument,
            underlying=underlying,
            side=side,
            qty=qty,
            avg_price=float(row.get("pamt") or 0),
            account=Account.KR_DERIV,
        )

    def _resolve_spot(self, code: str) -> tuple[Instrument, Underlying] | None:
        """현물 종목코드("A" 접두 유무 무관) → (주식|ETF, underlying). 취급 외면 None."""
        bare = code.lstrip("A")
        etf_underlying = self._etf_underlying.get(bare)
        if etf_underlying is not None:
            return Instrument.KR_ETF, etf_underlying
        underlying = Underlying.from_krx_code(bare)
        if underlying is None:
            return None
        return Instrument.KR_STOCK, underlying

    # --- 요청 본문 구성 ---
    # [라이브 정합 v6.4] 주문 TR은 `{tr}InBlock1` 래핑 필수(flat은 IGW50004 거부).
    # 현물 IsuNo는 "A"+종목코드, 비번 필드는 InptPwd.
    # 주문 성공 rsp_cd: 매수 00040 / 매도 00039 / 취소 00463.

    def _spot_isu(self, intent: OrderIntent) -> str:
        # 현물 주문 종목코드는 A 접두(주식 실측). ETF는 자기 종목코드 사용
        # (A 접두 체계는 주식과 동일 가정 — 첫 ETF 라이브 주문 시 확인).
        if intent.instrument is Instrument.KR_ETF:
            try:
                return f"A{self._etf_symbols[intent.underlying]}"
            except KeyError as exc:
                raise RestError(f"no ETF symbol for {intent.underlying}") from exc
        return f"A{intent.underlying.krx_code}"

    def _spot_order_body(self, intent: OrderIntent, account: Account) -> dict[str, Any]:
        return {
            f"{self.SPOT_ORDER_TR}InBlock1": {
                **self._order_account_fields(account),
                "IsuNo": self._spot_isu(intent),
                "OrdQty": int(intent.qty),
                "OrdPrc": int(intent.price) if intent.price is not None else 0,
                "BnsTpCode": "2" if intent.side is Side.BUY else "1",  # 1매도 2매수
                "OrdprcPtnCode": "00" if intent.order_type is OrderType.LIMIT else "03",
                "MgntrnCode": "000",  # 신용거래 없음
                "LoanDt": "",
                "OrdCndiTpCode": "0",
            }
        }

    # 선물 주문 InBlock 필드는 카탈로그 기반(선물 주문 자체는 미실측 — 첫 라이브 주문 시 확인).
    def _future_order_body(self, intent: OrderIntent, account: Account) -> dict[str, Any]:
        return {
            f"{self.FUTURE_ORDER_TR}InBlock1": {
                **self._order_account_fields(account),
                "FnoIsuNo": self._futures_symbol(intent),  # 선물 종목코드(t8401, 근|차근)
                "OrdQty": int(intent.qty),
                "FnoOrdPrc": intent.price if intent.price is not None else 0.0,
                "BnsTpCode": "2" if intent.side is Side.BUY else "1",  # 1매도 2매수
                "FnoOrdprcPtnCode": "00" if intent.order_type is OrderType.LIMIT else "03",
            }
        }

    def _fx_order_body(self, code: str, side: Side, qty: int, price: float,
                       account: Account) -> dict[str, Any]:
        # 원달러선물도 선물옵션 시장(CFOAT00100) — FnoIsuNo에 콤보 종목코드를 그대로.
        return {
            f"{self.FUTURE_ORDER_TR}InBlock1": {
                **self._order_account_fields(account),
                "FnoIsuNo": code,
                "OrdQty": int(qty),
                "FnoOrdPrc": price,
                "BnsTpCode": "2" if side is Side.BUY else "1",  # 1매도 2매수
                "FnoOrdprcPtnCode": "00",  # 지정가
            }
        }

    def _future_amend_body(
        self, ctx: OrderContext, qty: float | None, price: float | None
    ) -> dict[str, Any]:
        return {
            f"{self.FUTURE_AMEND_TR}InBlock1": {
                **self._order_account_fields(ctx.account),
                "FnoIsuNo": self._futures_symbol(ctx.intent),
                "OrgOrdNo": int(ctx.order_id),  # 원주문 보존
                "MdfyQty": int(qty if qty is not None else ctx.intent.qty),
                "FnoOrdPrc": price if price is not None else (ctx.intent.price or 0.0),
                "FnoOrdprcPtnCode": "00",
            }
        }

    def _future_cancel_body(self, ctx: OrderContext,
                            qty: float | None = None) -> dict[str, Any]:
        # 취소수량 = 장부의 남은 수량. 원주문 수량으로 보내면 부분체결 뒤 LS가 01443("취소수량이
        # 취소가능수량을 초과")으로 거부한다(실측 2026-09-09 #21579).
        return {
            f"{self.FUTURE_CANCEL_TR}InBlock1": {
                **self._order_account_fields(ctx.account),
                "FnoIsuNo": self._futures_symbol(ctx.intent),
                "OrgOrdNo": int(ctx.order_id),  # 원주문 보존
                "CancQty": int(qty if qty is not None else ctx.intent.qty),
            }
        }

    def _futures_symbol(self, intent: OrderIntent) -> str:
        """주문 의도의 (종목, 근|차근) → 선물 종목코드. 미보유 월물이면 RestError."""
        try:
            return self._futures_codes[(intent.underlying, intent.instrument)]
        except KeyError as exc:
            raise RestError(
                f"no futures symbol configured for {intent.underlying}/{intent.instrument}"
            ) from exc

    def _amend_body(
        self, ctx: OrderContext, qty: float | None, price: float | None
    ) -> dict[str, Any]:
        # 취소 실측과 동일한 래핑 패턴 + 카탈로그 필드(정정 자체는 미실측 — 첫 라이브 정정 시 확인).
        return {
            f"{self.SPOT_AMEND_TR}InBlock1": {
                **self._order_account_fields(ctx.account),
                "OrgOrdNo": int(ctx.order_id),  # 원주문 보존
                "IsuNo": self._spot_isu(ctx.intent),
                "OrdQty": int(qty if qty is not None else ctx.intent.qty),
                "OrdPrc": int(price if price is not None else (ctx.intent.price or 0)),
                "OrdprcPtnCode": "00",
                "OrdCndiTpCode": "0",
            }
        }

    def _cancel_body(self, ctx: OrderContext, qty: float | None = None) -> dict[str, Any]:
        # 취소수량 = 장부의 남은 수량(호출자 전달). 없으면 원주문 수량(옛 동작).
        return {
            f"{self.SPOT_CANCEL_TR}InBlock1": {
                **self._order_account_fields(ctx.account),
                "OrgOrdNo": int(ctx.order_id),  # 원주문 보존
                "IsuNo": self._spot_isu(ctx.intent),
                "OrdQty": int(qty if qty is not None else ctx.intent.qty),
            }
        }

    # --- 응답 파싱 ---

    def _require(self, order_id: str) -> OrderContext:
        ctx = self._orders.get(order_id)
        if ctx is None:
            raise ValueError(f"unknown order_id {order_id}")
        return ctx

    def _check_ok(self, resp: RestResponse, tr_cd: str) -> None:
        # LS 성공 rsp_cd는 "00"으로 시작(운영 "00000", 모의 "00136" 등).
        # "01xxx"(가격범위 01427, 정정할 수량 없음 01433 등)/"4xxxx"/"IGW…"는 거부 (v6.15).
        rsp_cd = resp.body.get("rsp_cd")
        if rsp_cd is not None and not str(rsp_cd).startswith("00"):
            message = f"{tr_cd} rejected ({rsp_cd}): {resp.body.get('rsp_msg')}"
            if str(rsp_cd) in _ORDER_GONE_RSP_CDS:
                raise OrderGoneError(message)
            raise RestError(message)

    def _parse_order_id(self, resp: RestResponse, tr_cd: str) -> str:
        self._check_ok(resp, tr_cd)
        for key in (f"{tr_cd}OutBlock2", f"{tr_cd}OutBlock"):
            block = resp.body.get(key)
            if isinstance(block, dict) and "OrdNo" in block:
                return str(block["OrdNo"])
        # 응답 형식 파악용으로 본문을 그대로 남긴다(정정 등 미실측 TR의 실제 형식 확인).
        raise RestError(f"order id missing in {tr_cd} response: {resp.body}")
