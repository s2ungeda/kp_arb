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
    apply_command(state, {"cmd": "per_qty", "screen": screen, "qty": 5})
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


def test_threshold_limit_and_warning() -> None:
    state = CoreState()
    # ±1% 한계는 입력 자체 거부
    result = apply_command(state, {"cmd": "set_threshold", "screen": "autoT",
                                   "block": "entry", "set": 0, "value": -0.02})
    assert not result["ok"]
    assert state.screens[ScreenKind.AUTO_T].entry_sets[0].threshold is None
    # 0 이하는 저장되지만 경고 반환 → 화면이 확인창
    result = apply_command(state, {"cmd": "set_threshold", "screen": "autoT",
                                   "block": "entry", "set": 0, "value": -0.001})
    assert result["ok"] and result["warnings"] == ["낮은수치"]


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
    assert screen.per_order_qty == 5
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
            "cmd": "per_qty", "screen": "autoT", "qty": 30})
        assert resp.status == 200 and (await resp.json())["ok"]
        assert state.screens[ScreenKind.AUTO_T].per_order_qty == 30

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
