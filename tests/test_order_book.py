"""OrderBook 계약 테스트 — 스냅샷 1회 + 이후 이벤트로만 상태·포지션·잔고 갱신."""
from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent, Position
from kp_arb.gateways.ls_ws import Fill
from kp_arb.order_book import OrderBook, OrderStatus, TrackedOrder

SAMSUNG = Underlying.SAMSUNG


def intent(side: Side = Side.BUY, qty: float = 10,
           instrument: Instrument = Instrument.KR_STOCK) -> OrderIntent:
    return OrderIntent(venue=Venue.LS, underlying=SAMSUNG, instrument=instrument,
                       side=side, qty=qty, order_type=OrderType.MARKET)


def fill(order_id: str, qty: float, price: float, *, fee: float = 0.0,
         fill_id: str = "F1") -> Fill:
    return Fill(fill_id=fill_id, order_id=order_id, qty=qty, price=price, fee=fee, ts=1.0)


# --- 스냅샷 (최초 실행/온디맨드) ---


def test_snapshot_initializes_state() -> None:
    ob = OrderBook()
    ob.load_snapshot(
        positions=[Position(venue=Venue.LS, instrument=Instrument.KR_STOCK,
                            underlying=SAMSUNG, side=Side.BUY, qty=100, avg_price=290_000,
                            account=Account.KR_STOCK)],
        balances={Account.KR_STOCK: 5_000_000},
        open_orders=[TrackedOrder("100", intent(qty=5), status=OrderStatus.ACCEPTED)],
    )
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 100
    assert ob.balance(Account.KR_STOCK) == 5_000_000
    assert [o.order_id for o in ob.open_orders()] == ["100"]


def test_snapshot_removes_stale_phantom_keeps_recent() -> None:
    # HL은 취소·체결 통보가 없어, '적' 재조회 시 스냅샷에 사라진 미체결(phantom)을 정리한다.
    # 단 방금 낸 주문은 아직 거래소 미체결조회에 안 뜰 수 있어 유예 내면 보존.
    ob = OrderBook()
    stale = ob.track("OLD", intent())
    stale.placed_ts = 0.0  # 아주 오래됨(monotonic 기준 유예 밖) → phantom
    ob.track("NEW", intent())  # 방금(placed_ts=now) → 유예 안
    ob.load_snapshot(open_orders=())  # 거래소엔 아무 미체결도 없음
    assert ob.order("OLD") is None       # 오래된 phantom 정리
    assert ob.order("NEW") is not None    # 방금 낸 것은 보존


def test_snapshot_keeps_exchange_orders() -> None:
    # 스냅샷(거래소 실측)에 있으면 나이와 무관하게 유지.
    ob = OrderBook()
    stale = ob.track("EX", intent())
    stale.placed_ts = 0.0
    ob.load_snapshot(open_orders=[TrackedOrder("EX", intent(), status=OrderStatus.ACCEPTED)])
    assert ob.order("EX") is not None


def test_snapshot_preserves_orders_of_unreconciled_account() -> None:
    # 한 계좌 조회 실패(reconcile 목록에서 빠짐) → 그 계좌의 살아있는 미체결은 보존.
    ob = OrderBook()
    s = ob.track("S1", intent(instrument=Instrument.KR_STOCK))
    s.placed_ts = 0.0  # 오래됨(유예 밖)
    d = ob.track("D1", intent(instrument=Instrument.KR_STOCK_FUTURE))
    d.placed_ts = 0.0
    # KR_STOCK만 조회 성공(빈 미체결) — KR_DERIV는 실패라 정리 대상 아님.
    ob.load_snapshot(open_orders=(), reconcile_accounts={Account.KR_STOCK})
    assert ob.order("S1") is None      # 조회 성공 계좌 → phantom 정리
    assert ob.order("D1") is not None   # 조회 실패 계좌 → 보존


