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


def test_settings_global_command_and_persistence(tmp_path: Path) -> None:
    state = CoreState()
    res = apply_command(state, {
        "cmd": "settings_global",
        "hl_daily_limit_usdc": 5000.0,
        "fx_carry_rate": 0.02, "eq_carry_rate": 0.04,
        "sound_fill": {"enabled": True, "path": "C:/s/fill.wav"},
        "sound_ws": {"enabled": False, "path": "C:/s/ws.wav"},
    })
    assert res["ok"]
    assert state.settings.hl_daily_limit_usdc == 5000.0
    assert state.settings.fx_carry_rate == 0.02 and state.settings.eq_carry_rate == 0.04
    assert state.settings.sound_fill.enabled
    # 저장·복원 왕복 — 공통설정도 core_state.json에 남는다
    path = tmp_path / "core_state.json"
    save_state(path, state)
    restored = load_state(path)
    assert restored.settings.hl_daily_limit_usdc == 5000.0
    assert restored.settings.fx_carry_rate == 0.02 and restored.settings.eq_carry_rate == 0.04
    assert restored.settings.sound_fill.enabled
    assert restored.settings.sound_fill.path == "C:/s/fill.wav"
    assert not restored.settings.sound_ws.enabled


def test_load_state_missing_or_corrupt(tmp_path: Path) -> None:
    assert load_state(tmp_path / "none.json").fx_month == "near"
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    assert load_state(bad).fx_month == "near"


async def test_ws_hub_snapshot_push_and_heartbeat() -> None:
    # 실시간 채널(DESIGN §12.1): 접속 즉시 스냅샷 → mark()면 묶어서 푸시 → 조용하면 하트비트.
    import asyncio
    import json

    from kp_arb.core_server import WsHub

    hub = WsHub(None, coalesce_s=0.01, heartbeat_s=0.05)
    runner = asyncio.create_task(hub.run())
    client = TestClient(TestServer(make_app(CoreState(), hub=hub)))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        first = json.loads((await ws.receive()).data)
        assert first["channel"] == "manual" and first["data"]["connected"] is False
        assert hub.subscribers == 1

        hub.mark()  # 이벤트 발생 → 묶음 뒤 스냅샷 1회
        pushed = json.loads((await asyncio.wait_for(ws.receive(), 1.0)).data)
        assert "data" in pushed and pushed["channel"] == "manual"
        assert hub.pushes == 1

        beat = json.loads((await asyncio.wait_for(ws.receive(), 1.0)).data)  # 조용함 → 하트비트
        assert beat.get("heartbeat") is True and "ts" in beat

        await ws.close()
        await asyncio.sleep(0.05)
        assert hub.subscribers == 0  # 접속 종료 시 목록에서 제거
    finally:
        runner.cancel()
        await client.close()


def test_order_book_on_change_fires_on_mutations() -> None:
    # 장부 변화 훅 — 주문 등록·취소마다 불려 실시간 채널이 밀어줄 타이밍을 안다.
    from kp_arb.domain.enums import Instrument, OrderType, Side, Underlying, Venue
    from kp_arb.domain.models import OrderIntent
    from kp_arb.order_book import OrderBook

    ob = OrderBook()
    hits: list[int] = []
    ob.on_change.append(lambda: hits.append(1))
    ob.track("X1", OrderIntent(venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
                               instrument=Instrument.HL_PERP, side=Side.BUY, qty=1,
                               order_type=OrderType.LIMIT, price=100.0))
    ob.on_cancel("X1")
    ob.on_cancel("X1")  # 이미 닫힌 주문 — 변화 없음 → 호출 안 함
    assert len(hits) == 2


async def test_boot_errors_exposed_in_state() -> None:
    # 코어 조립 자체가 실패(system=None)해도 /state load_errors에 실려 메인창이 팝업.
    import asyncio

    state = CoreState()
    stop = asyncio.Event()
    client = TestClient(TestServer(make_app(
        state, on_shutdown=stop.set, boot_errors=["시동(코어 조립 실패: KeyError: 'X')"])))
    await client.start_server()
    try:
        data = await (await client.get("/state")).json()
        assert data["load_errors"] == ["시동(코어 조립 실패: KeyError: 'X')"]
    finally:
        await client.close()


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


def test_base_dir_dev_vs_frozen(monkeypatch) -> None:
    import sys
    from pathlib import Path

    from kp_arb.core_server import _base_dir

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert _base_dir().name == "kp-arb" or _base_dir().is_dir()  # 개발: 프로젝트 루트
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\dist\meme\meme-core.exe")
    assert _base_dir() == Path(r"C:\dist\meme")  # 배포: exe 옆
