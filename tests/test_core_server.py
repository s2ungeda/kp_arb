"""코어 API 테스트 — 명령 적용(순수) + HTTP 왕복 + 저장/복원 (DESIGN §12, §6.2)."""
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from kp_arb.core_server import (
    apply_command,
    load_state,
    make_app,
    save_state,
    snapshot,
)
from kp_arb.strategy_core import CoreState, ScreenKind


def _ready(state: CoreState, screen: str = "autoM") -> None:
    apply_command(state, {"cmd": "per_qty", "screen": screen,
                          "block": "entry", "qty": 5})
    apply_command(state, {"cmd": "settings", "screen": screen, "max_position": 100})
    apply_command(state, {"cmd": "set_threshold", "screen": screen,
                          "block": "entry", "set": 0, "value": 0.006})
    apply_command(state, {"cmd": "set_target", "screen": screen,
                          "block": "entry", "set": 0, "value": 100})


def test_setup_and_run() -> None:
    state = CoreState()
    _ready(state)
    result = apply_command(state, {"cmd": "run", "screen": "autoM",
                                   "block": "entry", "set": 0, "value": True})
    assert result["ok"]
    assert state.screens[ScreenKind.AUTO_M].entry_sets[0].running


def test_run_rejected_without_inputs() -> None:
    state = CoreState()
    result = apply_command(state, {"cmd": "run", "screen": "autoM",
                                   "block": "entry", "set": 0, "value": True})
    assert not result["ok"] and result["errors"]
    assert not state.screens[ScreenKind.AUTO_M].entry_sets[0].running


def test_threshold_free_input() -> None:
    # 기준값 자유 입력 — 0 경고·±1% 한계 없음 (사용자 확정 2026-07-24)
    state = CoreState()
    result = apply_command(state, {"cmd": "set_threshold", "screen": "autoT",
                                   "block": "entry", "set": 0, "value": -0.02})
    assert result["ok"] and result["warnings"] == []
    assert state.screens[ScreenKind.AUTO_T].entry_sets[0].threshold == -0.02
    result = apply_command(state, {"cmd": "set_threshold", "screen": "autoT",
                                   "block": "exit", "set": 0, "value": 0.02})
    assert result["ok"]
    assert state.screens[ScreenKind.AUTO_T].exit_sets[0].threshold == 0.02
    result = apply_command(state, {"cmd": "set_threshold", "screen": "autoT",
                                   "block": "entry", "set": 0, "value": None})
    assert result["ok"]
    assert state.screens[ScreenKind.AUTO_T].entry_sets[0].threshold is None


def test_ls_order_checkbox_per_set() -> None:
    state = CoreState()
    apply_command(state, {"cmd": "ls_order", "screen": "autoT",
                          "block": "exit", "set": 1, "value": False})
    screen = state.screens[ScreenKind.AUTO_T]
    assert not screen.exit_sets[1].ls_order
    assert screen.exit_sets[0].ls_order and screen.entry_sets[1].ls_order


def test_shutdown_stops_all_sets() -> None:
    state = CoreState()
    _ready(state)
    apply_command(state, {"cmd": "run", "screen": "autoM",
                          "block": "entry", "set": 0, "value": True})
    result = apply_command(state, {"cmd": "shutdown"})
    assert result["ok"]
    assert not state.screens[ScreenKind.AUTO_M].entry_sets[0].running


def test_unknown_and_bad_commands() -> None:
    state = CoreState()
    assert not apply_command(state, {"cmd": "nope"})["ok"]
    assert not apply_command(state, {"cmd": "per_qty", "screen": "없는화면",
                                     "qty": 1})["ok"]
    assert not apply_command(state, {"cmd": "fx_month", "choice": "far"})["ok"]


