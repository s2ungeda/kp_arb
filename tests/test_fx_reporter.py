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
    # total_coin = HL 보유종목 Σ(평균단가×수량) (사용자 확정 2026-07-24), token 기본 Meme
    sink = MockSink()
    reporter = FXExposureReporter(sink)
    positions = [
        hl(Side.SELL),                                # 2 * 52 = 104 (HL)
        dom(Instrument.KR_STOCK, 100, 70_000),        # 국내 — total_coin 제외
        dom(Instrument.KR_STOCK_FUTURE, 2, 71_000),   # 국내 — 제외
    ]
    signal = await reporter.report(positions, fx=1_350.0, id="s1", datetime="2026-07-01")

    assert signal.total_coin == 104.0
    assert signal.total_domestic == 0.0
    assert signal.fx == 1_350.0
    assert signal.token == "Meme"
    assert signal.id == "s1"
    assert sink.sent == [signal]
    assert reporter.last_sent_ok is True


async def test_only_hl_counts() -> None:
    # HL 명목만 집계 — 국내 포지션은 롱/숏 무관하게 제외
    reporter = FXExposureReporter(MockSink())
    positions = [dom(Instrument.KR_STOCK, 5, 70_000),
                 dom(Instrument.KR_STOCK_FUTURE, 3, 71_000, side=Side.SELL)]
    signal = await reporter.report(positions, fx=1_350.0, id="s1")
    assert signal.total_coin == 0.0  # HL 없음


async def test_id_generated_when_omitted() -> None:
    reporter = FXExposureReporter(MockSink())
    signal = await reporter.report([], fx=1_350.0)
    assert signal.id  # 자동 생성(uuid), 비어있지 않음


async def test_report_if_changed_skips_unchanged() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink)
    positions = [hl(Side.SELL)]
    first = await reporter.report_if_changed(positions, fx=1_350.0, id="a")
    second = await reporter.report_if_changed(positions, fx=1_350.0, id="b")
    assert first is not None
    assert second is None  # total_coin 불변 → 재전송 안 함
    assert len(sink.sent) == 1


async def test_report_if_changed_publishes_on_change() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink)
    small = Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                     underlying=SAMSUNG, side=Side.SELL, qty=2, avg_price=52.0)
    big = Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                   underlying=SAMSUNG, side=Side.SELL, qty=5, avg_price=52.0)
    await reporter.report_if_changed([small], fx=1_350.0, id="a")
    changed = await reporter.report_if_changed([big], fx=1_350.0, id="b")
    assert changed is not None and changed.total_coin == 260.0  # 5*52
    assert len(sink.sent) == 2


async def test_send_failure_tracked() -> None:
    reporter = FXExposureReporter(MockSink(ok=False))
    await reporter.report([hl(Side.SELL)], fx=1_350.0, id="s1")
    assert reporter.last_sent_ok is False
