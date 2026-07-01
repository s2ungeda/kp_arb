"""통합 드라이런 스모크 테스트 (BUILD_PLAN Phase 5-1). 라이브 호출 없음.

mock 게이트웨이 + NoopStrategy로 한 사이클: 시세→MarketState→리스크→상태저장→노출 데이터 전송.
검증: 주문 0건 / 노출 전송 1회 이상 / 상태 저장됨.
"""
from pathlib import Path

from kp_arb.domain.enums import Account, Instrument, Side, Underlying, Venue
from kp_arb.domain.models import Position, Quote
from kp_arb.fx_reporter import FXExposureReporter, Signal
from kp_arb.gateways.hl import Mark
from kp_arb.gateways.ls_ws import MarketStatus
from kp_arb.gateways.mock_hl import MockHLGateway
from kp_arb.gateways.mock_ls import MockLSGateway
from kp_arb.risk import RiskLimits, RiskManager
from kp_arb.runner import DryRunner
from kp_arb.session_service import SessionService
from kp_arb.state_store import StateStore
from kp_arb.strategy.noop import NoopStrategy

SAMSUNG = Underlying.SAMSUNG
SAMSUNG_CODE = Underlying.SAMSUNG.krx_code


class MockSink:
    def __init__(self) -> None:
        self.sent: list[Signal] = []

    async def send(self, signal: Signal) -> bool:
        self.sent.append(signal)
        return True


async def test_dry_run_cycle_smoke(tmp_path: Path) -> None:
    session = SessionService()
    ls = MockLSGateway()
    hl = MockHLGateway()
    # 녹화 포지션: 국내 현물 + HL perp(노출 발생).
    ls.seed_position(Position(venue=Venue.LS, instrument=Instrument.KR_STOCK, underlying=SAMSUNG,
                              side=Side.BUY, qty=100, avg_price=70_000, account=Account.KR_STOCK))
    ls.seed_balance(Account.KR_STOCK, 10_000_000)
    hl.seed_position(Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                              underlying=SAMSUNG, side=Side.SELL, qty=2, avg_price=52.0))
    sink = MockSink()
    reporter = FXExposureReporter(sink, token="tok")

    async with StateStore(str(tmp_path / "state.db")) as store:
        runner = DryRunner(
            session=session, strategy=NoopStrategy(), ls=ls, hl=hl,
            reporter=reporter, store=store, risk=RiskManager(RiskLimits()),
        )
        # 녹화 시세/장운영 주입.
        runner.feed_market_status(MarketStatus(tr_key=SAMSUNG_CODE, body={"jang_cd": "20"}))
        runner.feed_quote(Quote(underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
                                bid=69_900, ask=70_100, ts=1.0))
        runner.feed_mark(Mark(underlying=SAMSUNG, price=52.0))
        runner.set_fx(1_350.0)

        order_ids = await runner.run_cycle(ts=1.0)

        # 상태 저장 확인(같은 연결에서).
        positions = await store.load_positions()
        events = await store.load_events()

    # 1) 주문 0건 (NoopStrategy)
    assert order_ids == []
    assert ls.placed == [] and hl.placed == []
    # 2) 노출 전송 1회 이상 (total_coin = 국내 롱 명목: 주식 100*70000)
    assert len(sink.sent) >= 1
    assert sink.sent[0].total_coin == 7_000_000.0
    assert sink.sent[0].total_domestic == 0.0
    assert sink.sent[0].fx == 1_350.0
    assert reporter.last_sent_ok is True
    # 3) 상태 저장됨
    assert len(positions) == 2  # 국내 + HL 포지션 영속화
    assert any(e["component"] == "runner" for e in events)


async def test_dry_run_deadzone_no_reference(tmp_path: Path) -> None:
    # JIF 미수신(데드존)이면 레퍼런스 없음 → MarketState.reference None, 여전히 주문 0건.
    session = SessionService()
    ls = MockLSGateway()
    hl = MockHLGateway()
    reporter = FXExposureReporter(MockSink(), token="tok")

    async with StateStore(str(tmp_path / "state.db")) as store:
        runner = DryRunner(session=session, strategy=NoopStrategy(), ls=ls, hl=hl,
                           reporter=reporter, store=store, risk=RiskManager(RiskLimits()))
        order_ids = await runner.run_cycle(ts=1.0)

    assert order_ids == []
    assert reporter.last_sent_ok is True  # 노출 0이라도 전송은 됨
