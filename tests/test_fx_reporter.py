"""FXExposureReporter 계약 테스트. 라이브 없음(mock sink). USD/KRW 주문 없음."""
from kp_arb.domain.enums import Account, Instrument, Side, Underlying, Venue
from kp_arb.domain.models import Position
from kp_arb.fx_reporter import FXExposureReporter, Signal

SAMSUNG = Underlying.SAMSUNG


class MockSink:
    """전송된 Signal을 기록하는 mock. sent_ok 반환값 설정 가능."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[Signal] = []

    async def send(self, signal: Signal) -> bool:
        self.sent.append(signal)
        return self.ok


def dom(instrument: Instrument, qty: float, avg: float, side: Side = Side.BUY) -> Position:
    spot = instrument in (Instrument.KR_STOCK, Instrument.KR_ETF)
    acct = Account.KR_STOCK if spot else Account.KR_DERIV
    return Position(venue=Venue.LS, instrument=instrument, underlying=SAMSUNG,
                    side=side, qty=qty, avg_price=avg, account=acct)


def hl(side: Side = Side.SELL) -> Position:
    return Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP, underlying=SAMSUNG,
                    side=side, qty=2, avg_price=52.0)


async def test_report_computes_total_coin() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink, token="secret")
    positions = [
        dom(Instrument.KR_STOCK, 100, 70_000),        # 100*70000*1 = 7,000,000
        dom(Instrument.KR_STOCK_FUTURE, 2, 71_000),   # 2*71000*10  = 1,420,000
        dom(Instrument.KR_ETF, 10, 50_000),           # 10*50000*2  = 1,000,000
    ]
    signal = await reporter.report(positions, fx=1_350.0, id="s1", datetime="2026-07-01")

    assert signal.total_coin == 9_420_000.0
    assert signal.total_domestic == 0.0
    assert signal.fx == 1_350.0
    assert signal.token == "secret"
    assert signal.id == "s1"
    assert sink.sent == [signal]
    assert reporter.last_sent_ok is True


async def test_hl_and_short_excluded() -> None:
    reporter = FXExposureReporter(MockSink())
    positions = [hl(Side.SELL), dom(Instrument.KR_STOCK, 5, 70_000, side=Side.SELL)]
    signal = await reporter.report(positions, fx=1_350.0, id="s1")
    assert signal.total_coin == 0.0  # HL 제외 + 국내 숏 제외(롱만)


async def test_multipliers_override() -> None:
    reporter = FXExposureReporter(MockSink(), multipliers={Instrument.KR_ETF: 1.0})
    signal = await reporter.report([dom(Instrument.KR_ETF, 10, 50_000)], fx=1_350.0, id="s1")
    assert signal.total_coin == 500_000.0  # 10*50000*1 (오버라이드)


async def test_id_generated_when_omitted() -> None:
    reporter = FXExposureReporter(MockSink())
    signal = await reporter.report([], fx=1_350.0)
    assert signal.id  # 자동 생성(uuid), 비어있지 않음


async def test_report_if_changed_skips_unchanged() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink)
    positions = [dom(Instrument.KR_STOCK, 100, 70_000)]
    first = await reporter.report_if_changed(positions, fx=1_350.0, id="a")
    second = await reporter.report_if_changed(positions, fx=1_350.0, id="b")
    assert first is not None
    assert second is None  # total_coin 불변 → 재전송 안 함
    assert len(sink.sent) == 1


async def test_report_if_changed_publishes_on_change() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink)
    await reporter.report_if_changed([dom(Instrument.KR_STOCK, 100, 70_000)], fx=1_350.0, id="a")
    changed = await reporter.report_if_changed(
        [dom(Instrument.KR_STOCK, 200, 70_000)], fx=1_350.0, id="b"
    )
    assert changed is not None and changed.total_coin == 14_000_000.0
    assert len(sink.sent) == 2


async def test_send_failure_tracked() -> None:
    reporter = FXExposureReporter(MockSink(ok=False))
    await reporter.report([dom(Instrument.KR_STOCK, 1, 70_000)], fx=1_350.0, id="s1")
    assert reporter.last_sent_ok is False
