"""리허설 판정 루프 테스트 (7-3a) — 발주 없음, 판정·누적·딜레이·가드."""
from datetime import datetime
from typing import Any

from kp_arb.core_engine import RehearsalEngine, should_fire
from kp_arb.strategy_core import Block, CoreState, ScreenKind

NOON = datetime(2026, 7, 23, 12, 0)


class StubSystem:
    """판정에 필요한 최소 표면 — 신호와 est/주문가를 고정 반환."""

    def __init__(self, entry: float | None, exit_: float | None) -> None:
        self.signals = (entry, exit_)

    def pair_signal(self, u: Any, inst: Any, qty: int) -> tuple[Any, Any]:
        return self.signals

    def est_pair_prices(self, u: Any, inst: Any, qty: int,
                        s_en: float, s_ex: float) -> tuple[Any, Any, Any, Any]:
        return 200.0, 201.0, 300_000.0, 301_000.0


def _state(*, entry_th: float = 0.0006, delay_ms: int = 500) -> CoreState:
    state = CoreState()
    screen = state.screens[ScreenKind.AUTO_M]
    screen.per_order_qty = 5
    screen.settings.max_position = 100
    screen.settings.delay_ms = delay_ms
    screen.entry_sets[0].threshold = entry_th
    screen.entry_sets[0].target_qty = 100
    screen.entry_sets[0].running = True
    return state


def test_should_fire_directions() -> None:
    assert should_fire(Block.ENTRY, 0.001, 0.0006)      # 진입: 신호 ≥ 기준
    assert not should_fire(Block.ENTRY, 0.0004, 0.0006)
    assert should_fire(Block.EXIT, -0.001, 0.0)         # 청산: 신호 ≤ 기준
    assert not should_fire(Block.EXIT, 0.001, 0.0)


def test_entry_fires_and_accumulates() -> None:
    state = _state()
    engine = RehearsalEngine(state, StubSystem(0.001, 0.005))  # type: ignore[arg-type]
    engine.tick(NOON, mono=0.0)
    screen = state.screens[ScreenKind.AUTO_M]
    assert screen.entry_sets[0].fired_qty == 5           # 1회주문수량만큼 발주 기록
    assert engine.runtime[ScreenKind.AUTO_M].virtual_position == 5


def test_delay_gates_next_fire() -> None:
    state = _state(delay_ms=500)
    engine = RehearsalEngine(state, StubSystem(0.001, 0.005))  # type: ignore[arg-type]
    engine.tick(NOON, mono=0.0)
    engine.tick(NOON, mono=0.2)   # 500ms 안 지남 — 발주 없음
    assert state.screens[ScreenKind.AUTO_M].entry_sets[0].fired_qty == 5
    engine.tick(NOON, mono=0.6)   # 딜레이 경과 — 다시 발주
    assert state.screens[ScreenKind.AUTO_M].entry_sets[0].fired_qty == 10


def test_below_threshold_or_stopped_no_fire() -> None:
    state = _state(entry_th=0.002)
    engine = RehearsalEngine(state, StubSystem(0.001, 0.005))  # type: ignore[arg-type]
    engine.tick(NOON, mono=0.0)   # 신호 0.1% < 기준 0.2%
    assert state.screens[ScreenKind.AUTO_M].entry_sets[0].fired_qty == 0
    state.screens[ScreenKind.AUTO_M].entry_sets[0].threshold = 0.0006
    state.screens[ScreenKind.AUTO_M].entry_sets[0].running = False
    engine.tick(NOON, mono=1.0)   # 실행 꺼짐 — 발주 없음
    assert state.screens[ScreenKind.AUTO_M].entry_sets[0].fired_qty == 0


def test_operating_window_guard() -> None:
    state = _state()
    engine = RehearsalEngine(state, StubSystem(0.001, 0.005))  # type: ignore[arg-type]
    engine.tick(datetime(2026, 7, 23, 16, 0), mono=0.0)  # 자동M 8:45~15:35 밖
    assert state.screens[ScreenKind.AUTO_M].entry_sets[0].fired_qty == 0


def test_exit_reduces_virtual_position() -> None:
    state = _state()
    screen = state.screens[ScreenKind.AUTO_M]
    screen.exit_sets[0].threshold = 0.0
    screen.exit_sets[0].target_qty = 100
    screen.exit_sets[0].running = True
    engine = RehearsalEngine(state, StubSystem(0.0001, -0.001))  # entry 미달, exit 충족
    engine.tick(NOON, mono=0.0)
    assert screen.entry_sets[0].fired_qty == 0
    assert screen.exit_sets[0].fired_qty == 5
    assert engine.runtime[ScreenKind.AUTO_M].virtual_position == -5  # SF 숏 스프레드


def test_target_completion_stops_firing() -> None:
    state = _state(delay_ms=0)
    state.screens[ScreenKind.AUTO_M].entry_sets[0].target_qty = 8  # 5 + 3(잔여)
    engine = RehearsalEngine(state, StubSystem(0.001, 0.005))  # type: ignore[arg-type]
    engine.tick(NOON, mono=0.0)
    engine.tick(NOON, mono=1.0)
    engine.tick(NOON, mono=2.0)  # 목표 완료 후엔 더 안 나감
    assert state.screens[ScreenKind.AUTO_M].entry_sets[0].fired_qty == 8
