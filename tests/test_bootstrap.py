"""부트스트랩 계약 테스트 — 시동(스냅샷→실시간 결선)과 선물 월물 선택. 라이브 없음."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from kp_arb.bootstrap import LiveSystem, select_near_month_futures
from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent, Position
from kp_arb.gateways.ls_ws import LSWebSocketClient
from kp_arb.gateways.mock_ls import MockLSGateway
from kp_arb.order_book import OrderBook, OrderStatus
from kp_arb.session_service import SessionService

SAMSUNG = Underlying.SAMSUNG


# --- 선물 최근월물 선택 (t8401 실측 shape) ---

MASTER_ROWS = [
    {"hname": "삼성전자   F 202608", "shcode": "A1168000", "basecode": "A005930"},
    {"hname": "삼성전자   F 202607", "shcode": "A1167000", "basecode": "A005930"},
    {"hname": "삼성전자   F 202703", "shcode": "A1173000", "basecode": "A005930"},
    {"hname": "삼성전자   SP 2607-2", "shcode": "D116768S", "basecode": "A005930"},  # 스프레드 제외
    {"hname": "현대차     F 202607", "shcode": "A1667000", "basecode": "A005380"},
    {"hname": "SK하이닉스 F 202607", "shcode": "A5067000", "basecode": "A000660"},
    {"hname": "카카오     F 202607", "shcode": "A9997000", "basecode": "A035720"},  # 대상 외
]


def test_select_near_month_futures() -> None:
    symbols = select_near_month_futures(MASTER_ROWS)
    assert symbols == {
        Underlying.SAMSUNG: "A1167000",   # 202607 < 202608 < 202703
        Underlying.HYUNDAI: "A1667000",
        Underlying.SK_HYNIX: "A5067000",
    }


def test_select_ignores_spread_and_unknown() -> None:
    rows = [
        {"hname": "삼성전자   SP 2607-2", "shcode": "D116768S", "basecode": "A005930"},
        {"hname": "카카오     F 202607", "shcode": "A9997000", "basecode": "A035720"},
    ]
    assert select_near_month_futures(rows) == {}


# --- LiveSystem 시동 (mock 게이트웨이 + 가짜 WS) ---


class FakeConnection:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def _gen(self) -> AsyncIterator[str]:
        for frame in self.frames:
            yield frame

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen()


class FakeConnector:
    def __init__(self, frames: list[str]) -> None:
        self.conn = FakeConnection(frames)

    async def connect(self) -> FakeConnection:
        return self.conn


def _fill_frame(order_id: str) -> str:
    return json.dumps({"header": {"tr_cd": "SC1"},
                       "body": {"execno": "1", "ordno": order_id, "execqty": "10",
                                "execprc": "70000", "exectime": "100000000"}})


def _system(
    stock_frames: list[str], deriv_frames: list[str] | None = None
) -> tuple[LiveSystem, FakeConnector, FakeConnector | None]:
    gw = MockLSGateway()
    gw.seed_balance(Account.KR_STOCK, 5_000_000)
    gw.seed_position(Position(venue=Venue.LS, instrument=Instrument.KR_STOCK,
                              underlying=SAMSUNG, side=Side.BUY, qty=100,
                              avg_price=290_000, account=Account.KR_STOCK))
    stock_connector = FakeConnector(stock_frames)
    deriv_connector = FakeConnector(deriv_frames) if deriv_frames is not None else None
    system = LiveSystem(
        gateway=gw,  # type: ignore[arg-type]  # LSGateway 계약만 사용
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=LSWebSocketClient(stock_connector),
        deriv_ws=(LSWebSocketClient(deriv_connector)
                  if deriv_connector is not None else None),
    )
    return system, stock_connector, deriv_connector


async def test_start_loads_snapshot_then_streams() -> None:
    intent = OrderIntent(venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
                         side=Side.BUY, qty=10, order_type=OrderType.MARKET)
    system, connector, _ = _system([])
    oid = await system.place(intent)  # 주문 등록(track)
    await system.start()

    # 1) 최초 스냅샷이 OrderBook에 로드됨
    assert system.order_book.balance(Account.KR_STOCK) == 5_000_000
    assert system.order_book.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 100
    assert system.order_book.order(oid) is not None
    await system.wait()  # 프레임 소진 → 정상 종료

    # 구독 등록 확인: 시세(3종)+JIF+주식 체결통보 (선물 통보는 파생 WS 몫)
    trs = {json.loads(m)["body"]["tr_cd"] for m in connector.conn.sent}
    assert {"H1_", "NH1", "JIF", "SC0", "SC1"} <= trs
    assert "O01" not in trs


async def test_fill_frame_updates_order_book_realtime() -> None:
    intent = OrderIntent(venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
                         side=Side.BUY, qty=10, order_type=OrderType.MARKET)
    system, _, _ = _system([_fill_frame("LS-1")])  # MockLSGateway의 첫 주문번호
    oid = await system.place(intent)  # start 전에 track(체결 프레임과의 race 방지)
    assert oid == "LS-1"
    await system.start()
    await system.wait()  # 프레임 재생 완료

    order = system.order_book.order("LS-1")
    assert order is not None and order.status is OrderStatus.FILLED
    assert system.order_book.position_qty(SAMSUNG, Instrument.KR_STOCK,
                                          Account.KR_STOCK) == 110  # 100(스냅샷)+10(체결)


async def test_session_init_env_seeds_phase(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from kp_arb.domain.enums import SessionPhase

    monkeypatch.setenv("KP_SESSION_INIT", "regular")
    system, _, _ = _system([])
    await system.start()
    await system.wait()
    assert system.session.phase_for(SAMSUNG) is SessionPhase.REGULAR


async def test_session_init_invalid_stays_dead(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from kp_arb.domain.enums import SessionPhase

    monkeypatch.setenv("KP_SESSION_INIT", "bogus")
    system, _, _ = _system([])
    await system.start()
    await system.wait()
    assert system.session.phase_for(SAMSUNG) is SessionPhase.DEAD  # 보수 유지


async def test_hl_slot_snapshot_marks_and_fills() -> None:
    # HL 슬롯: 스냅샷 포지션 합류 + 마크 fan-out + HL 체결 → OrderBook.
    import json as _json

    from kp_arb.gateways.hl_ws import HLWebSocketClient
    from kp_arb.gateways.mock_hl import MockHLGateway

    hl_gw = MockHLGateway()
    hl_gw.seed_position(Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                                 underlying=SAMSUNG, side=Side.SELL, qty=0.1, avg_price=184.0))
    hl_fill = _json.dumps({"channel": "userFills", "data": {"fills": [
        {"coin": "xyz:SMSN", "px": "185.0", "sz": "0.2", "side": "A",
         "oid": 777, "tid": 1, "time": 1.0}]}})
    mark = _json.dumps({"channel": "activeAssetCtx",
                        "data": {"coin": "xyz:SMSN", "ctx": {"markPx": "184.5"}}})
    hl_ws = HLWebSocketClient(FakeConnector([mark, hl_fill]))

    gw = MockLSGateway()
    system = LiveSystem(
        gateway=gw,  # type: ignore[arg-type]
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=hl_gw,
        hl_ws=hl_ws,
    )
    hl_intent = OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                            instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.2,
                            order_type=OrderType.MARKET)
    marks: list[float] = []
    system.on_mark.append(lambda m: marks.append(m.price))
    system.order_book.track("777", hl_intent)  # HL 체결 매칭용
    await system.start()
    await system.wait()

    # 스냅샷: HL 포지션 합류 (숏 0.1)
    assert system.order_book.position_qty(SAMSUNG, Instrument.HL_PERP) == -0.1 - 0.2
    assert marks == [184.5]  # 마크 fan-out
    assert system.order_book.order("777").filled_qty == 0.2  # HL 체결 반영


async def test_place_routes_hl_to_hl_gateway() -> None:
    from kp_arb.gateways.hl_ws import HLWebSocketClient
    from kp_arb.gateways.mock_hl import MockHLGateway

    hl_gw = MockHLGateway()
    system = LiveSystem(
        gateway=MockLSGateway(),  # type: ignore[arg-type]
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=hl_gw,
        hl_ws=HLWebSocketClient(FakeConnector([])),
    )
    await system.start()
    oid = await system.place(OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                                         instrument=Instrument.HL_PERP, side=Side.SELL,
                                         qty=0.1, order_type=OrderType.MARKET))
    assert oid.startswith("HL-") and len(hl_gw.placed) == 1
    await system.wait()


async def test_deriv_ws_subscribes_futures_fills_only() -> None:
    system, _, deriv_connector = _system([], deriv_frames=[])
    await system.start()
    await system.wait()
    assert deriv_connector is not None
    trs = {json.loads(m)["body"]["tr_cd"] for m in deriv_connector.conn.sent}
    assert trs == {"O01", "C01", "H01"}  # 파생 WS는 선물 통보만
    types = {json.loads(m)["header"]["tr_type"] for m in deriv_connector.conn.sent}
    assert types == {"1"}  # 계좌 등록