def test_state_persistence_roundtrip(tmp_path: Path) -> None:
    state = CoreState()
    _ready(state)
    apply_command(state, {"cmd": "run", "screen": "autoM",
                          "block": "entry", "set": 0, "value": True})
    apply_command(state, {"cmd": "fx_month", "choice": "next"})
    apply_command(state, {"cmd": "ls_order", "screen": "autoM",
                          "block": "entry", "set": 0, "value": False})
    path = tmp_path / "core_state.json"
    save_state(path, state)

    restored = load_state(path)
    screen = restored.screens[ScreenKind.AUTO_M]
    assert screen.entry_per_qty == 5
    assert not screen.entry_sets[0].ls_order  # 세트별 LS주문 체크 복원
    assert screen.settings.max_position == 100
    assert screen.entry_sets[0].threshold == 0.006
    assert screen.entry_sets[0].target_qty == 100
    assert restored.fx_month == "next"
    assert not screen.entry_sets[0].running  # 실행 상태는 복원 안 함 (안전)


def test_load_state_missing_or_corrupt(tmp_path: Path) -> None:
    assert load_state(tmp_path / "none.json").fx_month == "near"
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    assert load_state(bad).fx_month == "near"


async def test_http_roundtrip_and_shutdown_hook() -> None:
    import asyncio

    state = CoreState()
    stop = asyncio.Event()
    client = TestClient(TestServer(make_app(state, on_shutdown=stop.set)))
    await client.start_server()
    try:
        resp = await client.get("/state")
        assert resp.status == 200
        data = await resp.json()
        assert set(data["screens"]) == {"autoT", "autoM"}

        resp = await client.post("/command", json={
            "cmd": "per_qty", "screen": "autoT", "block": "entry", "qty": 30})
        assert resp.status == 200 and (await resp.json())["ok"]
        assert state.screens[ScreenKind.AUTO_T].entry_per_qty == 30

        resp = await client.post("/command", data=b"not json")
        assert resp.status == 400

        resp = await client.post("/command", json={"cmd": "shutdown"})
        assert (await resp.json())["ok"]
        await asyncio.wait_for(stop.wait(), timeout=1.0)
    finally:
        await client.close()


def test_snapshot_serializable() -> None:
    import json

    text = json.dumps(snapshot(CoreState()), default=str, ensure_ascii=False)
    assert "autoT" in text and "autoM" in text


def test_live_snapshot_disconnected() -> None:
    from kp_arb.core_server import live_snapshot

    live = live_snapshot(CoreState(), None, None)
    assert live["connected"] is False and live["rehearsal"] is True


def test_settings_operating_hours_validated() -> None:
    state = CoreState()
    result = apply_command(state, {"cmd": "settings", "screen": "autoM",
                                   "operating_hours": "09:00-15:00"})
    assert result["ok"]
    assert state.screens[ScreenKind.AUTO_M].settings.operating_hours == "09:00-15:00"
    result = apply_command(state, {"cmd": "settings", "screen": "autoM",
                                   "operating_hours": "가나다"})
    assert not result["ok"]  # 형식 오류는 저장 거부
    assert state.screens[ScreenKind.AUTO_M].settings.operating_hours == "09:00-15:00"


def test_reset_fired() -> None:
    state = CoreState()
    state.screens[ScreenKind.AUTO_M].entry_sets[0].fired_qty = 100
    result = apply_command(state, {"cmd": "reset_fired", "screen": "autoM",
                                   "block": "entry", "set": 0})
    assert result["ok"]
    assert state.screens[ScreenKind.AUTO_M].entry_sets[0].fired_qty == 0


def test_legacy_per_order_qty_migrates(tmp_path: Path) -> None:
    # 옛 저장(per_order_qty 단일) → 진입/청산 양쪽으로 이어받음
    import json as _json
    path = tmp_path / "core_state.json"
    path.write_text(_json.dumps({"screens": {"autoM": {"per_order_qty": 7}}}),
                    encoding="utf-8")
    restored = load_state(path)
    screen = restored.screens[ScreenKind.AUTO_M]
    assert screen.entry_per_qty == 7 and screen.exit_per_qty == 7