def test_replace_positions_only_touches_instrument() -> None:
    # 체결 후 HL 포지션만 실측으로 교체 — LS·잔고·주문은 유지, 목록에 없는 HL 종목은 청산.
    ob = OrderBook()
    ob.load_snapshot(positions=[
        Position(venue=Venue.LS, instrument=Instrument.KR_STOCK, underlying=SAMSUNG,
                 side=Side.BUY, qty=100, avg_price=70_000, account=Account.KR_STOCK),
        Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP, underlying=SAMSUNG,
                 side=Side.BUY, qty=0.1, avg_price=163.0),
    ])
    ob.replace_positions([
        Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                 underlying=Underlying.SK_HYNIX, side=Side.SELL, qty=0.05, avg_price=1400.0),
    ], instrument=Instrument.HL_PERP)
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 0            # 청산(목록 없음)
    assert ob.position_qty(Underlying.SK_HYNIX, Instrument.HL_PERP, None) == -0.05  # 신규
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 100   # LS 유지


def test_on_fill_ignores_duplicate_after_full_fill() -> None:
    # 발주 즉시체결을 반영해 전량 체결된 뒤, userFills 중복 통보가 와도 초과체결 안 된다.
    ob = OrderBook()
    ob.track("O1", OrderIntent(
        venue=Venue.HYPERLIQUID, underlying=SAMSUNG, instrument=Instrument.HL_PERP,
        side=Side.SELL, qty=0.1, order_type=OrderType.LIMIT, price=165.0))
    ob.on_fill(fill("O1", 0.1, 168.0))  # 전량 체결
    assert ob.order("O1").status is OrderStatus.FILLED
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == -0.1
    ob.on_fill(fill("O1", 0.1, 168.0, fill_id="F2"))  # 중복(같은 체결 재통보) → 무시
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == -0.1  # 초과 안 됨


def _hl_intent(side: Side, qty: float) -> OrderIntent:
    return OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                       instrument=Instrument.HL_PERP, side=side, qty=qty,
                       order_type=OrderType.LIMIT, price=100.0)


def test_place_fill_absorbs_duplicate_userfill_partial() -> None:
    # 부분 즉시체결: 발주응답 3/5 선반영 → 같은 3을 userFills가 재통보해도 이중 반영 안 됨.
    ob = OrderBook()
    ob.track("O1", _hl_intent(Side.BUY, 5))
    ob.apply_place_fill(fill("O1", 3, 100.0, fill_id="place-O1"))  # 발주응답 선반영
    assert ob.order("O1").filled_qty == 3
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 3
    assert ob.order("O1").status is OrderStatus.PARTIAL

    ob.on_fill(fill("O1", 3, 100.0, fill_id="hl1"))  # 같은 체결 userFills 재통보 → 흡수
    assert ob.order("O1").filled_qty == 3                            # 6 아님
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 3   # 이중 반영 안 됨
    assert ob.order("O1").status is OrderStatus.PARTIAL

    ob.on_fill(fill("O1", 2, 101.0, fill_id="hl2"))  # 잔여 2 진짜 체결
    assert ob.order("O1").filled_qty == 5
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 5
    assert ob.order("O1").status is OrderStatus.FILLED


def test_place_fill_absorb_across_split_userfills() -> None:
    # 선반영 3을 userFills가 2+1로 쪼개 재통보 → 각각 흡수(총 3), 이중 반영 없음.
    ob = OrderBook()
    ob.track("O1", _hl_intent(Side.BUY, 5))
    ob.apply_place_fill(fill("O1", 3, 100.0, fill_id="place-O1"))
    ob.on_fill(fill("O1", 2, 100.0, fill_id="hl1"))  # 2 흡수(잔여 선반영 1)
    ob.on_fill(fill("O1", 1, 100.0, fill_id="hl2"))  # 1 흡수(선반영 0)
    assert ob.order("O1").filled_qty == 3
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 3


