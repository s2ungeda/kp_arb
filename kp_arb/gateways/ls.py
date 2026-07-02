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

from ..config import LSAccounts
from ..domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from ..domain.models import OrderIntent, Position
from ..order_book import OrderStatus, TrackedOrder
from ..routing import account_for
from .base import LSGateway
from .ls_auth import TokenManager, TokenTransport
from .ls_rest import LSRestClient, RateLimiter, RestError, RestResponse, RestTransport

LIVE_BASE_URL = "https://openapi.ls-sec.co.kr:8080"
_LS_ACCOUNTS: tuple[Account, ...] = (Account.KR_STOCK, Account.KR_DERIV)


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
    DERIV_POSITIONS_TR = "CFOAQ50600"   # 선물옵션 잔고·평가 (get_positions, 모의 미제공→빈결과)
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
    ) -> None:
        self._rest_by_account = dict(rest_by_account)
        self._accounts = accounts
        self._futures_symbols: dict[Underlying, str] = dict(futures_symbols or {})
        # 단일종목 레버리지 ETF 코드(config.yaml에서 주입). 없으면 ETF 미취급.
        self._etf_symbols: dict[Underlying, str] = dict(etf_symbols or {})
        self._etf_underlying = {v: k for k, v in self._etf_symbols.items()}
        self._orders: dict[str, OrderContext] = {}
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
        now: Callable[[], float] = time.monotonic,
    ) -> LSApiGateway:
        """계좌별 키로 계좌별 토큰·REST 클라이언트를 조립. (레이트리밋은 계좌별 독립.)"""
        rest_by_account: dict[Account, LSRestClient] = {}
        for account in _LS_ACCOUNTS:
            cred = accounts.for_account(account)
            tokens = TokenManager(cred.appkey, cred.appsecret, token_transport, now=now)
            limiter = RateLimiter(now=now)
            rest_by_account[account] = LSRestClient(base_url, tokens, rest_transport, limiter)
        return cls(rest_by_account, accounts=accounts,
                   futures_symbols=futures_symbols, etf_symbols=etf_symbols)

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
        elif intent.instrument is Instrument.KR_STOCK_FUTURE:
            tr_cd, path = self.FUTURE_ORDER_TR, self.FUTURE_PATH
            body = self._future_order_body(intent, account)
        else:
            raise NotImplementedError(f"{intent.instrument} 주문 TR 미정 [OPEN §13 #3]")
        resp = await self._rest_for(account).request(tr_cd, body, path=path)
        order_id = self._parse_order_id(resp, tr_cd)
        self._orders[order_id] = OrderContext(order_id, intent, account, body)
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
        return new_id

    async def cancel_order(self, order_id: str) -> None:
        ctx = self._require(order_id)
        if ctx.intent.instrument in self._SPOT:
            tr_cd, path = self.SPOT_CANCEL_TR, self.SPOT_PATH
            body = self._cancel_body(ctx)
        else:
            tr_cd, path = self.FUTURE_CANCEL_TR, self.FUTURE_PATH
            body = self._future_cancel_body(ctx)
        resp = await self._rest_for(ctx.account).request(tr_cd, body, path=path)
        self._check_ok(resp, tr_cd)

    async def get_positions(self, account: Account) -> Sequence[Position]:
        """계좌별 잔고(포지션) 조회. 주식 CSPAQ12300 / 선물 FOCCQ33600."""
        if account is Account.KR_STOCK:
            resp = await self._rest_for(account).request(
                self.STOCK_POSITIONS_TR, self._account_fields(account), path=self.STOCK_ACC_PATH
            )
            rows = self._rows(resp, self.STOCK_POSITIONS_TR)
            return [self._stock_position(r) for r in rows]
        resp = await self._rest_for(account).request(
            self.DERIV_POSITIONS_TR, self._account_fields(account), path=self.DERIV_ACC_PATH
        )
        rows = self._rows(resp, self.DERIV_POSITIONS_TR)
        return [self._deriv_position(r) for r in rows]

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
        """미체결 주문 스냅샷(주식 CSPAQ13700). 선물 미체결 TR은 미확인 → 빈 결과."""
        if account is not Account.KR_STOCK:
            return []  # 선물 미체결 조회 TR 확인 후 구현
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
        return [self._open_order(row) for row in rows if float(row.get("MrcAbleQty", 0)) > 0]

    def _open_order(self, row: dict[str, Any]) -> TrackedOrder:
        # 실측 행: IsuNo "A005930", BnsTpCode 1매도/2매수, OrdPrc 문자열, ExecQty 체결누계.
        instrument, underlying = self._resolve_spot(str(row["IsuNo"]))
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
        )

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

    async def raw_request(
        self, account: Account, tr_cd: str, path: str, *, method: str = "POST"
    ) -> RestResponse:
        """진단용 원시 TR 요청(계좌 자격 주입, 파싱 없음). 실 응답 필드 확인용."""
        return await self._rest_for(account).request(
            tr_cd, self._account_fields(account), path=path, method=method
        )

    # --- 잔고/포지션 파싱 ---

    def _rows(self, resp: RestResponse, tr_cd: str) -> list[dict[str, Any]]:
        self._check_ok(resp, tr_cd)
        block = resp.body.get(f"{tr_cd}OutBlock3")
        return list(block) if isinstance(block, list) else []

    def _amount(self, resp: RestResponse, tr_cd: str, field: str) -> float:
        self._check_ok(resp, tr_cd)
        block = resp.body.get(f"{tr_cd}OutBlock2")
        if not isinstance(block, dict) or field not in block:
            raise RestError(f"{field} missing in {tr_cd} response")
        return float(block[field])

    def _stock_position(self, row: dict[str, Any]) -> Position:
        # 주식/ETF 잔고는 롱 전용(공매도 미사용). 종목코드로 주식 vs ETF 판별.
        instrument, underlying = self._resolve_spot(str(row["IsuNo"]))
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

    def _deriv_position(self, row: dict[str, Any]) -> Position:
        side = Side.BUY if str(row["BnsTpCode"]) == "2" else Side.SELL  # 1매도 2매수
        return Position(
            venue=Venue.LS,
            instrument=Instrument.KR_STOCK_FUTURE,
            underlying=self._underlying(str(row["IsuNo"])),
            side=side,
            qty=float(row["BalQty"]),
            avg_price=float(row["AvrPrc"]),
            account=Account.KR_DERIV,
        )

    def _underlying(self, code: str) -> Underlying:
        underlying = Underlying.from_krx_code(code)
        if underlying is None:
            raise RestError(f"unknown issue code {code}")
        return underlying

    def _resolve_spot(self, code: str) -> tuple[Instrument, Underlying]:
        """현물 종목코드("A" 접두 유무 무관) → (주식|ETF, underlying) 판별."""
        bare = code.lstrip("A")
        etf_underlying = self._etf_underlying.get(bare)
        if etf_underlying is not None:
            return Instrument.KR_ETF, etf_underlying
        return Instrument.KR_STOCK, self._underlying(bare)

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
                "FnoIsuNo": self._futures_symbol(intent.underlying),  # 선물 종목코드(config)
                "OrdQty": int(intent.qty),
                "FnoOrdPrc": intent.price if intent.price is not None else 0.0,
                "BnsTpCode": "2" if intent.side is Side.BUY else "1",  # 1매도 2매수
                "FnoOrdprcPtnCode": "00" if intent.order_type is OrderType.LIMIT else "03",
            }
        }

    def _future_amend_body(
        self, ctx: OrderContext, qty: float | None, price: float | None
    ) -> dict[str, Any]:
        return {
            f"{self.FUTURE_AMEND_TR}InBlock1": {
                **self._order_account_fields(ctx.account),
                "FnoIsuNo": self._futures_symbol(ctx.intent.underlying),
                "OrgOrdNo": int(ctx.order_id),  # 원주문 보존
                "MdfyQty": int(qty if qty is not None else ctx.intent.qty),
                "FnoOrdPrc": price if price is not None else (ctx.intent.price or 0.0),
                "FnoOrdprcPtnCode": "00",
            }
        }

    def _future_cancel_body(self, ctx: OrderContext) -> dict[str, Any]:
        return {
            f"{self.FUTURE_CANCEL_TR}InBlock1": {
                **self._order_account_fields(ctx.account),
                "FnoIsuNo": self._futures_symbol(ctx.intent.underlying),
                "OrgOrdNo": int(ctx.order_id),  # 원주문 보존
                "CancQty": int(ctx.intent.qty),
            }
        }

    def _futures_symbol(self, underlying: Underlying) -> str:
        try:
            return self._futures_symbols[underlying]
        except KeyError as exc:
            raise RestError(f"no futures symbol configured for {underlying}") from exc

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

    def _cancel_body(self, ctx: OrderContext) -> dict[str, Any]:
        return {
            f"{self.SPOT_CANCEL_TR}InBlock1": {
                **self._order_account_fields(ctx.account),
                "OrgOrdNo": int(ctx.order_id),  # 원주문 보존
                "IsuNo": self._spot_isu(ctx.intent),
                "OrdQty": int(ctx.intent.qty),
            }
        }

    # --- 응답 파싱 ---

    def _require(self, order_id: str) -> OrderContext:
        ctx = self._orders.get(order_id)
        if ctx is None:
            raise ValueError(f"unknown order_id {order_id}")
        return ctx

    def _check_ok(self, resp: RestResponse, tr_cd: str) -> None:
        # LS 성공 rsp_cd는 "0"으로 시작(운영 "00000", 모의 "00136" 등). 오류는 "4xxxx"/"IGW…".
        rsp_cd = resp.body.get("rsp_cd")
        if rsp_cd is not None and not str(rsp_cd).startswith("0"):
            raise RestError(f"{tr_cd} rejected ({rsp_cd}): {resp.body.get('rsp_msg')}")

    def _parse_order_id(self, resp: RestResponse, tr_cd: str) -> str:
        self._check_ok(resp, tr_cd)
        for key in (f"{tr_cd}OutBlock2", f"{tr_cd}OutBlock"):
            block = resp.body.get(key)
            if isinstance(block, dict) and "OrdNo" in block:
                return str(block["OrdNo"])
        raise RestError(f"order id missing in {tr_cd} response")
