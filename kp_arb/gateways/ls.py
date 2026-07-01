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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..config import LSAccounts
from ..domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from ..domain.models import OrderIntent, Position
from ..routing import account_for
from .base import LSGateway
from .ls_rest import LSRestClient, RestError, RestResponse


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
    DERIV_TR = "FOCCQ33600"             # 선물옵션 잔고·증거금
    STOCK_ACC_PATH = "/stock/accno"
    DERIV_ACC_PATH = "/futureoption/accno"

    _SPOT: frozenset[Instrument] = frozenset({Instrument.KR_STOCK, Instrument.KR_ETF})
    _SUCCESS: frozenset[str] = frozenset({"00000"})

    def __init__(
        self,
        rest: LSRestClient,
        *,
        accounts: LSAccounts | None = None,
        futures_symbols: Mapping[Underlying, str] | None = None,
    ) -> None:
        self._rest = rest
        self._accounts = accounts
        self._futures_symbols: dict[Underlying, str] = dict(futures_symbols or {})
        self._orders: dict[str, OrderContext] = {}
        self.connected = False

    def _account_fields(self, account: Account) -> dict[str, Any]:
        """주문/조회 요청에 넣을 계좌 식별 필드. 실 계좌번호·비번은 env(LSAccounts)."""
        if self._accounts is None:
            return {"account": account.value}  # 플레이스홀더(테스트/드라이런)
        acct = self._accounts.for_account(account)
        return {"AcntNo": acct.number, "InptPwd": acct.password}  # 실 필드명은 라이브 확인

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
        resp = await self._rest.request(tr_cd, body, path=path)
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
        resp = await self._rest.request(tr_cd, body, path=path)
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
        resp = await self._rest.request(tr_cd, body, path=path)
        self._check_ok(resp, tr_cd)

    async def get_positions(self, account: Account) -> Sequence[Position]:
        """계좌별 잔고(포지션) 조회. 주식 CSPAQ12300 / 선물 FOCCQ33600."""
        if account is Account.KR_STOCK:
            resp = await self._rest.request(
                self.STOCK_POSITIONS_TR, self._account_fields(account), path=self.STOCK_ACC_PATH
            )
            rows = self._rows(resp, self.STOCK_POSITIONS_TR)
            return [self._stock_position(r) for r in rows]
        resp = await self._rest.request(
            self.DERIV_TR, self._account_fields(account), path=self.DERIV_ACC_PATH
        )
        rows = self._rows(resp, self.DERIV_TR)
        return [self._deriv_position(r) for r in rows]

    async def get_balance(self, account: Account) -> float:
        """계좌별 가용자금 조회. 주식 예수금 CSPAQ22200 / 선물 증거금 FOCCQ33600."""
        if account is Account.KR_STOCK:
            resp = await self._rest.request(
                self.STOCK_DEPOSIT_TR, self._account_fields(account), path=self.STOCK_ACC_PATH
            )
            return self._amount(resp, self.STOCK_DEPOSIT_TR, "DpsAmt")
        resp = await self._rest.request(
            self.DERIV_TR, self._account_fields(account), path=self.DERIV_ACC_PATH
        )
        return self._amount(resp, self.DERIV_TR, "OrdAbleAmt")

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
        # 주식 잔고는 롱 전용(공매도 미사용).
        return Position(
            venue=Venue.LS,
            instrument=Instrument.KR_STOCK,
            underlying=self._underlying(str(row["IsuNo"])),
            side=Side.BUY,
            qty=float(row["BalQty"]),
            avg_price=float(row["AvrPrc"]),
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

    # --- 요청 본문 구성 (TR 필드명/코드는 라이브 구현 시 확인) ---

    def _spot_order_body(self, intent: OrderIntent, account: Account) -> dict[str, Any]:
        return {
            **self._account_fields(account),
            "IsuNo": intent.underlying.krx_code,
            "OrdQty": intent.qty,
            "OrdPrc": intent.price if intent.price is not None else 0.0,
            "BnsTpCode": "2" if intent.side is Side.BUY else "1",  # 1매도 2매수
            "OrdprcPtnCode": "00" if intent.order_type is OrderType.LIMIT else "03",
        }

    def _future_order_body(self, intent: OrderIntent, account: Account) -> dict[str, Any]:
        return {
            **self._account_fields(account),
            "FnoIsuNo": self._futures_symbol(intent.underlying),  # 선물 종목코드(config)
            "OrdQty": intent.qty,
            "FnoOrdPrc": intent.price if intent.price is not None else 0.0,
            "BnsTpCode": "2" if intent.side is Side.BUY else "1",  # 1매도 2매수
            "FnoOrdprcPtnCode": "00" if intent.order_type is OrderType.LIMIT else "03",
        }

    def _future_amend_body(
        self, ctx: OrderContext, qty: float | None, price: float | None
    ) -> dict[str, Any]:
        return {
            **self._account_fields(ctx.account),
            "FnoIsuNo": self._futures_symbol(ctx.intent.underlying),
            "OrgOrdNo": ctx.order_id,  # 원주문 보존
            "MdfyQty": qty if qty is not None else ctx.intent.qty,
            "FnoOrdPrc": price if price is not None else (ctx.intent.price or 0.0),
            "FnoOrdprcPtnCode": "00",
        }

    def _future_cancel_body(self, ctx: OrderContext) -> dict[str, Any]:
        return {
            **self._account_fields(ctx.account),
            "FnoIsuNo": self._futures_symbol(ctx.intent.underlying),
            "OrgOrdNo": ctx.order_id,  # 원주문 보존
            "CancQty": ctx.intent.qty,
        }

    def _futures_symbol(self, underlying: Underlying) -> str:
        try:
            return self._futures_symbols[underlying]
        except KeyError as exc:
            raise RestError(f"no futures symbol configured for {underlying}") from exc

    def _amend_body(
        self, ctx: OrderContext, qty: float | None, price: float | None
    ) -> dict[str, Any]:
        return {
            **self._account_fields(ctx.account),
            "OrgOrdNo": ctx.order_id,  # 원주문 보존
            "IsuNo": ctx.intent.underlying.krx_code,
            "OrdQty": qty if qty is not None else ctx.intent.qty,
            "OrdPrc": price if price is not None else (ctx.intent.price or 0.0),
        }

    def _cancel_body(self, ctx: OrderContext) -> dict[str, Any]:
        return {
            **self._account_fields(ctx.account),
            "OrgOrdNo": ctx.order_id,  # 원주문 보존
            "IsuNo": ctx.intent.underlying.krx_code,
            "OrdQty": ctx.intent.qty,
        }

    # --- 응답 파싱 ---

    def _require(self, order_id: str) -> OrderContext:
        ctx = self._orders.get(order_id)
        if ctx is None:
            raise ValueError(f"unknown order_id {order_id}")
        return ctx

    def _check_ok(self, resp: RestResponse, tr_cd: str) -> None:
        rsp_cd = resp.body.get("rsp_cd")
        if rsp_cd is not None and rsp_cd not in self._SUCCESS:
            raise RestError(f"{tr_cd} rejected ({rsp_cd}): {resp.body.get('rsp_msg')}")

    def _parse_order_id(self, resp: RestResponse, tr_cd: str) -> str:
        self._check_ok(resp, tr_cd)
        for key in (f"{tr_cd}OutBlock2", f"{tr_cd}OutBlock"):
            block = resp.body.get(key)
            if isinstance(block, dict) and "OrdNo" in block:
                return str(block["OrdNo"])
        raise RestError(f"order id missing in {tr_cd} response")