def test_place_fill_absorb_partial_when_userfill_batches_more() -> None:
    # userFills 한 건이 즉시체결분(선반영 3)+잔여 1 = 4를 묶어 오면: 3 흡수, 1만 신규 반영.
    ob = OrderBook()
    ob.track("O1", _hl_intent(Side.BUY, 5))
    ob.apply_place_fill(fill("O1", 3, 100.0, fill_id="place-O1"))
    ob.on_fill(fill("O1", 4, 100.0, fill_id="hl1"))
    assert ob.order("O1").filled_qty == 4
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 4


# --- 상태 전이 (이벤트로만) ---


def test_lifecycle_ack_partial_filled() -> None:
    ob = OrderBook()
    ob.track("1", intent(qty=10))
    assert ob.order("1").status is OrderStatus.NEW

    ob.on_ack("1")
    assert ob.order("1").status is OrderStatus.ACCEPTED

    ob.on_fill(fill("1", qty=4, price=290_000))
    o = ob.order("1")
    assert o.status is OrderStatus.PARTIAL
    assert o.filled_qty == 4 and o.remaining_qty == 6

    ob.on_fill(fill("1", qty=6, price=291_000, fill_id="F2"))
    o = ob.order("1")
    assert o.status is OrderStatus.FILLED
    assert o.filled_qty == 10
    assert o.avg_fill_price == (4 * 290_000 + 6 * 291_000) / 10
    assert ob.open_orders() == []


def test_cancel_and_reject() -> None:
    ob = OrderBook()
    ob.track("1", intent())
    ob.track("2", intent())
    ob.on_cancel("1")
    ob.on_reject("2")
    assert ob.order("1").status is OrderStatus.CANCELLED
    assert ob.order("2").status is OrderStatus.REJECTED
    assert ob.open_orders() == []


def test_ws_order_event_dispatch() -> None:
    # WS OrderEvent(SC0 접수/SC3 취소)가 상태 전이로 연결됨 — 취소 통보는 원주문(orgordno) 대상.
    from kp_arb.gateways.ls_ws import OrderEvent

    ob = OrderBook()
    ob.track("9852", intent())
    ob.on_order_event(OrderEvent(kind="ack", order_id="9852"))
    assert ob.order("9852").status is OrderStatus.ACCEPTED

    ob.on_order_event(OrderEvent(kind="cancel", order_id="9901", org_order_id="9852"))
    assert ob.order("9852").status is OrderStatus.CANCELLED


def test_unknown_order_fill_ignored() -> None:
    ob = OrderBook()
    assert ob.on_fill(fill("999", qty=1, price=100)) is None
    assert ob.positions() == []


# --- 포지션 증분 (체결 즉시) ---


def test_fill_creates_and_averages_position() -> None:
    ob = OrderBook()
    ob.track("1", intent(qty=10))
    ob.on_fill(fill("1", qty=10, price=290_000))
    ob.track("2", intent(qty=10))
    ob.on_fill(fill("2", qty=10, price=292_000))

    positions = ob.positions()
    assert len(positions) == 1
    assert positions[0].qty == 20
    assert positions[0].avg_price == 291_000  # 가중평균
    assert positions[0].side is Side.BUY


def test_sell_fill_reduces_then_closes() -> None:
    ob = OrderBook()
    ob.track("1", intent(qty=10))
    ob.on_fill(fill("1", qty=10, price=290_000))

    ob.track("2", intent(side=Side.SELL, qty=4))
    ob.on_fill(fill("2", qty=4, price=295_000))
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 6
    assert ob.positions()[0].avg_price == 290_000  # 감소는 평단 유지

    ob.track("3", intent(side=Side.SELL, qty=6))
    ob.on_fill(fill("3", qty=6, price=295_000))
    assert ob.positions() == []  # 청산


def test_fill_reverses_direction() -> None:
    # 선물: 롱 2 → 매도 5 체결 → 숏 3(평단=체결가).
    ob = OrderBook()
    ob.track("1", intent(qty=2, instrument=Instrument.KR_STOCK_FUTURE))
    ob.on_fill(fill("1", qty=2, price=71_000))
    ob.track("2", intent(side=Side.SELL, qty=5, instrument=Instrument.KR_STOCK_FUTURE))
    ob.on_fill(fill("2", qty=5, price=72_000))

    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK_FUTURE, Account.KR_DERIV) == -3
    pos = ob.positions()[0]
    assert pos.side is Side.SELL and pos.qty == 3
    assert pos.avg_price == 72_000


