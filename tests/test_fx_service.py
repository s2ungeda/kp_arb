"""FX 보고 서비스 테스트 — 제어·스냅샷 (소켓·실전송 없이 sink mock)."""
from typing import Any

from kp_arb.domain.enums import Instrument, Side, Underlying, Venue
from kp_arb.domain.models import Position
from kp_arb.fx_reporter import Signal
from kp_arb.fx_service import FxReportService


class _StubBook:
    def __init__(self, positions: list[Position]) -> None:
        self._positions = positions

    def positions(self) -> list[Position]:
        return self._positions


class _StubSystem:
    def __init__(self, positions: list[Position], fx: float) -> None:
        self.order_book = _StubBook(positions)
        self._fx = fx

    def usdkrw_effective(self, now: Any = None) -> tuple[float, str]:
        return self._fx, "현물"


def _hl(qty: float, avg: float) -> Position:
    return Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                    underlying=Underlying.SAMSUNG, side=Side.SELL, qty=qty, avg_price=avg)


class _MockSink:
    def __init__(self) -> None:
        self.sent: list[Signal] = []

    async def send(self, signal: Signal) -> bool:
        self.sent.append(signal)
        return True

    def peer_list(self) -> list[dict[str, object]]:
        return [{"name": "감시", "ip": "10.0.0.5", "port": 5001}]


def test_control_and_snapshot() -> None:
    svc = FxReportService(_StubSystem([_hl(2, 52.0)], 1385.0))  # type: ignore[arg-type]
    svc.pause()
    assert svc.paused and svc.snapshot()["paused"] is True
    svc.resume()
    assert not svc.paused
    svc.set_interval(0.1)  # 하한 0.5로 클램프
    assert svc.interval_s == 0.5
    svc.request_send_now()
    assert svc._want_send_now


async def test_send_records_last_and_log() -> None:
    svc = FxReportService(_StubSystem([_hl(2, 52.0)], 1385.0))  # type: ignore[arg-type]
    svc._sink = _MockSink()  # type: ignore[assignment]
    svc._reporter._sink = svc._sink  # type: ignore[attr-defined]
    await svc._send(force=True)
    # total_coin = HL 명목(2×52=104) × 환율(1385) = 원화
    assert svc.last_signal is not None
    assert svc.last_signal.total_coin == 104.0 * 1385.0
    assert svc.last_signal.token == "Meme"
    snap = svc.snapshot()
    assert snap["last"]["total_coin"] == 104.0 * 1385.0
    assert snap["peers"][0]["name"] == "감시"
    assert snap["hl"][0]["notional"] == 104.0 * 1385.0  # 화면 구성도 원화
    assert len(snap["log"]) == 1
