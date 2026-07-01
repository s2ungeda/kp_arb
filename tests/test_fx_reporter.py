"""FXExposureReporter 계약 테스트. 라이브 없음(mock sink). USD/KRW 주문 없음."""
from kp_arb.domain.enums import Account, Instrument, Side, Underlying, Venue
from kp_arb.domain.models import Position
from kp_arb.fx_reporter import ExposureReport, FXExposureReporter

SAMSUNG = Underlying.SAMSUNG


class MockSink:
    """발행된 노출 메시지를 기록하는 mock. sent_ok 반환값 설정 가능."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.published: list[ExposureReport] = []

    async def publish(self, report: ExposureReport) -> bool:
        self.published.append(report)
        return self.ok


def hl_pos(underlying: Underlying, side: Side, qty: float, avg: float) -> Position:
    return Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                    underlying=underlying, side=side, qty=qty, avg_price=avg)


def ls_pos() -> Position:
    return Position(venue=Venue.LS, instrument=Instrument.KR_STOCK, underlying=SAMSUNG,
                    side=Side.BUY, qty=100, avg_price=70_000, account=Account.KR_STOCK)


async def test_report_computes_and_publishes() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink, source_id="kp-arb")
    report = await reporter.report([hl_pos(SAMSUNG, Side.SELL, 2, 52.0)], ts=1.0)

    assert report.exposure_usd == -104.0  # signed -2 * 52
    assert report.source_id == "kp-arb"
    assert report.ts == 1.0
    assert sink.published == [report]  # mock sink로 발행됨
    assert reporter.last_sent_ok is True


async def test_marks_override_avg_price() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink, source_id="kp-arb")
    report = await reporter.report(
        [hl_pos(SAMSUNG, Side.BUY, 3, 52.0)], marks={SAMSUNG: 60.0}, ts=1.0
    )
    assert report.exposure_usd == 180.0  # 3 * 60 (mark override)


async def test_domestic_positions_excluded() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink, source_id="kp-arb")
    report = await reporter.report([ls_pos()], ts=1.0)
    assert report.exposure_usd == 0.0  # 국내(KRW) 포지션은 USD 노출 아님
    assert len(sink.published) == 1


async def test_report_if_changed_skips_unchanged() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink, source_id="kp-arb")
    positions = [hl_pos(SAMSUNG, Side.SELL, 2, 52.0)]
    first = await reporter.report_if_changed(positions, ts=1.0)
    second = await reporter.report_if_changed(positions, ts=2.0)

    assert first is not None
    assert second is None  # 변동 없으면 재발행 안 함
    assert len(sink.published) == 1


async def test_report_if_changed_publishes_on_change() -> None:
    sink = MockSink()
    reporter = FXExposureReporter(sink, source_id="kp-arb")
    await reporter.report_if_changed([hl_pos(SAMSUNG, Side.SELL, 2, 52.0)], ts=1.0)
    changed = await reporter.report_if_changed([hl_pos(SAMSUNG, Side.SELL, 3, 52.0)], ts=2.0)

    assert changed is not None
    assert changed.exposure_usd == -156.0  # -3 * 52
    assert len(sink.published) == 2


async def test_publish_failure_tracked() -> None:
    sink = MockSink(ok=False)
    reporter = FXExposureReporter(sink, source_id="kp-arb")
    await reporter.report([hl_pos(SAMSUNG, Side.SELL, 2, 52.0)], ts=1.0)
    assert reporter.last_sent_ok is False  # 발행 실패 감지(→ kill-switch 연동 가능)
