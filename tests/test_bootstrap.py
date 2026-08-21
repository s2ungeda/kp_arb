"""부트스트랩 계약 테스트 — 시동(스냅샷→실시간 결선)과 선물 월물 선택. 라이브 없음."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from kp_arb.bootstrap import HLAmendForbidden, LiveSystem, select_near_month_futures
from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent, Position
from kp_arb.gateways.ls_ws import LSWebSocketClient, WSClosed
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


def test_record_fill_appends_from_applied_fill() -> None:
    # 체결내역 보관 — 실제 적용 시점(order+체결량)으로 기록(종목·방향 함께).
    from collections import deque
    from types import SimpleNamespace

    ob = OrderBook()
    order = ob.track("O1", OrderIntent(
        venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
        instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.1,
        order_type=OrderType.LIMIT, price=163.0))
    sys = SimpleNamespace(order_book=ob, fills=deque(maxlen=200))
    LiveSystem._record_fill(sys, order, 0.1, 163.5, "F1")  # type: ignore[arg-type]
    assert len(sys.fills) == 1
    assert sys.fills[0]["side"] == "sell"
    assert sys.fills[0]["qty"] == 0.1 and sys.fills[0]["price"] == 163.5


def test_taker_immediate_fill_recorded_once_via_on_fill_applied() -> None:
    # 회귀(2026-08-20): taker 즉시체결(apply_place_fill)이 체결내역에 1회 잡히고,
    # 뒤늦은 userFills 재통보는 흡수돼 중복 안 됨 — 통보 타이밍 경합과 무관.
    from collections import deque
    from types import SimpleNamespace

    from kp_arb.gateways.ls_ws import Fill

    ob = OrderBook()
    sys = SimpleNamespace(order_book=ob, fills=deque(maxlen=200))
    ob.on_fill_applied.append(
        lambda o, q, p, fid: LiveSystem._record_fill(sys, o, q, p, fid))  # type: ignore[arg-type]
    ob.track("O1", OrderIntent(
        venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
        instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.1,
        order_type=OrderType.LIMIT, price=163.0))
    ob.apply_place_fill(Fill(fill_id="place-O1", order_id="O1", qty=0.1,
                             price=163.5, fee=0.0, ts=0.0))
    assert len(sys.fills) == 1 and sys.fills[0]["qty"] == 0.1  # 즉시체결 기록됨
    ob.on_fill(Fill(fill_id="tid-1", order_id="O1", qty=0.1, price=163.5, fee=0.0, ts=0.0))
    assert len(sys.fills) == 1  # 재통보 흡수 → 중복 없음


def test_record_cancel_captures_time_and_intent() -> None:
    # 취소내역 보관 — 취소된(잔여) 수량·종목·방향·시각을 담는다(주문 리스트 '취소' 행).
    from collections import deque
    from types import SimpleNamespace

    ob = OrderBook()
    order = ob.track("O9", OrderIntent(
        venue=Venue.HYPERLIQUID, underlying=Underlying.SK_HYNIX,
        instrument=Instrument.HL_PERP, side=Side.BUY, qty=0.05,
        order_type=OrderType.LIMIT, price=1400.0))
    sys = SimpleNamespace(cancels=deque(maxlen=200))
    LiveSystem._record_cancel(sys, order)
    assert len(sys.cancels) == 1
    c = sys.cancels[0]
    assert c["side"] == "buy" and c["qty"] == 0.05 and c["time"]  # 시각 채워짐


async def test_guarded_ws_restarts_then_stops(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # graceful close(run 정상 반환) → 재시작(재연결) / WSClosed → 종료 / 예외 → 포기.
    orig_sleep = asyncio.sleep
    monkeypatch.setattr("kp_arb.bootstrap.asyncio.sleep", lambda *_: orig_sleep(0))
    calls = {"n": 0}

    async def make_run() -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            return          # graceful close → 재시작
        raise WSClosed      # 커넥터 종료 → 재시작 안 함

    await LiveSystem._guarded_ws("HL", make_run)
    assert calls["n"] == 3  # 정상반환 2회 재시작 후 WSClosed로 종료(무한 아님)


async def test_guarded_ws_gives_up_on_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 예외(설정·인증 등)면 재시작 안 하고 그 채널만 포기(무한 재시도 방지).
    calls = {"n": 0}

    async def make_run() -> None:
        calls["n"] += 1
        raise RuntimeError("auth 실패")

    await LiveSystem._guarded_ws("선물", make_run)  # 예외 삼키고 반환
    assert calls["n"] == 1  # 1회만 — 재시작 안 함


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
        raise WSClosed  # 프레임 소진 = 세션 종료(테스트) — _guarded_ws 재시작 루프를 끊는다

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


def test_ws_statuses_collects_present_clients() -> None:
    # WS 세션 현황(Phase 8-3c) — 살아있는 채널만 모으고 없는 채널(HL)은 건너뛴다.
    system, _, _ = _system([], deriv_frames=[])
    statuses = system.ws_statuses()
    assert len(statuses) == 2  # LS 주식 + LS 선물 (HL 미설정 → 제외)
    assert all(s.venue == "LS" for s in statuses)
    assert [s.to_dict()["connected"] for s in statuses] == [False, False]  # 시동 전


def test_fx_price_only_near_month_feeds_theory() -> None:
    # §9.1 — 근·차근 둘 다 저장하되, 환율이론가는 최근월물로만 갱신(차근에 섞이지 않게).
    system, _, _ = _system([])
    system._fx_futures = ("175W07", 202607)
    system._fx_months = [("175W07", 202607), ("175W08", 202608)]

    system._apply_fx_price("175W08", 1600.0)  # 차근월물 먼저
    assert system.fx_futures_price["175W08"] == 1600.0
    assert system.usdkrw_theory is None       # 차근은 이론가에 안 먹임
    assert system.usdkrw_futures is None

    system._apply_fx_price("175W07", 1530.0)  # 최근월물
    assert system.fx_futures_price["175W07"] == 1530.0
    assert system.usdkrw_futures == 1530.0
    assert system.usdkrw_theory is not None    # 최근월물만 이론가 갱신


async def test_ws_reconnect_triggers_resync() -> None:
    # Phase 8-4b — 재연결 훅이 OrderBook 재스냅샷(refresh_snapshot)을 백그라운드로 부른다.
    system, _, _ = _system([], deriv_frames=[])
    system._wire()  # on_reconnect 콜백 등록
    calls: list[int] = []

    async def spy() -> None:
        calls.append(1)

    system.refresh_snapshot = spy  # type: ignore[method-assign]
    system._stock_ws.on_reconnect[0]()  # 재연결 발화(동기) → 백그라운드 재동기 태스크
    for task in list(system._bg):
        await task
    assert calls == [1]


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
    assert {"H1_", "UH1", "JIF", "SC0", "SC1"} <= trs  # NXT는 통합(UH1)로 수신
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


async def test_hl_daily_limit_blocks_over_limit() -> None:
    # DESIGN-settings §1 — place(길목)에서 당일 체결액 + 이 주문 금액 > 한도면 거부(수동·전략 공통).
    from kp_arb.gateways.hl_ws import HLWebSocketClient
    from kp_arb.gateways.mock_hl import MockHLGateway
    from kp_arb.limits import DailyLimitExceeded

    system = LiveSystem(
        gateway=MockLSGateway(),  # type: ignore[arg-type]
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=MockHLGateway(),
        hl_ws=HLWebSocketClient(FakeConnector([])),
    )
    await system.start()
    system.set_hl_daily_limit(1000.0)

    def _buy(qty: float) -> OrderIntent:
        return OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                           instrument=Instrument.HL_PERP, side=Side.BUY, qty=qty,
                           order_type=OrderType.LIMIT, price=1500.0)

    assert await system.place(_buy(0.5))          # 0 + 750 ≤ 1000 → 통과
    with pytest.raises(DailyLimitExceeded):
        await system.place(_buy(1.0))             # 0 + 1500 > 1000 → 거부
    system.set_hl_daily_limit(0.0)                # 0 = 무제한
    assert await system.place(_buy(1.0))          # 이제 통과
    await system.wait()


async def test_amend_price_forbids_hl() -> None:
    # HL은 어떤 경우에도 정정 금지 — amend_price(유일 정정 라우팅)가 하드 거부한다.
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
        instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.1,
        order_type=OrderType.LIMIT, price=100.0))
    with pytest.raises(HLAmendForbidden):
        await system.amend_price(oid, 101.0)
    await system.wait()


async def test_attach_engine_uses_realtime_positions_and_place() -> None:
    # 엔진 연결: 포지션=OrderBook 실시간 값, 주문=place(등록 포함), 시세 콜백 연결.
    from collections.abc import Sequence as _Seq

    from kp_arb.domain.models import MarketState
    from kp_arb.strategy.base import Strategy

    captured: list[MarketState] = []

    class OneShotBuy(Strategy):
        """첫 호출에만 LS 매수 1건 — 이후 재주문 없음(중복 방지 확인용 아님, 단순화)."""

        def __init__(self) -> None:
            self.fired = False

        def evaluate(self, state: MarketState) -> _Seq[OrderIntent]:
            captured.append(state)
            if self.fired or state.underlying is not SAMSUNG:
                return []
            self.fired = True
            return [OrderIntent(venue=Venue.LS, underlying=SAMSUNG,
                                instrument=Instrument.KR_STOCK, side=Side.BUY,
                                qty=10, order_type=OrderType.MARKET)]

    system, _, _ = _system([_fill_frame("LS-1")])
    engine = system.attach_engine(OneShotBuy())
    await system.start()          # 일괄 조회: 삼성 100주 → OrderBook
    await system.place(OrderIntent(venue=Venue.LS, underlying=SAMSUNG,
                                   instrument=Instrument.KR_STOCK, side=Side.BUY,
                                   qty=10, order_type=OrderType.MARKET))  # "LS-1" 등록
    await system.wait()           # 체결 프레임 반영 → 110주

    await system.run_strategy_loop(engine, interval_s=0.0, max_cycles=1)

    # 엔진이 받은 MarketState의 포지션 = OrderBook 실시간 값(110주)
    samsung_states = [s for s in captured if s.underlying is SAMSUNG]
    assert samsung_states and samsung_states[0].positions[0].qty == 110
    # 전략 주문이 place 경유 → OrderBook에 자동 등록됨 ("LS-2")
    assert system.order_book.order("LS-2") is not None
    # 리스크 상태가 OrderBook의 "실시간" 잔고를 참조 — 체결(10주×70,000)이 즉시 차감됨
    assert engine.risk_state.account_available_funds[Account.KR_STOCK] == 5_000_000 - 700_000


async def test_strategy_loop_noop_places_nothing() -> None:
    from kp_arb.strategy.noop import NoopStrategy

    system, _, _ = _system([])
    engine = system.attach_engine(NoopStrategy())
    await system.start()
    await system.wait()
    await system.run_strategy_loop(engine, interval_s=0.0, max_cycles=3)
    assert system.order_book.open_orders() == []  # 주문 0건


async def test_deriv_ws_subscribes_futures_fills_only() -> None:
    system, _, deriv_connector = _system([], deriv_frames=[])
    await system.start()
    await system.wait()
    assert deriv_connector is not None
    trs = {json.loads(m)["body"]["tr_cd"] for m in deriv_connector.conn.sent}
    assert trs == {"O01", "C01", "H01"}  # 파생 WS는 선물 통보만
    types = {json.loads(m)["header"]["tr_type"] for m in deriv_connector.conn.sent}
    assert types == {"1"}  # 계좌 등록

def test_usdkrw_effective_spot_window() -> None:
    # 주간 창(07:50~18:10) 안이고 외환현물이 있으면 현물, 아니면 선물이론가.
    from datetime import datetime

    system, _, _ = _system([])
    system.usdkrw_theory = 1_500.0
    day = datetime(2026, 7, 20, 10, 0)
    assert system.usdkrw_effective(day) == (1_500.0, "선물이론")  # 현물 미수신 → 이론가
    system.usdkrw_spot = 1_498.5
    assert system.usdkrw_effective(datetime(2026, 7, 20, 7, 50)) == (1_498.5, "현물")
    assert system.usdkrw_effective(day) == (1_498.5, "현물")
    assert system.usdkrw_effective(datetime(2026, 7, 20, 18, 10)) == (1_500.0, "선물이론")


def test_disparity_board_computes_pairs() -> None:
    # DESIGN §6.1: HL 환산 disp vs 국내(SF/ETF) disp → 진입/청산 스프레드.
    from kp_arb.domain.enums import SessionPhase
    from kp_arb.domain.models import Quote
    from kp_arb.etf_theory import EtfTheoryInputs

    system, _, _ = _system([])
    system.futures_symbols[SAMSUNG] = "A1167000"
    system.futures_expiry[SAMSUNG] = 202612  # 먼 만기 — 테스트 안정성
    system.etf_symbols[SAMSUNG] = "0193W0"
    system.usdkrw_theory = 1_500.0
    system.trades[(SAMSUNG, Instrument.KR_STOCK, "krx")] = 300_000.0  # 기초 현재가
    system.stock_change_pct[(SAMSUNG, "krx")] = 0.0  # 기초 등락률(drate) 0%
    system.session.seed_phase(SessionPhase.REGULAR)  # 정규장 공식 사용
    system.etf_theory[SAMSUNG] = EtfTheoryInputs(prev_nav=20_000.0, leverage=2.0)
    system.quotes[(SAMSUNG, Instrument.HL_PERP, "hl")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.HL_PERP,
        bid=201.0, ask=202.0, ts=0.0, market="hl",
    )
    system.quotes[(SAMSUNG, Instrument.KR_STOCK_FUTURE, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        bid=301_000.0, ask=302_000.0, ts=0.0,
    )
    system.quotes[(SAMSUNG, Instrument.KR_ETF, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_ETF,
        bid=20_000.0, ask=20_050.0, ts=0.0,
    )
    system.quotes[(SAMSUNG, Instrument.KR_STOCK, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
        bid=299_500.0, ask=300_500.0, ts=0.0,
    )

    board = system.disparity_board()

    sf = board[(SAMSUNG, Instrument.KR_STOCK_FUTURE)]
    # HL 환산: bid 301,500 / ask 303,000, 기초 300,000 → disp +0.5% / +1.0%
    assert sf.hl.bid is not None and abs(sf.hl.bid - 0.005) < 1e-9
    assert sf.hl.ask is not None and abs(sf.hl.ask - 0.010) < 1e-9
    # SF 이론가 = 300,000 × (1 + 3.5% × 잔존일/365) > 300,000 → disp는 그 대비
    assert sf.kr.bid is not None and sf.spread.entry is not None
    # 국내 maker 기준(meme.xlsx): 진입 = HL매수d − 국내매수d / 청산 = HL매도d − 국내매도d
    assert sf.spread.entry == sf.hl.bid - sf.kr.bid
    assert sf.spread.exit == (sf.hl.ask or 0) - (sf.kr.ask or 0)

    etf = board[(SAMSUNG, Instrument.KR_ETF)]
    # ETF 이론가 = 20,000(기초 등락률 0) → ask 20,050 disp +0.25% (인프라 유지 확인용)
    assert etf.kr.ask is not None and abs(etf.kr.ask - 0.0025) < 1e-9
    assert etf.spread.exit == (etf.hl.ask or 0) - (etf.kr.ask or 0)

    st = board[(SAMSUNG, Instrument.KR_STOCK)]
    # 주식 쌍: 기준가 = 자기 현재가 300,000 (이론가 없음 — 옛 엑셀 현대차 AE62 패턴)
    assert st.kr.bid is not None and abs(st.kr.bid - (-500 / 300_000)) < 1e-12
    assert st.kr.ask is not None and abs(st.kr.ask - (500 / 300_000)) < 1e-12
    assert st.spread.entry == st.hl.bid - st.kr.bid  # 진입 공식 동일 (maker 기준)
    assert st.kr_last is not None and abs(st.kr_last) < 1e-12  # 현재가 괴리는 항상 0


def test_pair_signal_est_based() -> None:
    import pytest

    # 7-3a: 진입 = HL매수d(est) − 국내매수d / 청산 = HL매도d(est) − 국내매도d.
    # 주식 쌍(기준가=자기 현재가 300,000, 환율 1,500)으로 검산.
    from kp_arb.domain.models import Quote

    system, _, _ = _system([])
    system.usdkrw_theory = 1_500.0
    system.trades[(SAMSUNG, Instrument.KR_STOCK, "krx")] = 300_000.0
    system.quotes[(SAMSUNG, Instrument.KR_STOCK, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
        bid=299_500.0, ask=300_500.0, ts=0.0)
    system.quotes[(SAMSUNG, Instrument.HL_PERP, "hl")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.HL_PERP,
        bid=201.0, ask=202.0, ts=0.0, market="hl",
        bids=[(201.0, 3.0), (200.0, 100.0)],
        asks=[(202.0, 3.0), (203.0, 100.0)])

    entry, exit_ = system.pair_signal(SAMSUNG, Instrument.KR_STOCK, 5, 5)
    # est(매수쪽, 5계약) = (201×3 + 200×2)/5 = 200.6 → 환산 300,900 → HL disp +0.003
    # 국내 매수d = (299,500−300,000)/300,000 = −1/600 → entry = 0.003 + 1/600
    assert entry == pytest.approx(0.003 + 1 / 600)
    # est(매도쪽) = (202×3 + 203×2)/5 = 202.4 → 303,600 → +0.012, 국내 매도d = +1/600
    assert exit_ == pytest.approx(0.012 - 1 / 600)

    # 수량이 커지면 est가 나빠져 진입 신호는 줄어든다 (2호가까지 파고듦)
    entry_big, _ = system.pair_signal(SAMSUNG, Instrument.KR_STOCK, 50, 50)
    assert entry_big is not None and entry is not None and entry_big < entry


async def test_fx_auction_places_hedge_on_new_stock_future() -> None:
    # 원달러선물 동시호가 대응: 삼성 주식선물 신규주문 접수(O01) → KR_FX 대응주문 발주.
    from kp_arb.fx_auction import FxAuctionSettings
    o01 = json.dumps({"header": {"tr_cd": "O01"}, "body": {
        "fnoIsuno": "A1169000", "bnstp": "2", "ordqty": "20",
        "ordprc": "142150", "ordno": "2224", "orgordno": "0"}})
    system, _, _ = _system([], deriv_frames=[o01])
    system.futures_symbols = {SAMSUNG: "A1169000"}  # 코드→종목 매칭용
    system.start_fx_auction(FxAuctionSettings(
        windows=(("00:00", "23:59"),), fx_code="175X9000",  # 항상 시간창 안
        price=1421.5, tick=10, hedge_ratio=0.5))
    await system.start()
    await system.wait()
    if system._bg:  # 백그라운드 발주 태스크 완료 대기
        await asyncio.gather(*list(system._bg))
    # 삼성 매수 20계약 @142150 → 원달러선물 매도 1계약 @1420.5
    assert system._gw.fx_placed == [("175X9000", Side.SELL, 1, 1420.5)]
    assert system.fx_hedges and system.fx_hedges[0]["status"] == "접수"


async def test_fx_auction_ignores_when_stopped_or_amend() -> None:
    # 실행 안 함 + 정정(orgordno≠0)이면 대응 안 함.
    new = json.dumps({"header": {"tr_cd": "O01"}, "body": {
        "fnoIsuno": "A1169000", "bnstp": "2", "ordqty": "20",
        "ordprc": "142150", "ordno": "2224", "orgordno": "0"}})
    amend = json.dumps({"header": {"tr_cd": "O01"}, "body": {
        "fnoIsuno": "A1169000", "bnstp": "2", "ordqty": "20",
        "ordprc": "142150", "ordno": "2232", "orgordno": "2224"}})  # 정정
    system, _, _ = _system([], deriv_frames=[new, amend])
    system.futures_symbols = {SAMSUNG: "A1169000"}
    # start_fx_auction 호출 안 함 → 실행중 아님
    await system.start()
    await system.wait()
    if system._bg:
        await asyncio.gather(*list(system._bg))
    assert system._gw.fx_placed == []  # 미실행이라 대응 없음


def test_set_carry_rates_replaces_theory_rates() -> None:
    # 공통설정 이자율 주입 — 환율(fx)·주식선물(eq) 연이자율이 이론가 계산에 반영된다.
    system, _, _ = _system([])
    system.set_carry_rates(fx=0.02, eq=0.04)
    assert system._carry.fx == 0.02
    assert system._carry.stock_futures == 0.04


def test_monitor_snapshot_structure() -> None:
    # 시세 모니터 스냅샷 — 코어가 LS/HL 표 + 괴리보드 + 환율·잔고·장운영을 조립(모니터는 렌더만).
    from kp_arb.core_server import monitor_snapshot

    assert monitor_snapshot(None, 1, 0.0, 0.0) == {"connected": False}
    system, _, _ = _system([])
    snap = monitor_snapshot(system, 1, 0.0, 0.0)
    assert snap["connected"] is True
    assert set(snap) >= {"fx", "phase", "balances", "ls", "hl", "board"}
    assert all("theory" in r and "disp" in r for r in snap["ls"])   # LS 행: 이론가·괴리
    assert all("mark" in r and "oracle" in r for r in snap["hl"])   # HL 행: 마크·오라클
