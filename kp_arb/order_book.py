"""OrderBook — 주문·포지션·잔고의 실시간 관리 (DESIGN.md §5.9). 순수 로직.

운영 모델: 최초 실행 시 REST 스냅샷 1회(`load_snapshot`) → 이후는 **체결 이벤트로만**
주문 상태 전이·포지션·잔고를 증분 갱신한다(체결 대기 폴링 금지). 같은 스냅샷은
온디맨드(추후 UI 조회 버튼)로 재호출 가능.

상태 전이(이벤트로만): NEW → ACCEPTED(SC0) → PARTIAL/FILLED(SC1) / CANCELLED(SC3) / REJECTED(SC4).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from . import order_log
from .domain.enums import Account, Instrument, Side, Underlying
from .domain.models import OrderIntent, Position
from .gateways.ls_ws import Fill, OrderEvent

# 스냅샷 재조정 유예(초): 방금 낸 주문은 아직 거래소 미체결조회에 안 뜰 수 있어, 이 시간
# 안에 tracked된 주문은 스냅샷에 없어도 지우지 않는다(그보다 오래된 phantom만 정리).
_SNAPSHOT_GRACE_S = 15.0


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
    placed_ts: float = 0.0  # 추적 시각(monotonic) — 스냅샷 재조정 유예용
    placed_at: str = ""     # 접수 시각(HH:MM:SS, 표시용) — 주문 리스트 '시각' 칸
    provisional_filled: float = 0.0  # 발주 응답 선반영 수량 — userFills 재통보 흡수용(HL 즉시체결)

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
        # 체결이 **실제 적용될 때**(즉시체결·대기체결 공용, 흡수된 재통보는 제외) 불리는
        # 콜백 — 체결내역·누적을 타이밍 경합 없이 정확히 1회 기록하려는 용도(LiveSystem이 붙임).
        self.on_fill_applied: list[Callable[[TrackedOrder, float, float, str], None]] = []
        # 미아 이벤트 버퍼(주문 역전 대비) — track 전에 온 WS 이벤트(체결·접수·취소·거부)를
        # 주문번호별로 보관했다가 track 직후 replay. LS·HL 공용. 외부 주문은 상한으로 버려짐.
        self._pending: dict[str, list[tuple[str, Fill | None]]] = {}

    # --- 최초/온디맨드 스냅샷 (REST 조회 결과 주입) ---

    def load_snapshot(
        self,
        *,
        positions: Iterable[Position] = (),
        balances: dict[Account, float] | None = None,
        open_orders: Iterable[TrackedOrder] = (),
        reconcile_accounts: Collection[Account | None] | None = None,
    ) -> None:
        """REST 스냅샷으로 상태 초기화. 이후 갱신은 이벤트로만.

        reconcile_accounts: phantom 정리를 적용할 계좌 목록(조회에 성공한 계좌만).
        None이면 전 계좌 정리(기존 동작). 지정하면 **그 계좌의 주문만** 정리 대상 —
        조회 실패한 계좌의 살아있는 미체결이 빈 스냅샷 때문에 지워지는 것을 막는다.
        """
        self._positions.clear()
        for p in positions:
            key = (p.underlying, p.instrument, p.account)
            self._positions[key] = _Pos(qty=p.signed_qty, avg_price=p.avg_price)
        self._balances = dict(balances or {})
        # 미체결 재조정: 스냅샷(거래소 실측)에 있으면 갱신, 없으면 **오래된 것만** 제거 —
        # HL 취소/체결 통보 누락으로 남은 phantom 정리. 방금 낸 주문(유예 내)은 보존.
        snapshot = {o.order_id: o for o in open_orders}
        now = time.monotonic()
        for oid in list(self._orders):
            if oid in snapshot:
                continue
            order = self._orders[oid]
            if reconcile_accounts is not None and order.intent.account not in reconcile_accounts:
                continue  # 조회 실패(또는 미조회) 계좌 — 살아있는 주문 보존
            if now - order.placed_ts >= _SNAPSHOT_GRACE_S:
                del self._orders[oid]
        self._orders.update(snapshot)

    def replace_positions(
        self, positions: Iterable[Position], *, instrument: Instrument
    ) -> None:
        """주어진 instrument의 포지션만 스냅샷 목록으로 교체 — 다른 instrument·잔고·주문 유지.

        체결 이벤트 후 HL 포지션(외부 거래 포함)을 clearinghouse 실측으로 실시간 갱신할 때
        사용(디바운스 조회). 목록에 없는 종목은 청산된 것 → 제거된다.
        """
        for key in [k for k in self._positions if k[1] is instrument]:
            del self._positions[key]
        for p in positions:
            self._positions[(p.underlying, p.instrument, p.account)] = _Pos(
                qty=p.signed_qty, avg_price=p.avg_price)

    # --- 주문 등록 (place_order 직후) ---

    def track(self, order_id: str, intent: OrderIntent) -> TrackedOrder:
        order = TrackedOrder(order_id=order_id, intent=intent, placed_ts=time.monotonic(),
                             placed_at=time.strftime("%H:%M:%S"))
        self._orders[order_id] = order
        return order

    # --- 이벤트 (WS 체결통보 → 상태 전이 + 증분 갱신) ---

    def on_ack(self, order_id: str) -> TrackedOrder | None:
        order = self._orders.get(order_id)
        if order is None:
            self._buffer_event(order_id, "ack")  # 접수 통보가 track보다 먼저 — 보관
            return None
        if order.status is OrderStatus.NEW:
            order.status = OrderStatus.ACCEPTED
        return order

    def on_fill(self, fill: Fill) -> TrackedOrder | None:
        """체결 이벤트 → 주문 누적·상태 전이 + 포지션·잔고 증분. 미지 주문은 무시(None).

        발주 응답으로 선반영(apply_place_fill)한 즉시체결이 있으면, userFills가 같은
        체결을 재통보하는 만큼(provisional_filled)을 흡수해 이중 반영을 막는다 —
        부분 즉시체결(잔량이 남는 경우)도 안전.
        """
        order = self._orders.get(fill.order_id)
        if order is None:
            self._buffer_event(fill.order_id, "fill", fill)  # 체결이 track보다 먼저 — 보관
            return None
        qty = fill.qty
        if order.provisional_filled > 0:  # 발주응답 선반영분 — userFills 재통보 흡수
            absorbed = min(qty, order.provisional_filled)
            order.provisional_filled -= absorbed
            qty -= absorbed
        if qty <= 0:  # 전량 흡수(선반영과 중복) — 무시
            return order
        if order.remaining_qty <= 0:  # 이미 전량 체결 뒤 중복 통보 — 무시
            return order
        self._apply_fill_core(order, qty, fill.price, fill.fee, fill.fill_id)
        return order

    def apply_place_fill(self, fill: Fill) -> TrackedOrder | None:
        """발주 응답의 즉시체결을 선반영(HL). 이후 userFills 재통보는 그 수량만큼 흡수돼
        이중 반영되지 않는다 — userFills를 놓쳐도 주문·포지션이 정합(미체결 잔류 방지).

        체결내역(deque) 기록·엔진 통지는 userFills가 전담하므로 여기선 호출하지 않는다.
        """
        order = self._orders.get(fill.order_id)
        if order is None:
            return None
        applied = min(fill.qty, order.remaining_qty)  # 주문수량 초과분은 무시
        if applied <= 0:
            return order
        self._apply_fill_core(order, applied, fill.price, fill.fee, fill.fill_id)
        order.provisional_filled += applied
        return order

    def _apply_fill_core(self, order: TrackedOrder, qty: float, price: float,
                         fee: float, fill_id: str) -> None:
        """누적·상태 전이 + 포지션·잔고 반영(수량 명시). on_fill/apply_place_fill 공용."""
        total = order.filled_qty + qty
        order.avg_fill_price = (
            (order.avg_fill_price * order.filled_qty + price * qty) / total
        )
        order.filled_qty = total
        order.status = (
            OrderStatus.FILLED if total >= order.intent.qty else OrderStatus.PARTIAL
        )
        self._apply_fill_to_position(order.intent, qty, price)
        self._apply_fill_to_balance(order.intent, qty, price, fee)
        order_log.order_filled(  # 거래소별 파일에 체결통보(부분/전량·누적) 기록
            order.intent, qty, price, fill_id, order.filled_qty)
        for cb in self.on_fill_applied:  # 실제 적용 1회 — 체결내역·누적(타이밍 무관)
            cb(order, qty, price, fill_id)

    def on_cancel(self, order_id: str) -> TrackedOrder | None:
        order = self._orders.get(order_id)
        if order is None:
            self._buffer_event(order_id, "cancel")  # 취소 통보가 track보다 먼저 — 보관
            return None
        if order.is_open:
            order.status = OrderStatus.CANCELLED
        return order

    def on_reject(self, order_id: str) -> TrackedOrder | None:
        order = self._orders.get(order_id)
        if order is None:
            self._buffer_event(order_id, "reject")  # 거부 통보가 track보다 먼저 — 보관
            return None
        if order.is_open:
            order.status = OrderStatus.REJECTED
        return order

    def on_order_event(self, event: OrderEvent) -> TrackedOrder | None:
        """WS 주문 이벤트(SC0/2/3/4) 디스패치. 결선: client.on_order_event.append(...)"""
        if event.kind == "ack":
            return self.on_ack(event.order_id)
        # 취소/거부 통보는 새 통보의 원주문(orgordno)이 대상. 없으면 자기 자신.
        target = event.org_order_id or event.order_id
        if event.kind == "cancel":
            return self.on_cancel(target)
        if event.kind == "reject":
            return self.on_reject(target)
        return None  # amend(정정)는 새 주문 재등록이 필요 — 상위(게이트웨이 결선)에서 처리

    # --- 미아 이벤트 버퍼 (주문 역전 대비, LS·HL 공용) ---

    _PENDING_CAP = 512  # 상한 — 외부(내가 안 낸) 주문 이벤트가 무한정 쌓이지 않게

    def _buffer_event(self, order_id: str, kind: str, fill: Fill | None = None) -> None:
        """track 안 된 주문번호로 온 WS 이벤트를 도착 순서대로 보관 — track 시 replay."""
        buf = self._pending.get(order_id)
        if buf is None:
            if len(self._pending) >= self._PENDING_CAP:  # 가장 오래된 미아부터 버림
                del self._pending[next(iter(self._pending))]
            buf = self._pending[order_id] = []
        buf.append((kind, fill))

    def replay_pending(self, order_id: str) -> None:
        """그 주문번호로 track 직후 호출 — 보관된 이벤트를 도착 순서대로 반영(역전 복구).

        체결이 apply_place_fill보다 먼저 왔어도, replay를 apply_place_fill **뒤**에 두면
        provisional_filled가 겹친 체결을 흡수해 이중 반영을 막는다.
        """
        events = self._pending.pop(order_id, None)
        if not events:
            return
        for kind, fill in events:
            if kind == "fill" and fill is not None:
                self.on_fill(fill)
            elif kind == "ack":
                self.on_ack(order_id)
            elif kind == "cancel":
                self.on_cancel(order_id)
            elif kind == "reject":
                self.on_reject(order_id)

    # --- 증분 계산 (순수) ---

    def _apply_fill_to_position(
        self, intent: OrderIntent, qty: float, price: float
    ) -> None:
        key = (intent.underlying, intent.instrument, intent.account)
        pos = self._positions.setdefault(key, _Pos())
        signed = qty if intent.side is Side.BUY else -qty
        new_qty = pos.qty + signed
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            # 신규 또는 같은 방향 증가 → 평단 가중평균.
            pos.avg_price = (
                (abs(pos.qty) * pos.avg_price + qty * price) / abs(new_qty)
            )
        elif (new_qty > 0) != (pos.qty > 0) and new_qty != 0:
            pos.avg_price = price  # 방향 반전 → 남은 수량의 평단 = 체결가
        elif new_qty == 0:
            pos.avg_price = 0.0  # 청산
        # 상계(방향 유지·감소)는 평단 유지.
        pos.qty = new_qty

    def _apply_fill_to_balance(
        self, intent: OrderIntent, qty: float, price: float, fee: float
    ) -> None:
        # KR 계좌 현금 증분(매수 -금액 / 매도 +금액, 수수료 차감).
        # HL(계좌 없음)은 마진 모델이라 제외.
        if intent.account is None:
            return
        amount = qty * price
        delta = -amount if intent.side is Side.BUY else amount
        self._balances[intent.account] = (
            self._balances.get(intent.account, 0.0) + delta - fee
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

    def avg_price(self, underlying: Underlying, instrument: Instrument,
                  account: Account | None = None) -> float:
        """보유 평균단가. 없으면 0."""
        pos = self._positions.get((underlying, instrument, account))
        return pos.avg_price if pos is not None else 0.0
