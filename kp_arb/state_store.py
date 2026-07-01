"""StateStore — SQLite 영속화 + 재시작 복구 (DESIGN.md §10). aiosqlite 기반.

DESIGN §10 스키마 8종(positions/orders/fills/inventory/market_state/session_log/
fx_exposure_report/events)을 만든다. 재시작 시 positions + 미체결 orders + 최신 inventory로 복구.
라이브 무관(로컬 SQLite 파일). 테스트는 임시 DB로만.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite
from pydantic import BaseModel

from .domain.enums import Account, Instrument, OrderType, Side, Underlying
from .domain.models import Position

OPEN_STATUSES: frozenset[str] = frozenset({"open", "partial"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    underlying TEXT, venue TEXT, instrument TEXT, account TEXT,
    side TEXT, qty REAL, avg_price REAL, updated_at REAL,
    PRIMARY KEY (underlying, venue, instrument, account)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, venue TEXT, instrument TEXT, account TEXT,
    side TEXT, qty REAL, price REAL, type TEXT, status TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY, order_id TEXT, qty REAL, price REAL, fee REAL, ts REAL
);
CREATE TABLE IF NOT EXISTS inventory (
    ts REAL, underlying TEXT, signed_units REAL, krw_notional REAL,
    hl_notional REAL, net_delta REAL
);
CREATE TABLE IF NOT EXISTS market_state (
    ts REAL, underlying TEXT, ref_instrument TEXT, kr_price REAL, hl_mark REAL, fx REAL
);
CREATE TABLE IF NOT EXISTS session_log (
    ts REAL, underlying TEXT, instrument TEXT, tradeable INTEGER, is_reference INTEGER
);
CREATE TABLE IF NOT EXISTS fx_exposure_report (ts REAL, exposure_usd REAL, sent_ok INTEGER);
CREATE TABLE IF NOT EXISTS events (ts REAL, level TEXT, component TEXT, message TEXT);
"""


class OrderRecord(BaseModel):
    """orders 테이블 행 (DESIGN §10)."""

    order_id: str
    venue: str
    instrument: Instrument
    side: Side
    qty: float
    order_type: OrderType
    status: str
    ts: float
    account: Account | None = None
    price: float | None = None


class InventoryRow(BaseModel):
    """inventory 테이블 행 (DESIGN §10)."""

    ts: float
    underlying: Underlying
    signed_units: float
    krw_notional: float
    hl_notional: float
    net_delta: float


@dataclass
class RecoveryBundle:
    """재시작 복구 묶음: 포지션 + 미체결 주문 + 최신 인벤토리."""

    positions: list[Position]
    open_orders: list[OrderRecord]
    inventory: dict[Underlying, InventoryRow]


def _acct(account: Account | None) -> str:
    return account.value if account is not None else ""


class StateStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> StateStore:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _require(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("StateStore not connected")
        return self._db

    # --- positions ---

    async def save_position(self, position: Position, *, ts: float) -> None:
        await self._require().execute(
            "INSERT OR REPLACE INTO positions"
            "(underlying, venue, instrument, account, side, qty, avg_price, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                position.underlying.value,
                position.venue.value,
                position.instrument.value,
                _acct(position.account),
                position.side.value,
                position.qty,
                position.avg_price,
                ts,
            ),
        )
        await self._require().commit()

    async def load_positions(self) -> list[Position]:
        async with self._require().execute("SELECT * FROM positions") as cur:
            rows = await cur.fetchall()
        return [self._to_position(row) for row in rows]

    @staticmethod
    def _to_position(row: Any) -> Position:
        return Position(
            venue=row["venue"],
            instrument=row["instrument"],
            underlying=row["underlying"],
            side=row["side"],
            qty=row["qty"],
            avg_price=row["avg_price"],
            account=Account(row["account"]) if row["account"] else None,
        )

    # --- orders ---

    async def save_order(self, order: OrderRecord) -> None:
        await self._require().execute(
            "INSERT OR REPLACE INTO orders"
            "(order_id, venue, instrument, account, side, qty, price, type, status, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order.order_id,
                order.venue,
                order.instrument.value,
                _acct(order.account),
                order.side.value,
                order.qty,
                order.price,
                order.order_type.value,
                order.status,
                order.ts,
            ),
        )
        await self._require().commit()

    async def set_order_status(self, order_id: str, status: str) -> None:
        await self._require().execute(
            "UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id)
        )
        await self._require().commit()

    async def load_open_orders(self) -> list[OrderRecord]:
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        query = f"SELECT * FROM orders WHERE status IN ({placeholders})"
        async with self._require().execute(query, tuple(OPEN_STATUSES)) as cur:
            rows = await cur.fetchall()
        return [self._to_order(row) for row in rows]

    @staticmethod
    def _to_order(row: Any) -> OrderRecord:
        return OrderRecord(
            order_id=row["order_id"],
            venue=row["venue"],
            instrument=Instrument(row["instrument"]),
            account=Account(row["account"]) if row["account"] else None,
            side=Side(row["side"]),
            qty=row["qty"],
            price=row["price"],
            order_type=OrderType(row["type"]),
            status=row["status"],
            ts=row["ts"],
        )

    # --- fills / inventory ---

    async def add_fill(
        self, *, fill_id: str, order_id: str, qty: float, price: float, fee: float, ts: float
    ) -> None:
        await self._require().execute(
            "INSERT OR REPLACE INTO fills(fill_id, order_id, qty, price, fee, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (fill_id, order_id, qty, price, fee, ts),
        )
        await self._require().commit()

    async def add_inventory(self, row: InventoryRow) -> None:
        await self._require().execute(
            "INSERT INTO inventory"
            "(ts, underlying, signed_units, krw_notional, hl_notional, net_delta)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                row.ts,
                row.underlying.value,
                row.signed_units,
                row.krw_notional,
                row.hl_notional,
                row.net_delta,
            ),
        )
        await self._require().commit()

    async def latest_inventory(self, underlying: Underlying) -> InventoryRow | None:
        async with self._require().execute(
            "SELECT * FROM inventory WHERE underlying = ? ORDER BY ts DESC LIMIT 1",
            (underlying.value,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return InventoryRow(
            ts=row["ts"],
            underlying=row["underlying"],
            signed_units=row["signed_units"],
            krw_notional=row["krw_notional"],
            hl_notional=row["hl_notional"],
            net_delta=row["net_delta"],
        )

    # --- 로그성 테이블 (insert 위주) ---

    async def add_market_state(
        self,
        *,
        ts: float,
        underlying: Underlying,
        ref_instrument: Instrument | None,
        kr_price: float | None,
        hl_mark: float | None,
        fx: float | None,
    ) -> None:
        await self._require().execute(
            "INSERT INTO market_state(ts, underlying, ref_instrument, kr_price, hl_mark, fx)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                ts,
                underlying.value,
                ref_instrument.value if ref_instrument is not None else None,
                kr_price,
                hl_mark,
                fx,
            ),
        )
        await self._require().commit()

    async def add_session_log(
        self,
        *,
        ts: float,
        underlying: Underlying,
        instrument: Instrument,
        tradeable: bool,
        is_reference: bool,
    ) -> None:
        await self._require().execute(
            "INSERT INTO session_log(ts, underlying, instrument, tradeable, is_reference)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, underlying.value, instrument.value, int(tradeable), int(is_reference)),
        )
        await self._require().commit()

    async def add_fx_report(self, *, ts: float, exposure_usd: float, sent_ok: bool) -> None:
        await self._require().execute(
            "INSERT INTO fx_exposure_report(ts, exposure_usd, sent_ok) VALUES (?, ?, ?)",
            (ts, exposure_usd, int(sent_ok)),
        )
        await self._require().commit()

    async def log_event(self, *, ts: float, level: str, component: str, message: str) -> None:
        await self._require().execute(
            "INSERT INTO events(ts, level, component, message) VALUES (?, ?, ?, ?)",
            (ts, level, component, message),
        )
        await self._require().commit()

    async def load_events(self) -> list[dict[str, Any]]:
        async with self._require().execute("SELECT * FROM events ORDER BY ts") as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # --- 재시작 복구 ---

    async def recover(self) -> RecoveryBundle:
        positions = await self.load_positions()
        open_orders = await self.load_open_orders()
        inventory: dict[Underlying, InventoryRow] = {}
        for underlying in Underlying:
            latest = await self.latest_inventory(underlying)
            if latest is not None:
                inventory[underlying] = latest
        return RecoveryBundle(positions, open_orders, inventory)
