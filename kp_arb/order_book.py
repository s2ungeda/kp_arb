"""OrderBook — 주문·포지션·잔고의 실시간 관리 (DESIGN.md §5.9). 순수 로직.

운영 모델: 최초 실행 시 REST 스냅샷 1회(`load_snapshot`) → 이후는 **체결 이벤트로만**
주문 상태 전이·포지션·잔고를 증분 갱신한다(체결 대기 폴링 금지). 같은 스냅샷은
온디맨드(추후 UI 조회 버튼)로 재호출 가능.

상태 전이(이벤트로만): NEW → ACCEPTED(SC0) → PARTIAL/FILLED(SC1) / CANCELLED(SC3) / REJECTED(SC4).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from . import order_log
from .domain.enums import Account, Instrument, Side, Underlying, Venue
from .domain.models import OrderIntent, Position
from .gateways.ls_ws import Fill, OrderEvent
from .routing import account_for

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

# 체결 완료 판정 톨러런스 — HL은 체결이 잘게 쪼개져 와(55.712·0.022…) 합이 부동소수점상
# 주문수량에 한 톨 못 미친다(99.99999…). 이 잔여 이하는 '전량 체결'로 본다. 국내 최소단위
# (주식 1주·HL 0.001)보다 훨씬 작아 실제 부분체결은 가리지 않는다.
_FILL_EPS = 1e-6

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
    placed_epoch: float = 0.0  # 접수 시각(epoch 초, 정렬용) — 시동 조회분은 거래소 값으로 채움
    provisional_filled: float = 0.0  # 발주 응답 선반영 수량 — userFills 재통보 흡수용(HL 즉시체결)
    # 이미 반영한 체결번호(LS yakseq/execno, HL tid) — 같은 체결이 두 번 오거나(서버 재전송)
    # 코어 안에서
    # 두 번 처리돼도(운영 사고 2026-10-12: 훅 이중 등록) 한 번만 반영. 비교·표시에서 제외
    seen_fill_ids: set[str] = field(default_factory=set, compare=False, repr=False)
    # 취소 **확정** 수량 누적(결정 69) — 거래소가 실제로 거둬들인 수량. 체결 + 취소 = 주문수량이
    # 돼야 주문이 닫힌다. 확정이 남은 수량보다 적으면 차이는 아직 통보 안 온 체결(기다림)
    cancelled_qty: float = 0.0
    # 취소 수량이 통보에 실려 온 게 아니라 "남은 전부"로 가정해 닫았음(HL — 취소 응답·통보에 확정
    # 수량 개념이 없다, 사용자 2026-10-13 "거래소가 다른데 LS처럼 처리하면 안 되지"). 이 경우 뒤에
    # 오는 체결(취소 직전 체결의 통보가 늦음)은 정상 경합 — 그만큼 취소 수량을 줄여 셈을 맞춘다
    cancel_assumed: bool = False

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN

    @property
    def remaining_qty(self) -> float:
        """아직 셈이 안 끝난 수량 = 주문 − 체결 − 취소 확정(결정 69)."""
        rem = self.intent.qty - self.filled_qty - self.cancelled_qty
        return 0.0 if rem <= _FILL_EPS else rem  # 부동소수점 잔여는 0으로(완료로 봄)


@dataclass
class _Pos:
    """내부 포지션 상태(signed). qty>0 롱 / qty<0 숏."""

    qty: float = 0.0
    avg_price: float = 0.0
    events: list[str] = field(default_factory=list)


class OrderBook:
    def __init__(self) -> None:
        # 주문 키 = (계좌, 주문번호). LS는 계좌마다 번호를 따로 매겨 주식·선물 번호가 겹친다(운영
        # 사고 2026-10-14: 주식 HTS #4545의 체결 8주가 선물 선주문 #4545에 들어가 HL 80계약 초과
        # 헤지 — 결정 73 "계좌·종목·주문번호로 찾는다"). HL은 계좌 None
        self._orders: dict[tuple[Account | None, str], TrackedOrder] = {}
        self._positions: dict[_PositionKey, _Pos] = {}
        self._balances: dict[Account, float] = {}
        # 체결이 **실제 적용될 때**(즉시체결·대기체결 공용, 흡수된 재통보는 제외) 불리는
        # 콜백 — 체결내역·누적을 타이밍 경합 없이 정확히 1회 기록하려는 용도(LiveSystem이 붙임).
        self.on_fill_applied: list[Callable[[TrackedOrder, float, float, str], None]] = []
        # 장부가 바뀔 때(주문 등록·접수·체결·취소·거부·스냅샷 반영)마다 불리는 콜백 —
        # 코어→화면 실시간 채널(DESIGN §12.1)이 "밀어줄 게 생겼다"를 알아채는 용도.
        self.on_change: list[Callable[[], None]] = []
        # 미아 이벤트 버퍼(주문 역전 대비) — track 전에 온 WS 이벤트(체결·접수·취소·거부)를
        # 주문번호별로 보관했다가 track 직후 replay. LS·HL 공용. 외부 주문은 상한으로 버려짐.
        # (종류, 체결, 취소 확정 수량) — 도착 순서대로
        # 키 (계좌, 주문번호) — 주식 통보가 같은 번호의 선물 주문에 재생되지 않게(결정 73)
        self._pending: dict[tuple[Account | None, str],
                            list[tuple[str, Fill | None, float | None]]] = {}

    # --- 최초/온디맨드 스냅샷 (REST 조회 결과 주입) ---

    def load_snapshot(
        self,
        *,
        positions: Iterable[Position] = (),
        balances: dict[Account, float] | None = None,
        open_orders: Iterable[TrackedOrder] = (),
        reconcile_accounts: Collection[Account | None] | None = None,
        scope: Collection[Account | None] | None = None,
    ) -> None:
        """REST 스냅샷으로 상태 초기화. 이후 갱신은 이벤트로만.

        reconcile_accounts: phantom 정리를 적용할 계좌 목록(조회에 성공한 계좌만).
        None이면 전 계좌 정리(기존 동작). 지정하면 **그 계좌의 주문만** 정리 대상 —
        조회 실패한 계좌의 살아있는 미체결이 빈 스냅샷 때문에 지워지는 것을 막는다.
        scope: 이번 스냅샷이 다루는 계좌(HL은 None). 지정하면 **그 계좌의 포지션·잔고만** 갈아
        끼우고 나머지는 그대로 둔다 — 한 시장(HL)의 재연결이 다른 시장(LS) 장부를 건드리지 않게
        (실측 2026-09-09: HL 재연결 때 LS까지 전체 재동기 → 선물 선주문이 유령으로 지워짐).
        """
        if scope is None:
            self._positions.clear()
            self._balances = dict(balances or {})
        else:
            for key in [k for k in self._positions if k[2] in scope]:
                del self._positions[key]
            self._balances.update(balances or {})
        for p in positions:
            key = (p.underlying, p.instrument, p.account)
            self._positions[key] = _Pos(qty=p.signed_qty, avg_price=p.avg_price)
        # 미체결 재조정: 스냅샷(거래소 실측)에 있으면 갱신, 없으면 **오래된 것만** 제거 —
        # HL 취소/체결 통보 누락으로 남은 phantom 정리. 방금 낸 주문(유예 내)은 보존.
        snapshot = {self._key_of(o.intent, o.order_id): o for o in open_orders}
        now = time.monotonic()
        for okey in list(self._orders):
            if okey in snapshot:
                continue
            oid = okey[1]
            order = self._orders[okey]
            if reconcile_accounts is not None and order.intent.account not in reconcile_accounts:
                continue  # 조회 실패(또는 미조회) 계좌 — 살아있는 주문 보존
            if order.cancelled_qty > 0 and order.remaining_qty > 0:
                # 부분 취소 확정 뒤 체결 통보를 기다리는 주문(결정 69) — 거래소는 이미 닫아 미체결
                # 목록에 없는 게 정상. 지우면 뒤따르는 체결이 미아가 돼 헤지가 빠진다(검토 10-13 A)
                logging.getLogger("kp_arb.order").warning(
                    "재동기: #%s 부분 취소 확정(취소 %g) 뒤 체결 %g 통보 대기 중 — 유령 정리 제외",
                    oid, order.cancelled_qty, order.remaining_qty)
                continue
            if now - order.placed_ts >= _SNAPSHOT_GRACE_S:
                del self._orders[okey]
        for okey, snap in snapshot.items():
            cur = self._orders.get(okey)
            if cur is None:
                self._orders[okey] = snap
            else:
                self._merge_snapshot_order(cur, snap)
        self._changed()

    def _merge_snapshot_order(self, cur: TrackedOrder, snap: TrackedOrder) -> None:
        """추적 중인 주문에 스냅샷(REST 미체결 조회)을 **병합**(검토 2026-10-13 ★2) — 객체를 바꿔
        끼우면 `seen_fill_ids`(중복 체결 방어)·`provisional_filled`·`cancelled_qty`·intent(출처·
        꼬리표)가
        사라져 같은 체결 통보가 다시 반영되거나(10-12 사고와 같은 결과) 취소 셈이 틀어진다.
        · 로컬이 이미 닫았으면(취소 확정 등) 스냅샷이 되돌리지 않는다.
        · 스냅샷 체결이 더 많으면 끊긴 사이 체결 — 수량·상태만 갱신하고(포지션은 스냅샷이 이미
          갈아 끼움) 차이를 `on_fill_applied`로 알린다(엔진 헤지, fill_id "resync-번호").
        · 로컬이 더 많으면 통보가 조회보다 앞선 것 — 로컬 유지(되돌리면 취소수량 오류 01443)."""
        log = logging.getLogger("kp_arb.order")
        if not cur.placed_at and snap.placed_at:  # 접수시각(표시·정렬)은 거래소 값으로 채움
            cur.placed_at, cur.placed_epoch = snap.placed_at, snap.placed_epoch
        if not cur.is_open:
            log.warning("재동기: #%s 로컬은 %s인데 거래소 미체결에 있음 — 로컬 유지(통보가 먼저)",
                        cur.order_id, cur.status.value)
            return
        diff = snap.filled_qty - cur.filled_qty
        if diff > _FILL_EPS:
            price = snap.avg_fill_price if snap.avg_fill_price > 0 else (cur.intent.price or 0.0)
            log.warning("재동기: #%s 체결 %g → %g — 끊긴 사이 체결 %g 반영(통보 없이), 엔진 알림",
                        cur.order_id, cur.filled_qty, snap.filled_qty, diff)
            cur.filled_qty = snap.filled_qty
            if snap.avg_fill_price > 0:
                cur.avg_fill_price = snap.avg_fill_price
            cur.status = self._status_after_qty(cur)
            fill_id = f"resync-{cur.order_id}"
            order_log.order_filled(cur.intent, diff, price, fill_id, cur.filled_qty,
                                   order_id=cur.order_id)
            for cb in self.on_fill_applied:
                cb(cur, diff, price, fill_id)
        elif diff < -_FILL_EPS:
            log.warning("재동기: #%s 로컬 체결 %g > 거래소 조회 %g — 통보가 조회보다 앞섬, 유지",
                        cur.order_id, cur.filled_qty, snap.filled_qty)
        elif cur.status is OrderStatus.NEW and snap.status is not OrderStatus.NEW:
            cur.status = snap.status  # 접수 통보를 놓친 신규 → 거래소가 받았음

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

    @staticmethod
    def _acct_of(intent: OrderIntent) -> Account | None:
        """주문의 계좌 — LS는 intent.account(없으면 상품으로 라우팅), HL은 None."""
        if intent.venue is not Venue.LS:
            return None
        return intent.account or account_for(intent.instrument)

    @classmethod
    def _key_of(cls, intent: OrderIntent, order_id: str) -> tuple[Account | None, str]:
        return (cls._acct_of(intent), order_id)

    @classmethod
    def account_of(cls, intent: OrderIntent) -> Account | None:
        """장부가 이 주문을 둔 계좌(LS) — 번호만 넘기던 경로가 계좌를 같이 넘길 때 쓴다(검토 G2)."""
        return cls._acct_of(intent)

    def hl_order(self, order_id: str) -> TrackedOrder | None:
        """HL 주문만 찾는다(계좌 없음 키). HL 통보·응답 경로에서 LS 번호로 흘러가지 않게 명시."""
        return self._orders.get((None, order_id))

    def _find(self, order_id: str, account: Account | None = None) -> TrackedOrder | None:
        """주문 찾기(결정 73). 계좌를 주면 그 계좌의 번호만. 안 주면 HL(계좌 None) 먼저, 없으면
        LS 계좌들 중 **그 번호가 하나뿐일 때만** 돌려준다 — 둘 이상이면 열린 것이 하나뿐일 때 그것,
        아니면 None + 경고(계좌 없이 가릴 수 없는 번호)."""
        if account is not None:
            return self._orders.get((account, order_id))
        hl = self._orders.get((None, order_id))
        if hl is not None:
            return hl
        matches = [o for (a, oid), o in self._orders.items() if oid == order_id and a is not None]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            live = [o for o in matches if o.is_open]
            if len(live) == 1:
                return live[0]
            logging.getLogger("kp_arb.order").warning(
                "주문번호 #%s가 계좌 %s에 겹침 — 계좌 없이는 가릴 수 없음", order_id,
                [str(self._acct_of(o.intent)) for o in matches])
        return None

    def track(self, order_id: str, intent: OrderIntent) -> TrackedOrder:
        order = TrackedOrder(order_id=order_id, intent=intent, placed_ts=time.monotonic(),
                             placed_at=time.strftime("%H:%M:%S"), placed_epoch=time.time())
        self._orders[self._key_of(intent, order_id)] = order
        self._changed()
        return order

    def _changed(self) -> None:
        for cb in self.on_change:
            cb()

    # --- 이벤트 (WS 체결통보 → 상태 전이 + 증분 갱신) ---

    def on_ack(self, order_id: str, account: Account | None = None) -> TrackedOrder | None:
        order = self._find(order_id, account)
        if order is None:
            self._buffer_event(order_id, "ack", account=account)  # 접수 통보가 track보다 먼저
            return None
        if order.status is OrderStatus.NEW:
            order.status = OrderStatus.ACCEPTED
            self._changed()
        return order

    def on_fill(self, fill: Fill) -> TrackedOrder | None:
        """체결 이벤트 → 주문 누적·상태 전이 + 포지션·잔고 증분. 미지 주문은 무시(None).

        발주 응답으로 선반영(apply_place_fill)한 즉시체결이 있으면, userFills가 같은
        체결을 재통보하는 만큼(provisional_filled)을 흡수해 이중 반영을 막는다 —
        부분 즉시체결(잔량이 남는 경우)도 안전.
        """
        order = self._find(fill.order_id, fill.account)
        if order is None:
            self._buffer_event(fill.order_id, "fill", fill, account=fill.account)  # track 전
            return None
        if fill.fill_id and fill.fill_id in order.seen_fill_ids:
            # 같은 체결번호 두 번째 — 서버 재전송이든 코어 안 이중 처리든 반영하지 않는다.
            # 숨기지 않고
            # 경고로 남긴다(2026-10-12: 조용히 두 번 들어가 HL이 초과 헤지됨)
            logging.getLogger("kp_arb.order").warning(
                "중복 체결 통보 무시 #%s 체결번호 %s %g @ %g — 이미 반영됨",
                         fill.order_id, fill.fill_id, fill.qty, fill.price)
            return order
        if fill.fill_id:
            order.seen_fill_ids.add(fill.fill_id)
        qty = fill.qty
        if order.provisional_filled > 0:  # 발주응답 선반영분 — userFills 재통보 흡수
            absorbed = min(qty, order.provisional_filled)
            order.provisional_filled -= absorbed
            qty -= absorbed
        if qty <= _FILL_EPS:  # 전량 흡수(선반영과 중복) — 무시. 부동소수점 찌꺼기(2.8e-17,
            return order      # 실측 2026-09-11: 0.521+5.208+0.234 vs 5.963)도 체결로 안 본다
        if order.status is OrderStatus.FILLED:  # 전량 체결 뒤 재통보(HL userFills 중복) — 무시
            return order
        if not order.is_open:
            # 취소·거부로 닫은 뒤 온 체결. 상태는 되살리지 않는다(유령 미체결 방지, 검토 10-13 ★1).
            # 수량·포지션·잔고는 실제 돈이니 반영한다.
            log = logging.getLogger("kp_arb.order")
            if order.cancel_assumed and order.cancelled_qty + _FILL_EPS >= qty:
                # HL처럼 "남은 전부 취소"로 가정해 닫은 주문 — 취소 직전 체결의 통보가 늦게 온 정상
                # 경합. 그만큼은 취소가 아니라 체결이었으니 취소 수량을 줄여 셈을 맞춘다
                order.cancelled_qty -= qty
                log.warning("닫힌 주문 #%s에 체결 통보 %s %g @ %g — 취소 직전 체결(정상 경합), "
                            "취소 수량 %g로 보정", fill.order_id, fill.fill_id, qty, fill.price,
                            order.cancelled_qty)
            else:
                # LS처럼 확정 수량으로 닫은 주문(결정 69) — 셈이 맞으면 올 일이 없다. 사람이 볼 일
                log.error("종료(%s)된 주문 #%s에 체결 통보 %s %g @ %g — 수량·포지션만 반영, 상태 "
                          "유지. 취소 확정 수량과 체결 셈이 어긋남(실포지션 확인)",
                          order.status.value, fill.order_id, fill.fill_id, qty, fill.price)
            self._apply_fill_core(order, qty, fill.price, fill.fee, fill.fill_id)
            return order
        if order.remaining_qty <= 0:  # 이미 전량 체결 뒤 중복 통보 — 무시
            return order
        if qty > order.remaining_qty + _FILL_EPS:
            # 셈(체결+취소)을 넘는 체결 — 거래소가 주문수량을 넘겨 체결할 수는 없으니 장부가
            # 틀린 것(취소 확정 과다 등). 실제 돈이라 자르지 않고 반영하되 에러로 남긴다(검토 F)
            logging.getLogger("kp_arb.order").error(
                "#%s 체결 %g가 장부 남은 수량 %g를 넘음(체결 %g 취소 %g 주문 %g) — 그대로 반영, "
                "LS·HL 실포지션 확인", fill.order_id, qty, order.remaining_qty,
                order.filled_qty, order.cancelled_qty, order.intent.qty)
        self._apply_fill_core(order, qty, fill.price, fill.fee, fill.fill_id)
        return order

    def apply_place_fill(self, fill: Fill) -> TrackedOrder | None:
        """발주 응답의 즉시체결을 선반영(HL). 이후 userFills 재통보는 그 수량만큼 흡수돼
        이중 반영되지 않는다 — userFills를 놓쳐도 주문·포지션이 정합(미체결 잔류 방지).

        체결내역(deque) 기록·엔진 통지는 userFills가 전담하므로 여기선 호출하지 않는다.
        """
        order = self._find(fill.order_id, fill.account)
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
        if order.is_open:  # 닫힌 주문의 늦은 체결은 상태를 되살리지 않는다(on_fill 참고)
            order.status = self._status_after_qty(order)
        self._apply_fill_to_position(order.intent, qty, price)
        self._apply_fill_to_balance(order.intent, qty, price, fee)
        order_log.order_filled(  # 거래소별 파일에 체결통보(부분/전량·누적·주문번호) 기록
            order.intent, qty, price, fill_id, order.filled_qty, order_id=order.order_id)
        for cb in self.on_fill_applied:  # 실제 적용 1회 — 체결내역·누적(타이밍 무관)
            cb(order, qty, price, fill_id)
        self._changed()

    @staticmethod
    def _status_after_qty(order: TrackedOrder) -> OrderStatus:
        """열린 주문의 수량 셈 뒤 상태(결정 69): 셈이 끝나면(체결+취소=주문) 취소가 있었으면
        CANCELLED, 없으면 FILLED. 안 끝났으면 체결이 있으면 PARTIAL, 없으면 그대로."""
        if order.remaining_qty <= 0.0:  # 톨러런스 — 부동소수점으로 한 톨 모자라도 끝
            return OrderStatus.CANCELLED if order.cancelled_qty > 0 else OrderStatus.FILLED
        return OrderStatus.PARTIAL if order.filled_qty > 0 else order.status

    def on_cancel(self, order_id: str, qty: float | None = None,
                  account: Account | None = None) -> TrackedOrder | None:
        """취소 확정 통보(결정 69, 사용자 2026-10-13: "남은 수량이 있으면 기다리고 없으면 상태
        확정"). qty = 거래소가 확정한 취소 수량(H01 qty / SC3 canccnfqty / HL sz), None = 남은
        수량 전부(REST 응답으로 닫는 HL 취소 등).
        · 확정 = 남은 수량 → 닫음(CANCELLED).
        · 확정 < 남은 수량 → 차이는 체결됐는데 통보가 아직 안 온 것. 열어 둔 채 기다린다(시간
          제한 없음 — 옛 120초 타이머 폐기). 체결이 와서 셈이 맞으면 그때 닫힌다.
        · 확정 > 남은 수량 → 장부가 체결을 더 셌다는 뜻(이중 반영류). 닫고 에러 로그."""
        order = self._find(order_id, account)
        if order is None:
            self._buffer_event(order_id, "cancel", qty=qty, account=account)  # track 전 — 보관
            return None
        if not order.is_open:
            return order
        rem = order.remaining_qty
        log = logging.getLogger("kp_arb.order")
        if qty is None or qty >= rem - _FILL_EPS:
            if qty is None:
                order.cancel_assumed = True  # 수량 없는 취소(HL) — 남은 전부로 가정
            if qty is not None and qty > rem + _FILL_EPS:
                log.error("취소 확정 수량 %g > 장부 남은 수량 %g #%s — 장부가 체결을 더 셈(이중 "
                          "반영?) — 닫음, LS·HL 실포지션 확인", qty, rem, order_id)
            order.cancelled_qty += rem
        else:
            order.cancelled_qty += qty
            log.warning("부분 취소 확정 #%s: 확정 %g < 남은 %g — 체결 %g 통보 대기(주문은 열어 둠)",
                        order_id, qty, rem, rem - qty)
        order.status = self._status_after_qty(order)
        self._changed()
        return order

    def on_reject(self, order_id: str, account: Account | None = None) -> TrackedOrder | None:
        order = self._find(order_id, account)
        if order is None:
            self._buffer_event(order_id, "reject", account=account)  # 거부 통보가 track보다 먼저
            return None
        if order.is_open:
            order.status = OrderStatus.REJECTED
            self._changed()
        return order

    def on_order_event(self, event: OrderEvent) -> TrackedOrder | None:
        """WS 주문 이벤트(SC0/2/3/4) 디스패치. 결선: client.on_order_event.append(...)"""
        if event.kind == "ack":
            return self.on_ack(event.order_id, account=event.account)
        # 취소/거부 통보는 새 통보의 원주문(orgordno)이 대상. 없으면 자기 자신.
        target = event.org_order_id or event.order_id
        if event.kind == "cancel":
            return self.on_cancel(target, qty=event.qty, account=event.account)
        if event.kind == "reject":
            return self.on_reject(target, account=event.account)
        return None  # amend(정정)는 새 주문 재등록이 필요 — 상위(게이트웨이 결선)에서 처리

    # --- 미아 이벤트 버퍼 (주문 역전 대비, LS·HL 공용) ---

    _PENDING_CAP = 512  # 상한 — 외부(내가 안 낸) 주문 이벤트가 무한정 쌓이지 않게

    def _buffer_event(self, order_id: str, kind: str, fill: Fill | None = None,
                      qty: float | None = None, account: Account | None = None) -> None:
        """track 안 된 주문번호로 온 WS 이벤트를 도착 순서대로 보관 — track 시 replay.

        체결이면 경고 한 줄(주문번호마다 1회) — 실증 2026-09-16: 응답 없는 발주(#3326)가 체결됐는데
        조용히 보관만 해서 헤지 없는 SF 매도를 아무도 몰랐다. 발주 응답 전 체결(정상 역전)도 같은
        경고가 뜨지만 곧 track되며 replay되니 로그에서 짝을 확인할 수 있다."""
        key = (account, order_id)
        buf = self._pending.get(key)
        if buf is None:
            if len(self._pending) >= self._PENDING_CAP:  # 가장 오래된 미아부터 버림
                del self._pending[next(iter(self._pending))]
            buf = self._pending[key] = []
        if kind == "fill" and fill is not None and not any(k == "fill" for k, _f, _q in buf):
            logging.getLogger("kp_arb.order").warning(
                "미추적 주문 #%s(계좌 %s) 체결 통보 %g @ %g — 이 프로세스가 낸 주문이 아니거나 "
                "응답 없는 발주(보관, 등록되면 반영 / 안 되면 포지션 직접 확인)",
                order_id, account.value if account is not None else "HL", fill.qty,
                fill.price)
        buf.append((kind, fill, qty))

    def replay_pending(self, order_id: str, account: Account | None = None) -> None:
        """그 주문번호로 track 직후 호출 — 보관된 이벤트를 도착 순서대로 반영(역전 복구).

        체결이 apply_place_fill보다 먼저 왔어도, replay를 apply_place_fill **뒤**에 두면
        provisional_filled가 겹친 체결을 흡수해 이중 반영을 막는다.
        """
        if account is None:  # 등록된 주문의 계좌로 — 같은 번호의 다른 계좌 통보는 건드리지 않는다
            order = self._find(order_id)
            account = self._acct_of(order.intent) if order is not None else None
        events = list(self._pending.pop((account, order_id), []))
        if account is not None:
            # 계좌 없이 보관된 이벤트(계좌를 안 싣는 호출자 — 실제 LS 통보는 항상 계좌가 있다)도
            # 이어 붙임
            events += self._pending.pop((None, order_id), [])
        if not events:
            return
        for kind, fill, qty in events:
            if kind == "fill" and fill is not None:
                self.on_fill(fill)
            elif kind == "ack":
                self.on_ack(order_id, account=account)
            elif kind == "cancel":
                self.on_cancel(order_id, qty=qty, account=account)
            elif kind == "reject":
                self.on_reject(order_id, account=account)

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

    def order(self, order_id: str, account: Account | None = None) -> TrackedOrder | None:
        """주문 조회 — 계좌를 주면 그 계좌의 번호(결정 73). 없으면 HL → 유일한 LS 번호 순."""
        return self._find(order_id, account)

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
