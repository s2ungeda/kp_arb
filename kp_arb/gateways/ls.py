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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..domain.enums import Account, Instrument, OrderType, Side, Venue
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

    _SPOT: frozenset[Instrument] = frozenset({Instrument.KR_STOCK, Instrument.KR_ETF})
    _SUCCESS: frozenset[str] = frozenset({"00000"})

    def __init__(self, rest: LSRestClient) -> None:
        self._rest = rest
        self._orders: dict[str, OrderContext] = {}
        self.connected = False

    async def connect(self) -> None:
        # 토큰은 LSRestClient.request 시점에 lazy 발급. 여기선 연결 플래그만.
        self.connected = True

    async def place_order(self, intent: OrderIntent) -> str:
        if intent.venue is not Venue.LS:
            raise ValueError("LSApiGateway only handles LS orders")
        account = account_for(intent.instrument)  # 라우팅 계약(불변식)
        if intent.instrument not in self._SPOT:
            raise NotImplementedError(
                f"{intent.instrument} 주문 TR 미정 [OPEN §13 #3] — 현물(주식/ETF)만 구현"
            )
        body = self._spot_order_body(intent, account)
        resp = await self._rest.request(self.SPOT_ORDER_TR, body, path=self.SPOT_PATH)
        order_id = self._parse_order_id(resp, self.SPOT_ORDER_TR)
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
        body = self._amend_body(ctx, qty, price)
        resp = await self._rest.request(self.SPOT_AMEND_TR, body, path=self.SPOT_PATH)
        new_id = self._parse_order_id(resp, self.SPOT_AMEND_TR)
        self._orders[new_id] = OrderContext(
            new_id, ctx.intent, ctx.account, body, replaces=order_id
        )
        return new_id

    async def cancel_order(self, order_id: str) -> None:
        ctx = self._require(order_id)
        body = self._cancel_body(ctx)
        resp = await self._rest.request(self.SPOT_CANCEL_TR, body, path=self.SPOT_PATH)
        self._check_ok(resp, self.SPOT_CANCEL_TR)

    async def get_positions(self, account: Account) -> Sequence[Position]:
        raise NotImplementedError("잔고/포지션 조회는 블록 1-5")

    async def get_balance(self, account: Account) -> float:
        raise NotImplementedError("예수금/증거금 조회는 블록 1-5")

    # --- 요청 본문 구성 (TR 필드명/코드는 라이브 구현 시 확인) ---

    def _spot_order_body(self, intent: OrderIntent, account: Account) -> dict[str, Any]:
        return {
            # Account enum → 실제 계좌상품코드 매핑은 config [OPEN §13 #3].
            "account": account.value,
            "IsuNo": intent.underlying.krx_code,
            "OrdQty": intent.qty,
            "OrdPrc": intent.price if intent.price is not None else 0.0,
            "BnsTpCode": "2" if intent.side is Side.BUY else "1",  # 1매도 2매수
            "OrdprcPtnCode": "00" if intent.order_type is OrderType.LIMIT else "03",
        }

    def _amend_body(
        self, ctx: OrderContext, qty: float | None, price: float | None
    ) -> dict[str, Any]:
        return {
            "account": ctx.account.value,
            "OrgOrdNo": ctx.order_id,  # 원주문 보존
            "IsuNo": ctx.intent.underlying.krx_code,
            "OrdQty": qty if qty is not None else ctx.intent.qty,
            "OrdPrc": price if price is not None else (ctx.intent.price or 0.0),
        }

    def _cancel_body(self, ctx: OrderContext) -> dict[str, Any]:
        return {
            "account": ctx.account.value,
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
