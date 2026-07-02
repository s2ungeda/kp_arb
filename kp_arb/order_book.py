"""OrderBook — 주문·포지션·잔고의 실시간 관리 (DESIGN.md §5.9). 순수 로직.

운영 모델: 최초 실행 시 REST 스냅샷 1회(`load_snapshot`) → 이후는 **체결 이벤트로만**
주문 상태 전이·포지션·잔고를 증분 갱신한다(체결 대기 폴링 금지). 같은 스냅샷은
온디맨드(추후 UI 조회 버튼)로 재호출 가능.

상태 전이(이벤트로만): NEW → ACCEPTED(SC0) → PARTIAL/FILLED(SC1) / CANCELLED(SC3) / REJECTED(SC4).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from .domain.enums import Account, Instrument, Side, Underlying
from .domain.models import OrderIntent, Position
from .gateways.ls_ws import Fill


class OrderStatus(StrEnum):
    NEW = "new"              # 주문 전송·접수응답 수신
    ACCEPTED = "accepted"    # 거래소 접수 통보(SC0)
    PARTIAL = "partial"      # 부분 체결(SC1)
    FILLED = "filled"        # 전량 체결(SC1)
    CANCELLED = "cancelled"  # 취소 통보(SC3)
    REJECTED = "rejected"    # 거부 통보(SC4)


_OPEN: frozenset[OrderStatus] = frozenset(
    {OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.PARTIAL}
)

_PositionKey = tuple[Underlying, Instrument, Account | None]


@dataclass
class TrackedOrder:
    """추적 중인 주문 1건(런타임). 상태는 이벤트로만 바뀐다."""

    order_id: str
    intent: OrderIntent
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.intent.qty - self.filled_qty)


@dataclass
class _Pos:
    """내부 포지션 상태(signed). qty>0 롱 / qty<0 숏."""

    qty: float = 0.0
    avg_price: float = 0.0
    events: list[str] = field(default_factory=list)


class OrderBook:
    def __init__(self) -> None:
        self._orders: dict[str, TrackedOrder] = {}
        self._positions: dict[_PositionKey, _Pos] = {}
        self._balances: dict[Account, float] = {}

    # --- 최초/온디맨드 스냅샷 (REST 조회 결과 주입) ---

    def load_snapshot(
        self,
        *,
        positions: Iterable[Position] = (),
        balances: dict[Account, float] | None = None,
        open_orders: Iterable[TrackedOrder] = (),
    ) -> None:
        """REST 스냅샷으로 상태 초기화. 이후 갱신은 이벤트로만."""
        self._positions.clear()
        for p in positions:
            key = (p.underlying, p.instrument, p.account)
            self._positions[key] = _Pos(qty=p.signed_qty, avg_price=p.avg_price)
        self._balances = dict(balances or {})
        for order in open_orders:
            self._orders[order.order_id] = order

    # --- 주문 등록 (place_order 직후) ---

    def track(self, order_id: str, intent: OrderIntent) -> TrackedOrder:
        order = TrackedOrder(order_id=order_id, intent=intent)
        self._orders[order_id] = order
        return order

    # --- 이벤트 (WS 체결통보 → 상태 전이 + 증분 갱신) ---

    def on_ack(self, order_id: str) -> TrackedOrder | None:
        order = self._orders.get(order_id)
        if order is not None and order.status is OrderStatus.NEW:
            order.status = OrderStatus.ACCEPTED
        return order

    def on_fill(self, fill: Fill) -> TrackedOrder | None:
        """체결 이벤트 → 주문 누적·상태 전이 + 포지션·잔고 증분. 미지 주문은 무시(None)."""
        order = self._orders.get(fill.order_id)
        if order is None:
            return None
        total = order.filled_qty + fill.qty
        order.avg_fill_price = (
            (order.avg_fill_price * order.filled_qty + fill.price * fill.qty) / total
        )
        order.filled_qty = total
        order.status = (
            OrderStatus.FILLED if total >= order.intent.qty else OrderStatus.PARTIAL
        )
        self._apply_fill_to_position(order.intent, fill)
        self._apply_fill_to_balance(order.intent, fill)
        return order

    def on_cancel(self, order_id: str) -> TrackedOrder | None:
        order = self._orders.get(order_id)
        if order is not None and order.is_open:
            order.status = OrderStatus.CANCELLED
        return order

    def on_reject(self, order_id: str) -> TrackedOrder | None:
        order = self._orders.get(order_id)
        if order is not None and order.is_open:
            order.status = OrderStatus.REJECTED
        return order

    # --- 증분 계산 (순수) ---

    def _apply_fill_to_position(self, intent: OrderIntent, fill: Fill) -> None:
        key = (intent.underlying, intent.instrument, intent.account)
        pos = self._positions.setdefault(key, _Pos())
        signed = fill.qty if intent.side is Side.BUY else -fill.qty
        new_qty = pos.qty + signed
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            # 신규 또는 같은 방향 증가 → 평단 가중평균.
            pos.avg_price = (
                (abs(pos.qty) * pos.avg_price + fill.qty * fill.price) / abs(new_qty)
            )
        elif (new_qty > 0) != (pos.qty > 0) and new_qty != 0:
            pos.avg_price = fill.price  # 방향 반전 → 남은 수량의 평단 = 체결가
        elif new_qty == 0:
            pos.avg_price = 0.0  # 청산
        # 상계(방향 유지·감소)는 평단 유지.
        pos.qty = new_qty

    def _apply_fill_to_balance(self, intent: OrderIntent, fill: Fill) -> None:
        # KR 계좌 현금 증분(매수 -금액 / 매도 +금액, 수수료 차감).
        # HL(계좌 없음)은 마진 모델이라 제외.
        if intent.account is None:
            return
        amount = fill.qty * fill.price
        delta = -amount if intent.side is Side.BUY else amount
        self._balances[intent.account] = (
            self._balances.get(intent.account, 0.0) + delta - fill.fee
        )

    # --- 실시간 조회 ---

    def order(self, order_id: str) -> TrackedOrder | None:
        return self._orders.get(order_id)

    def open_orders(self) -> list[TrackedOrder]:
        return [o for o in self._orders.values() if o.is_open]

    def balance(self, account: Account) -> float:
        return self._balances.get(account, 0.0)

    def positions(self) -> list[Position]:
        """현재 보유(0 아닌) 포지션을 도메인 Position으로 반환."""
        result: list[Position] = []
        for (underlying, instrument, account), pos in self._positions.items():
            if pos.qty == 0:
                continue
            result.append(
                Position(
                    venue=instrument.venue,
                    instrument=instrument,
                    underlying=underlying,
                    side=Side.BUY if pos.qty > 0 else Side.SELL,
                    qty=abs(pos.qty),
                    avg_price=pos.avg_price,
                    account=account,
                )
            )
        return result

    def position_qty(self, underlying: Underlying, instrument: Instrument,
                     account: Account | None = None) -> float:
        """signed 수량(롱 +, 숏 -). 없으면 0."""
        pos = self._positions.get((underlying, instrument, account))
        return pos.qty if pos is not None else 0.0
