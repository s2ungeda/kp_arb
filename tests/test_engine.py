"""ArbEngine 계약 테스트. mock 게이트웨이 입력 → MarketState 조립 / Noop → 주문 0건."""
from collections.abc import Sequence

from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import MarketState, OrderIntent, Position, Quote
from kp_arb.engine import ArbEngine
from kp_arb.gateways.hl import Mark
from kp_arb.gateways.mock_hl import MockHLGateway
from kp_arb.gateways.mock_ls import MockLSGateway
from kp_arb.session_service import SessionService
from kp_arb.strategy.base import Strategy
from kp_arb.strategy.noop import NoopStrategy

SAMSUNG_CODE = Underlying.SAMSUNG.krx_code


class CapturingStrategy(Strategy):
    """받은 MarketState를 기록하고 주문은 내지 않는다."""

    def __init__(self) -> None:
        self.states: list[MarketState] = []

    def evaluate(self, state: MarketState) -> Sequence[OrderIntent]:
        self.states.append(state)
        return []


class RoutingStrategy(Strategy):
    """고정 LS/HL 주문 1건씩 반환(라우팅 검증용)."""

    def evaluate(self, state: MarketState) -> Sequence[OrderIntent]:
        return [
            OrderIntent(
                venue=Venue.LS,
                underlying=Underlying.SAMSUNG,
                instrument=Instrument.KR_STOCK,
                side=Side.BUY,
                qty=10,
                order_type=OrderType.MARKET,
            ),
            OrderIntent(
                venue=Venue.HYPERLIQUID,
                underlying=Underlying.SAMSUNG,
                instrument=Instrument.HL_PERP,
                side=Side.SELL,
                qty=1,
                order_type=OrderType.MARKET,
            ),
        ]


def test_build_market_state_assembles_inputs() -> None:
    from kp_arb.gateways.ls_ws import MarketStatus

    session = SessionService()
    session.on_market_status(MarketStatus(tr_key="0", body={"jangubun": "1", "jstatus": "21"}))
    engine = ArbEngine(
        session=session, strategy=NoopStrategy(), ls=MockLSGateway(), hl=MockHLGateway()
    )
    engine.on_quote(Quote(underlying=Underlying.SAMSUNG, instrument=Instrument.KR_STOCK,
                          bid=69_900, ask=70_100, ts=1.0))
    engine.on_mark(Mark(underlying=Underlying.SAMSUNG, price=52.0))
    engine.set_fx(1_350.0)

    positions = [
        Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                 underlying=Underlying.SAMSUNG, side=Side.SELL, qty=1, avg_price=52.0),
        Position(venue=Venue.LS, instrument=Instrument.KR_STOCK, underlying=Underlying.HYUNDAI,
                 side=Side.BUY, qty=5, avg_price=200_000, account=Account.KR_STOCK),
    ]
    state = engine.build_market_state(Underlying.SAMSUNG, positions)

    assert state.underlying is Underlying.SAMSUNG
    assert state.reference_instrument is Instrument.KR_STOCK  # 정규장 레퍼런스
    assert state.reference_price_krw == 70_000  # (69900+70100)/2
    assert state.hl_mark_usd == 52.0
    assert state.usdkrw == 1_350.0
    assert state.session[Instrument.KR_STOCK].tradeable is True
    assert len(state.positions) == 1  # SAMSUNG만
    assert state.positions[0].underlying is Underlying.SAMSUNG


async def test_noop_places_no_orders() -> None:
    ls = MockLSGateway()
    hl = MockHLGateway()
    engine = ArbEngine(session=SessionService(), strategy=NoopStrategy(), ls=ls, hl=hl)
    result = await engine.run_once()
    assert result == {u: [] for u in Underlying}
    assert ls.placed == []
    assert hl.placed == []


async def test_routes_intents_to_correct_gateways() -> None:
    ls = MockLSGateway()
    hl = MockHLGateway()
    engine = ArbEngine(session=SessionService(), strategy=RoutingStrategy(), ls=ls, hl=hl)
    order_ids = await engine.step(Underlying.SAMSUNG)
    assert len(order_ids) == 2
    assert len(ls.placed) == 1 and ls.placed[0].instrument is Instrument.KR_STOCK
    assert ls.placed[0].account is Account.KR_STOCK  # 라우팅 계약 유지
    assert len(hl.placed) == 1 and hl.placed[0].instrument is Instrument.HL_PERP


async def test_risk_blocks_routing() -> None:
    from kp_arb.risk import RiskLimits, RiskManager, RiskState

    ls = MockLSGateway()
    hl = MockHLGateway()
    engine = ArbEngine(
        session=SessionService(),
        strategy=RoutingStrategy(),
        ls=ls,
        hl=hl,
        risk=RiskManager(RiskLimits()),
    )
    engine.risk_state = RiskState(kill_switch=True)  # 전부 차단
    order_ids = await engine.step(Underlying.SAMSUNG)
    assert order_ids == []
    assert ls.placed == [] and hl.placed == []


async def test_step_collects_positions_from_both_venues() -> None:
    strategy = CapturingStrategy()
    ls = MockLSGateway()
    hl = MockHLGateway()
    ls.seed_position(Position(venue=Venue.LS, instrument=Instrument.KR_STOCK,
                              underlying=Underlying.SAMSUNG, side=Side.BUY, qty=100,
                              avg_price=70_000, account=Account.KR_STOCK))
    hl.seed_position(Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                              underlying=Underlying.SAMSUNG, side=Side.SELL, qty=2, avg_price=52.0))
    engine = ArbEngine(session=SessionService(), strategy=strategy, ls=ls, hl=hl)

    await engine.step(Underlying.SAMSUNG)

    assert len(strategy.states) == 1
    assert len(strategy.states[0].positions) == 2  # LS + HL 둘 다 수집(SAMSUNG)