# --- 잔고 증분 (체결 즉시) ---


def test_balance_updates_on_fill() -> None:
    ob = OrderBook()
    ob.load_snapshot(balances={Account.KR_STOCK: 1_000_000})
    ob.track("1", intent(qty=2))
    ob.on_fill(fill("1", qty=2, price=100_000, fee=100))
    assert ob.balance(Account.KR_STOCK) == 1_000_000 - 200_000 - 100  # 매수: 차감+수수료

    ob.track("2", intent(side=Side.SELL, qty=1))
    ob.on_fill(fill("2", qty=1, price=110_000))
    assert ob.balance(Account.KR_STOCK) == 799_900 + 110_000  # 매도: 가산


def test_hl_fill_skips_balance() -> None:
    # HL(계좌 없음)은 KR 현금 잔고 증분 대상 아님(마진 모델) — 포지션만.
    ob = OrderBook()
    oi = OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                     instrument=Instrument.HL_PERP, side=Side.SELL, qty=1,
                     order_type=OrderType.MARKET)
    ob.track("1", oi)
    ob.on_fill(fill("1", qty=1, price=52.0))
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP) == -1
    assert ob.balance(Account.KR_STOCK) == 0.0


# --- 주문 역전(reordering) 대비: 미아 이벤트 버퍼 + replay (LS·HL 공용) ---

def test_fill_before_track_is_buffered_then_applied() -> None:
    # 체결이 track보다 먼저 와도 track 시 replay로 반영(taker 즉시체결 누락 방지).
    ob = OrderBook()
    assert ob.on_fill(fill("O1", 10, 100.0)) is None   # 미아 → 버퍼
    ob.track("O1", intent(qty=10))
    ob.replay_pending("O1")
    order = ob.order("O1")
    assert order is not None and order.filled_qty == 10
    assert order.status is OrderStatus.FILLED


def test_cancel_before_track_is_buffered_then_applied() -> None:
    ob = OrderBook()
    ob.on_cancel("O2")                                  # 취소가 먼저 → 버퍼
    ob.track("O2", intent())
    ob.replay_pending("O2")
    order = ob.order("O2")
    assert order is not None and order.status is OrderStatus.CANCELLED


def test_ack_before_track_is_buffered_then_applied() -> None:
    ob = OrderBook()
    ob.on_ack("O3")                                     # 접수가 먼저 → 버퍼
    ob.track("O3", intent())
    ob.replay_pending("O3")
    order = ob.order("O3")
    assert order is not None and order.status is OrderStatus.ACCEPTED


def test_replay_absorbs_duplicate_with_provisional() -> None:
    # 즉시체결(apply_place_fill) 뒤 replay되는 같은 체결은 provisional로 흡수 — 이중 반영 없음.
    ob = OrderBook()
    ob.on_fill(fill("O4", 0.1, 100.0))                  # WS 체결이 먼저 → 버퍼
    ob.track("O4", _hl_intent(Side.SELL, 0.1))
    ob.apply_place_fill(fill("O4", 0.1, 100.0, fill_id="place-O4"))  # 응답 즉시체결
    ob.replay_pending("O4")                             # 버퍼된 WS 체결 → 흡수
    order = ob.order("O4")
    assert order is not None and order.filled_qty == 0.1   # 0.2 아님(이중 없음)


def test_pending_buffer_is_bounded() -> None:
    ob = OrderBook()
    for i in range(OrderBook._PENDING_CAP + 50):        # 외부 주문 이벤트 무한 누적 방지
        ob.on_fill(fill(f"X{i}", 1, 1.0))
    assert len(ob._pending) <= OrderBook._PENDING_CAP
