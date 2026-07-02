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
