"""코어 API 테스트 — 명령 적용(순수) + HTTP 왕복 (DESIGN §12)."""
from datetime import datetime

from aiohttp.test_utils import TestClient, TestServer

from kp_arb.core_server import apply_command, make_app, snapshot
from kp_arb.strategy_core import Mode, PanelState

NOON = datetime(2026, 7, 21, 12, 0)


def _auto_ready(state: PanelState, index: int = 0) -> None:
    apply_command(state, {"cmd": "set_mode", "mode": "자동T"})
    apply_command(state, {"cmd": "set_inputs", "set": index,
                          "total": 100, "per": 20, "entry": 0.15, "exit": 0.0})


def test_set_inputs_and_start() -> None:
    state = PanelState()
    _auto_ready(state)
    result = apply_command(state, {"cmd": "start", "set": 0, "value": True})
    assert result["ok"] and state.sets[0].started


def test_start_rejected_on_bad_inputs() -> None:
    state = PanelState()
    apply_command(state, {"cmd": "set_mode", "mode": "자동T"})
    apply_command(state, {"cmd": "set_inputs", "set": 0,
                          "total": 100, "per": 20, "entry": 0.0, "exit": 0.1})
    result = apply_command(state, {"cmd": "start", "set": 0, "value": True})
    assert not result["ok"] and not state.sets[0].started


def test_mode_switch_resets_auto_flags() -> None:
    state = PanelState()
    _auto_ready(state)
    apply_command(state, {"cmd": "start", "set": 0, "value": True})
    apply_command(state, {"cmd": "pause", "set": 0, "value": True})
    apply_command(state, {"cmd": "set_mode", "mode": "수동"})
    assert state.mode is Mode.MANUAL
    assert not state.sets[0].started and not state.sets[0].paused


def test_manual_order_returns_plan() -> None:
    state = PanelState()  # 수동 기본, 하이닉스/주식선물 기본
    apply_command(state, {"cmd": "set_inputs", "set": 0, "total": 10, "per": 2})
    result = apply_command(state, {"cmd": "manual_order", "set": 0, "action": "진입"},
                           now=NOON)
    assert result["ok"], result["errors"]
    legs = result["plan"]["legs"]
    assert len(legs) == 2 and legs[1]["qty"] == 20  # 선물 2계약 → HL 20주


def test_manual_order_blocked_in_auto() -> None:
    state = PanelState()
    _auto_ready(state)
    result = apply_command(state, {"cmd": "manual_order", "set": 0, "action": "진입"},
                           now=NOON)
    assert not result["ok"]


def test_unknown_and_bad_commands() -> None:
    state = PanelState()
    assert not apply_command(state, {"cmd": "nope"})["ok"]
    assert not apply_command(state, {"cmd": "set_inputs", "set": 99, "total": 1})["ok"]


async def test_http_roundtrip() -> None:
    state = PanelState()
    client = TestClient(TestServer(make_app(state)))
    await client.start_server()
    try:
        resp = await client.get("/state")
        assert resp.status == 200
        data = await resp.json()
        assert data["mode"] == "수동" and len(data["sets"]) == 3

        resp = await client.post("/command", json={"cmd": "monitor_qty", "qty": 7})
        assert resp.status == 200 and (await resp.json())["ok"]
        assert state.monitor_qty == 7

        resp = await client.post("/command", data=b"not json")
        assert resp.status == 400
    finally:
        await client.close()


def test_snapshot_serializable() -> None:
    import json

    state = PanelState()
    text = json.dumps(snapshot(state), default=str, ensure_ascii=False)
    assert "자동" not in text and "수동" in text  # mode 기본값 직렬화 확인
