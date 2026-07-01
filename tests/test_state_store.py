"""StateStore 계약 테스트. 임시 DB로 저장→재로딩 복구 / 미체결 주문 복구."""
from pathlib import Path

from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import Position
from kp_arb.state_store import InventoryRow, OrderRecord, StateStore

SAMSUNG = Underlying.SAMSUNG


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def ls_position() -> Position:
    return Position(venue=Venue.LS, instrument=Instrument.KR_STOCK, underlying=SAMSUNG,
                    side=Side.BUY, qty=100, avg_price=70_000, account=Account.KR_STOCK)


def hl_position() -> Position:
    return Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP, underlying=SAMSUNG,
                    side=Side.SELL, qty=2, avg_price=52.0)


def order(order_id: str, status: str) -> OrderRecord:
    return OrderRecord(
        order_id=order_id, venue="ls", instrument=Instrument.KR_STOCK, account=Account.KR_STOCK,
        side=Side.BUY, qty=10, price=70_000, order_type=OrderType.LIMIT, status=status, ts=1.0,
    )


async def test_positions_persist_across_reopen(tmp_path: Path) -> None:
    path = _db(tmp_path)
    async with StateStore(path) as store:
        await store.save_position(ls_position(), ts=1.0)
        await store.save_position(hl_position(), ts=1.0)

    # 재시작(새 인스턴스로 재오픈) 후 복구.
    async with StateStore(path) as store2:
        positions = await store2.load_positions()

    assert len(positions) == 2
    ls = next(p for p in positions if p.venue is Venue.LS)
    hl = next(p for p in positions if p.venue is Venue.HYPERLIQUID)
    assert ls.account is Account.KR_STOCK and ls.qty == 100
    assert hl.account is None and hl.side is Side.SELL and hl.avg_price == 52.0


async def test_position_upsert_no_duplicate(tmp_path: Path) -> None:
    async with StateStore(_db(tmp_path)) as store:
        await store.save_position(ls_position(), ts=1.0)
        await store.save_position(ls_position(), ts=2.0)  # 같은 키 → 갱신
        positions = await store.load_positions()
    assert len(positions) == 1


async def test_open_orders_recovery(tmp_path: Path) -> None:
    path = _db(tmp_path)
    async with StateStore(path) as store:
        await store.save_order(order("O1", "open"))
        await store.save_order(order("O2", "filled"))
        await store.save_order(order("O3", "cancelled"))
        await store.save_order(order("O4", "partial"))

    async with StateStore(path) as store2:
        open_orders = await store2.load_open_orders()

    assert {o.order_id for o in open_orders} == {"O1", "O4"}  # open/partial만


async def test_set_order_status_updates_open_set(tmp_path: Path) -> None:
    async with StateStore(_db(tmp_path)) as store:
        await store.save_order(order("O1", "open"))
        await store.set_order_status("O1", "filled")
        assert await store.load_open_orders() == []


async def test_latest_inventory(tmp_path: Path) -> None:
    async with StateStore(_db(tmp_path)) as store:
        await store.add_inventory(InventoryRow(ts=1.0, underlying=SAMSUNG, signed_units=1,
                                               krw_notional=7_000_000, hl_notional=100,
                                               net_delta=0.1))
        await store.add_inventory(InventoryRow(ts=2.0, underlying=SAMSUNG, signed_units=3,
                                               krw_notional=21_000_000, hl_notional=300,
                                               net_delta=0.3))
        latest = await store.latest_inventory(SAMSUNG)
    assert latest is not None and latest.ts == 2.0 and latest.signed_units == 3
    assert latest.underlying is SAMSUNG


async def test_recover_bundle(tmp_path: Path) -> None:
    path = _db(tmp_path)
    async with StateStore(path) as store:
        await store.save_position(hl_position(), ts=1.0)
        await store.save_order(order("O1", "open"))
        await store.add_inventory(InventoryRow(ts=5.0, underlying=SAMSUNG, signed_units=2,
                                               krw_notional=0, hl_notional=104, net_delta=0.2))

    async with StateStore(path) as store2:
        bundle = await store2.recover()

    assert len(bundle.positions) == 1
    assert [o.order_id for o in bundle.open_orders] == ["O1"]
    assert bundle.inventory[SAMSUNG].signed_units == 2


async def test_event_log(tmp_path: Path) -> None:
    async with StateStore(_db(tmp_path)) as store:
        await store.log_event(ts=1.0, level="WARN", component="hl", message="disconnect")
        await store.log_event(ts=2.0, level="INFO", component="ls", message="reconnected")
        events = await store.load_events()
    assert [e["message"] for e in events] == ["disconnect", "reconnected"]
