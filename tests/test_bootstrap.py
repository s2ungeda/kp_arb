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

    entry, exit_ = system.pair_signal(SAMSUNG, Instrument.KR_STOCK, 5)
    # est(매수쪽, 5계약) = (201×3 + 200×2)/5 = 200.6 → 환산 300,900 → HL disp +0.003
    # 국내 매수d = (299,500−300,000)/300,000 = −1/600 → entry = 0.003 + 1/600
    assert entry == pytest.approx(0.003 + 1 / 600)
    # est(매도쪽) = (202×3 + 203×2)/5 = 202.4 → 303,600 → +0.012, 국내 매도d = +1/600
    assert exit_ == pytest.approx(0.012 - 1 / 600)

    # 수량이 커지면 est가 나빠져 진입 신호는 줄어든다 (2호가까지 파고듦)
    entry_big, _ = system.pair_signal(SAMSUNG, Instrument.KR_STOCK, 50)
    assert entry_big is not None and entry is not None and entry_big < entry
