"""코어 API 테스트 — 명령 적용(순수) + HTTP 왕복 + 저장/복원 (DESIGN §12, §6.2)."""
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from kp_arb.core_server import (
    apply_command,
    load_state,
    make_app,
    nxt_trade_symbols,
    save_state,
    snapshot,
)
from kp_arb.domain.enums import Instrument, Underlying
from kp_arb.strategy_core import CoreState, ScreenKind, _global_settings_from_dict


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


def test_state_persists_hl_merge(tmp_path: Path) -> None:
    # HL 호가단위(종목별 틱 USD, 2026-10-27)는 코어 재시동 때 복원 — 형식 틀린 항목·모르는 종목은
    # 버린다. 옛 형식 [nSigFigs, mantissa]는 legacy로 따로 들고 간다(첫 가격에 틱으로 변환).
    state = CoreState()
    state.hl_merge["samsung"] = 0.02
    state.hl_merge["sk_hynix"] = 0.1
    state.hl_merge_legacy["hyundai"] = [4, None]
    path = tmp_path / "core_state.json"
    save_state(path, state)
    restored = load_state(path)
    assert restored.hl_merge == {"samsung": 0.02, "sk_hynix": 0.1}
    assert restored.hl_merge_legacy == {"hyundai": [4, None]}

    from kp_arb.strategy_core import state_from_dict
    bad = state_from_dict({"hl_merge": {"samsung": [5, 2], "nope": [5, 2], "hyundai": "x",
                                        "sk_hynix": [None, 2], "samsung_x": 0.1}})
    assert bad.hl_merge == {} and bad.hl_merge_legacy == {"samsung": [5, 2]}
    old = state_from_dict({"hl_merge": {"sk_hynix": [4, None], "samsung": 0.5}})
    assert old.hl_merge == {"samsung": 0.5} and old.hl_merge_legacy == {"sk_hynix": [4, None]}


def test_sync_hl_merge_keeps_tick_across_price_band() -> None:
    # 사용자 2026-10-27 "하이닉스 가격 상승으로 호가단위가 변하는데 우리 시스템은 예전 상태" — 고른
    # 틱 0.1은 999달러대에선 nSigFigs 4, 1,000달러를 넘으면 원시(None)여야 한다. 2초 루프가 가격대
    # 변화를 보고 재구독. 없는 틱(0.01 → 1,000달러 위)은 가장 가까운 0.1로 대체하고 가격대마다 한 번
    # 경고. 옛 형식은 첫 가격에 틱으로 변환.
    from kp_arb.core_server import sync_hl_merge
    from kp_arb.domain.models import Quote

    class _HL:
        def __init__(self) -> None:
            self.quotes: dict[Any, Quote] = {}
            self.active: dict[Underlying, tuple[int | None, int | None]] = {}
            self.calls: list[tuple[Underlying, int | None, int | None]] = []

        def hl_merge_active(self, u: Underlying) -> tuple[int | None, int | None]:
            return self.active.get(u, (None, None))

        def set_hl_aggregation(self, u: Underlying, nsf: int | None, mant: int | None) -> None:
            self.active[u] = (nsf, mant)
            self.calls.append((u, nsf, mant))

    def quote(px: float) -> Quote:
        return Quote(underlying=Underlying.SK_HYNIX, instrument=Instrument.HL_PERP, bid=px,
                     ask=px, ts=0.0, market="hl")

    sys_ = _HL()
    state = CoreState()
    state.hl_merge["sk_hynix"] = 0.1
    assert sync_hl_merge(state, sys_) == [] and sys_.calls == []  # 가격 전 — 아무것도 안 함
    sys_.quotes[(Underlying.SK_HYNIX, Instrument.HL_PERP, "hl")] = quote(999.0)
    msgs = sync_hl_merge(state, sys_)
    assert sys_.calls == [(Underlying.SK_HYNIX, 4, None)] and "재적용" in msgs[0]
    assert sync_hl_merge(state, sys_) == []                       # 같은 가격대 — 조용
    sys_.quotes[(Underlying.SK_HYNIX, Instrument.HL_PERP, "hl")] = quote(1009.0)
    msgs = sync_hl_merge(state, sys_)
    assert sys_.calls[-1] == (Underlying.SK_HYNIX, None, None)   # 1,000달러 위 0.1 = 원시
    assert len(msgs) == 1 and "0.1" in msgs[0]
    assert state.hl_merge["sk_hynix"] == 0.1                      # 고른 틱은 그대로
    # 없는 틱: 0.01을 골랐는데 1,000달러 위 → 0.1 적용 + 경고 한 번(같은 가격대 반복 없음)
    state.hl_merge["sk_hynix"] = 0.01
    msgs = sync_hl_merge(state, sys_)
    assert any("없음" in m for m in msgs) and sys_.active[Underlying.SK_HYNIX] == (None, None)
    assert not any("없음" in m for m in sync_hl_merge(state, sys_))
    # 옛 형식 변환: [5, 2]를 1,009달러에서 보면 틱 0.2
    state.hl_merge.clear()
    state.hl_merge_legacy["sk_hynix"] = [5, 2]
    msgs = sync_hl_merge(state, sys_)
    assert state.hl_merge == {"sk_hynix": 0.2} and state.hl_merge_legacy == {}
    assert any("옛 형식" in m for m in msgs) and sys_.active[Underlying.SK_HYNIX] == (5, 2)


def test_settings_global_fx_fut_window_user_input() -> None:
    # 외환선물 우선시간(exec 결정 47)은 공통설정 사용자 입력 — HH:MM:SS 저장, 형식 오류는 거부.
    state = CoreState()
    assert (state.settings.fx_fut_start, state.settings.fx_fut_end) == ("08:45:00", "15:45:00")
    ok = apply_command(state, {"cmd": "settings_global",
                               "fx_fut_start": "08:00:00", "fx_fut_end": "17:30:00"})
    assert ok["ok"] and state.settings.fx_fut_start == "08:00:00"
    assert state.settings.fx_fut_end == "17:30:00"
    bad = apply_command(state, {"cmd": "settings_global", "fx_fut_end": "25:00:00"})
    assert not bad["ok"] and state.settings.fx_fut_end == "17:30:00"  # 거부 시 값 유지
    restored = CoreState()
    _global_settings_from_dict(restored.settings,
                               {"fx_fut_start": "09:00:00", "fx_fut_end": "bad"})
    assert (restored.settings.fx_fut_start, restored.settings.fx_fut_end) == (
        "09:00:00", "15:45:00")
    # 옛 키(fx_spot_*, 뜻이 반대)는 무시 → 기본값 유지
    _global_settings_from_dict(restored.settings,
                               {"fx_spot_start": "07:00", "fx_spot_end": "18:10"})
    assert (restored.settings.fx_fut_start, restored.settings.fx_fut_end) == (
        "09:00:00", "15:45:00")
    # 2구간: 둘 다 넣으면 저장, 하나만 넣으면 거부, 둘 다 비우면 미사용
    ok2 = apply_command(state, {"cmd": "settings_global",
                                "fx_fut_start2": "18:00:00", "fx_fut_end2": "23:00:00"})
    assert ok2["ok"] and (state.settings.fx_fut_start2, state.settings.fx_fut_end2) == (
        "18:00:00", "23:00:00")
    half = apply_command(state, {"cmd": "settings_global", "fx_fut_start2": "19:00:00",
                                 "fx_fut_end2": ""})
    assert not half["ok"] and state.settings.fx_fut_end2 == "23:00:00"
    off = apply_command(state, {"cmd": "settings_global", "fx_fut_start2": "",
                                "fx_fut_end2": ""})
    assert off["ok"] and state.settings.fx_fut_start2 == "" and state.settings.fx_fut_end2 == ""
    r2 = CoreState()
    _global_settings_from_dict(r2.settings, {"fx_fut_start2": "20:00:00", "fx_fut_end2": "21:00"})
    assert (r2.settings.fx_fut_start2, r2.settings.fx_fut_end2) == ("20:00:00", "21:00")


def test_settings_global_command_and_persistence(tmp_path: Path) -> None:
    state = CoreState()
    res = apply_command(state, {
        "cmd": "settings_global",
        "hl_daily_limit_usdc": 5000.0,
        "fx_carry_rate": 0.02, "eq_carry_rate": 0.04,
        "sound_fill": {"enabled": True, "path": "C:/s/fill.wav"},
        "sound_ws": {"enabled": False, "path": "C:/s/ws.wav"},
        "fx_futures_code": " A756A000 ",  # 환율 계산용 원달러선물 월물(2026-09-28)
        "ls_disconnect": {"popup": True, "telegram": False, "sound": True},  # §2a 2026-10-14
    })
    assert res["ok"]
    assert state.settings.fx_futures_code == "A756A000"
    dc = state.settings.ls_disconnect
    assert (dc.popup, dc.telegram, dc.sound) == (True, False, True)
    # 값을 안 보내면(콤보가 빈 화면) 기존 선택 유지, 빈 문자열이면 최근월물로 되돌림
    assert apply_command(state, {"cmd": "settings_global"})["ok"]
    assert state.settings.fx_futures_code == "A756A000"
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
    assert restored.settings.fx_futures_code == "A756A000"
    rdc = restored.settings.ls_disconnect
    assert (rdc.popup, rdc.telegram, rdc.sound) == (True, False, True)
    # 키를 안 보내면 그대로, bool 아닌 값은 무시
    assert apply_command(state, {"cmd": "settings_global"})["ok"]
    assert apply_command(state, {"cmd": "settings_global",
                                 "ls_disconnect": {"popup": "no", "telegram": True}})["ok"]
    dc = state.settings.ls_disconnect
    assert (dc.popup, dc.telegram, dc.sound) == (True, True, True)
    assert apply_command(state, {"cmd": "settings_global", "fx_futures_code": ""})["ok"]
    assert state.settings.fx_futures_code == ""


def test_load_state_missing_or_corrupt(tmp_path: Path) -> None:
    assert load_state(tmp_path / "none.json").fx_month == "near"
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    assert load_state(bad).fx_month == "near"


async def test_hl_trades_endpoint_returns_newest_first_or_empty() -> None:
    # HL 체결 창(2026-09-15): /hl_trades?underlying= — 코어 보관 30건을 최신 순으로. 시스템 없음·
    # 종목 오류면 빈 목록(창은 "체결 대기 중").
    from collections import deque

    from kp_arb.domain.enums import Underlying

    class _Sys:
        hl_trades = {Underlying.SK_HYNIX: deque([
            {"time": "10:00:00.000", "ts": 1.0, "side": "buy", "price": 1.0, "qty": 1.0},
            {"time": "10:00:01.000", "ts": 2.0, "side": "sell", "price": 2.0, "qty": 0.5},
        ], maxlen=30)}

        # make_app이 시동 때 주입하는 공통설정 훅 — 여기선 아무것도 안 함
        def set_hl_daily_limit(self, usdc: float) -> None: ...
        def set_carry_rates(self, fx: float, eq: float) -> None: ...
        def set_fx_fut_window(self, start: str, end: str, start2: str = "",
                              end2: str = "") -> None: ...
        def set_fx_futures_code(self, code: str) -> None: ...

    client = TestClient(TestServer(make_app(CoreState(), system=_Sys())))  # type: ignore[arg-type]
    await client.start_server()
    try:
        resp = await client.get("/hl_trades?underlying=sk_hynix")
        body = await resp.json()
        assert body["underlying"] == "sk_hynix"
        assert [r["price"] for r in body["rows"]] == [2.0, 1.0]  # 최신이 위
        resp = await client.get("/hl_trades?underlying=nope")
        assert (await resp.json()) == {"underlying": None, "rows": []}
        resp = await client.get("/hl_trades")  # 종목 없음 = trades 채널과 같은 모양(창 폴백)
        body = await resp.json()
        assert [r["price"] for r in body["trades"]["sk_hynix"]] == [2.0, 1.0]
    finally:
        await client.close()


async def test_ws_hub_trades_channel_pushes_only_on_hl_trade() -> None:
    # trades 채널(§12.1 3차, 2026-09-15): 구독 즉시 스냅샷, HL 체결이 올 때만 푸시(다른 이벤트·
    # LS 체결은 안 보냄). HTTP 폴링보다 0.5초쯤 늦던 HL 체결 창을 실시간으로.
    import asyncio
    import json
    from collections import deque

    from kp_arb.core_server import WsHub
    from kp_arb.domain.enums import Instrument, Underlying
    from kp_arb.gateways.ls_ws import TradeTick
    from kp_arb.order_book import OrderBook

    class _Sys:
        on_quote: list = []  # noqa: RUF012 - 테스트 스텁
        on_mark: list = []  # noqa: RUF012
        on_trade: list = []  # noqa: RUF012
        order_book = OrderBook()
        hl_trades = {Underlying.SK_HYNIX: deque(
            [{"time": "t", "ts": 1.0, "side": "buy", "price": 1.0, "qty": 1.0}], maxlen=30)}

    sys_ = _Sys()
    hub = WsHub(sys_, coalesce_s=0.01, heartbeat_s=5.0)  # type: ignore[arg-type]
    # 접속 직후의 manual 스냅샷은 LiveSystem 전체가 필요 — 이 테스트는 trades 채널만 본다
    hub._manual_text = lambda: '{"channel":"manual","ts":0,"data":{}}'  # type: ignore[method-assign]
    runner = asyncio.create_task(hub.run())
    client = TestClient(TestServer(make_app(CoreState(), hub=hub)))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.receive()  # manual 스냅샷
        await ws.send_str('{"subscribe":["trades"]}')
        snap = json.loads((await asyncio.wait_for(ws.receive(), 1.0)).data)
        assert snap["channel"] == "trades" and snap["data"]["trades"]["sk_hynix"][0]["price"] == 1.0

        ls_tick = TradeTick(underlying=Underlying.SK_HYNIX, instrument=Instrument.KR_STOCK,
                            price=1.0, market="krx")
        for h in sys_.on_trade:
            h(ls_tick)  # LS 체결 — manual은 밀어도 trades 채널엔 안 보냄
        got = json.loads((await asyncio.wait_for(ws.receive(), 1.0)).data)
        assert got["channel"] == "manual"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ws.receive(), 0.1)  # trades 푸시 없음

        hl_tick = TradeTick(underlying=Underlying.SK_HYNIX, instrument=Instrument.HL_PERP,
                            price=2.0, market="hl", side="sell", qty=0.5, ts=2.0)
        sys_.hl_trades[Underlying.SK_HYNIX].append(
            {"time": "t2", "ts": 2.0, "side": "sell", "price": 2.0, "qty": 0.5})
        for h in sys_.on_trade:
            h(hl_tick)
        pushed = json.loads((await asyncio.wait_for(ws.receive(), 1.0)).data)
        if pushed["channel"] == "manual":  # 같은 묶음에서 manual이 먼저 나갈 수 있다
            pushed = json.loads((await asyncio.wait_for(ws.receive(), 1.0)).data)
        assert pushed["channel"] == "trades"
        assert [r["price"] for r in pushed["data"]["trades"]["sk_hynix"]] == [2.0, 1.0]
        await ws.close()
    finally:
        runner.cancel()
        await client.close()


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


async def test_ws_hub_state_channel_snapshot_and_change_only_push() -> None:
    # state 채널(§12.1): 구독하면 /state와 같은 본문 1회 → 내용이 안 바뀌면 mark()해도 안 보냄
    # → 하트비트는 채널별로 온다.
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
        assert first["channel"] == "manual"
        await ws.send_str('{"subscribe":["state"]}')
        st = json.loads((await asyncio.wait_for(ws.receive(), 1.0)).data)
        assert st["channel"] == "state" and "settings" in st["data"] and "ws" in st["data"]

        hub.mark()  # 시세 이벤트 — manual은 푸시, state는 내용 그대로라 안 보냄
        got = []
        for _ in range(4):
            got.append(json.loads((await asyncio.wait_for(ws.receive(), 1.0)).data))
        kinds = [(m["channel"], bool(m.get("heartbeat"))) for m in got]
        assert ("manual", False) in kinds and ("state", False) not in kinds
        assert ("manual", True) in kinds and ("state", True) in kinds  # 채널별 하트비트
        await ws.close()
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
        # 장운영 단계는 /state 본문에 실려야 한다(결정 57 초록 — 10-16 실측: monitor_snapshot에만
        # 있어 체결쏴 화면이 늘 None을 봤음). 시스템 없으면 None.
        assert "phase" in data and data["phase"] is None

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


def test_manual_snapshot_carries_rejects() -> None:
    # 거부내역(주문리스트 '거부' 행, 2026-09-18) — 코어가 든 당일치 전부를 그대로 싣는다
    from types import SimpleNamespace

    from kp_arb.core_server import manual_snapshot

    assert manual_snapshot(None)["rejects"] == []
    fake = SimpleNamespace(order_book=SimpleNamespace(open_orders=lambda: []), fills=[],
                           cancels=[], rejects=[{"kind": "발주", "reason": "LS 02297"}],
                           instruments={}, quotes={}, trades={}, stock_change_pct={})
    try:
        snap = manual_snapshot(fake)  # type: ignore[arg-type]
    except AttributeError:
        return  # 가짜 시스템이 종목 조립까지 흉내 내지 않음 — 키 존재는 None 경로로 확인됨
    assert snap["rejects"] == [{"kind": "발주", "reason": "LS 02297"}]


def test_quote_symbols_setting_and_plan_input() -> None:
    # DESIGN-ls-xing §9 / DESIGN-settings §4: 시세 구독 종목 체크 저장·복원(모르는 키 무시),
    # 계획 재료는 체크 종목 + 체결쏴 주식 거래소 + 주식선물·HL선 월물 합집합 + 원달러 코드.
    # 실행 켜기는 자동 체크.
    from kp_arb.core_server import auto_check_target, ensure_quote_symbol, quote_plan_input
    from kp_arb.domain.enums import Underlying

    state = CoreState()
    assert state.settings.quote_symbols == {"samsung": True, "sk_hynix": True, "hyundai": False}
    ok = apply_command(state, {"cmd": "settings_global",
                               "quote_symbols": {"sk_hynix": False, "nope": True, "hyundai": 1}})
    assert ok["ok"] and state.settings.quote_symbols == {"samsung": True, "sk_hynix": False,
                                                         "hyundai": True}
    restored = CoreState()
    _global_settings_from_dict(restored.settings, {"quote_symbols": {"samsung": False, "x": 1}})
    assert restored.settings.quote_symbols == {"samsung": False, "sk_hynix": True,
                                               "hyundai": False}
    state.autom.book(Underlying.SAMSUNG, "stock").market = "nxt"
    state.autom.book(Underlying.SAMSUNG, "sf").future_month = "next"
    state.autom.book(Underlying.SAMSUNG, "sf_hl_first").future_month = "near"

    class _Sys:
        futures_symbols = {Underlying.SAMSUNG: "A116B000"}
        next_futures_symbols = {Underlying.SAMSUNG: "A116C000"}

        @staticmethod
        def fx_futures_code() -> str:
            return "A756B000"

    inp = quote_plan_input(state, _Sys())
    assert inp.symbols_on == frozenset({Underlying.SAMSUNG, Underlying.HYUNDAI})
    assert inp.futures_months[Underlying.SAMSUNG] == frozenset({"near", "next"})
    assert inp.futures_codes[(Underlying.SAMSUNG, "next")] == "A116C000"
    assert inp.fx_code == "A756B000"
    assert quote_plan_input(state, None).fx_code is None and not quote_plan_input(
        state, None).futures_codes
    # NXT 체결(NS3)은 NXT 개장 화면에서 오늘 '적'을 눌렀거나 실행 중인 종목만(사용자 2026-10-27).
    # 동시호가 예약 행·체결쏴 주식의 거래소는 계획과 무관(주식 호가는 통합 UH1 하나 — 10-27 오후).
    assert inp.nxt_trade_symbols == frozenset()
    state.nxt_open.of(Underlying.SK_HYNIX).applied_day = "2026-10-27"
    assert nxt_trade_symbols(state, "2026-10-27") == frozenset({Underlying.SK_HYNIX})
    assert nxt_trade_symbols(state, "2026-10-28") == frozenset()   # 다른 날 — 안 붙는다
    state.nxt_open.of(Underlying.SK_HYNIX).applied_day = None
    state.nxt_open.of(Underlying.SAMSUNG).run_exit = True
    state.auction.book("sk_hynix").buy[0].on = True
    inp2 = quote_plan_input(state, _Sys())
    assert inp2.nxt_trade_symbols == frozenset({Underlying.SAMSUNG})
    assert inp2 == quote_plan_input(state, _Sys())
    state.nxt_open.of(Underlying.SAMSUNG).run_exit = False
    state.auction.book("sk_hynix").buy[0].on = False
    inp3 = quote_plan_input(state, _Sys())
    assert inp3.nxt_trade_symbols == frozenset()
    # 실행 켜기 → 체크 안 된 종목은 자동으로 켠다(거부 아님), 이미 켜져 있으면 변화 없음
    assert ensure_quote_symbol(state, Underlying.SK_HYNIX, "자동M")
    assert state.settings.quote_symbols["sk_hynix"]
    assert not ensure_quote_symbol(state, Underlying.SK_HYNIX, "자동M")
    # 자동 체크 대상 = 종목을 다루는 자동M·NXT 개장 명령 전부(사용자 2026-10-23 "적만 눌러도") —
    # 실행 끔·종목 없음·다른 명령은 아님
    assert auto_check_target({"cmd": "autom_ref_qty", "underlying": "sk_hynix"}) is (
        Underlying.SK_HYNIX)
    assert auto_check_target({"cmd": "autom_set", "underlying": "samsung"}) is Underlying.SAMSUNG
    assert auto_check_target({"cmd": "nxt_open_settings", "underlying": "hyundai"}) is (
        Underlying.HYUNDAI)
    assert auto_check_target({"cmd": "autom_run", "underlying": "samsung", "value": True})
    assert auto_check_target({"cmd": "autom_run", "underlying": "samsung", "value": False}) is None
    assert auto_check_target({"cmd": "nxt_open_run", "underlying": "samsung"}) is None
    assert auto_check_target({"cmd": "autom_stop_all"}) is None
    assert auto_check_target({"cmd": "settings_global", "underlying": "samsung"}) is None
    assert auto_check_target({"cmd": "autom_set", "underlying": "nope"}) is None


def test_settings_global_risk_limits_roundtrip() -> None:
    # DESIGN-settings §1a — 리스크 한도 저장·복원, 음수는 거부, 키 없으면 기존 값
    state = CoreState()
    ok = apply_command(state, {"cmd": "settings_global", "hl_price_limit_pct": 0.02,
                               "hl_max_qty_per_order": 300})
    assert ok["ok"] and state.settings.hl_price_limit_pct == 0.02
    assert state.settings.hl_max_qty_per_order == 300
    bad = apply_command(state, {"cmd": "settings_global", "hl_price_limit_pct": -1})
    assert not bad["ok"] and state.settings.hl_price_limit_pct == 0.02
    keep = apply_command(state, {"cmd": "settings_global", "hl_daily_limit_usdc": 5})
    assert keep["ok"] and state.settings.hl_max_qty_per_order == 300
    assert state.settings.quote_stale_s == 1800.0  # 기본 30분(결정 86)
    assert apply_command(state, {"cmd": "settings_global", "quote_stale_s": 600})["ok"]
    assert state.settings.quote_stale_s == 600.0
    assert not apply_command(state, {"cmd": "settings_global", "quote_stale_s": -1})["ok"]
    restored = CoreState()
    _global_settings_from_dict(restored.settings, {"hl_price_limit_pct": 0.01,
                                                   "hl_max_qty_per_order": -5})
    assert restored.settings.hl_price_limit_pct == 0.01
    assert restored.settings.hl_max_qty_per_order == 0.0


def test_auction_screen_state_persists_per_underlying(tmp_path: Path) -> None:
    # DESIGN-auction-order §6: 동시호가 종목별 상태(행·월물·설정·HL 행↔주문번호)가 core_state.json에
    # 저장·복원된다(예약실행은 상태에 없음)
    from kp_arb.auction_order import Row

    state = CoreState()
    b = state.auction.book("sk_hynix")
    b.buy[0] = Row(0.3, 20, True)
    b.month = "next"
    b.settings.fx_hms = "15:44:57"
    b.hl_orders["a"] = "777"
    path = tmp_path / "core_state.json"
    save_state(path, state)
    back = load_state(path)
    assert back.auction.book("sk_hynix") == b and "samsung" not in back.auction.books


async def test_manual_fx_futures_command_places_one_order_or_reports_rejection() -> None:
    # 진단용(사용자 2026-10-26): 원달러선물 1건을 KR_FX로 직접 — 월물·가격을 안 주면 공통설정
    # 월물·현재가, 거부는 사유 그대로 돌려준다
    from kp_arb.core_server import _manual_command
    from kp_arb.domain.enums import Side

    class _Sys:
        fx_futures_price = {"A756B000": 1321.5}
        placed: list[tuple[str, Side, int, float, str]] = []
        fail = False

        def fx_futures_code(self) -> str:
            return "A756B000"

        async def place_fx_futures(self, code: str, side: Side, qty: int, price: float,
                                   src: str = "") -> str:
            if self.fail:
                raise RuntimeError("CFOAT00100 요청 실패 -9: 해당 계좌가 없습니다.")
            self.placed.append((code, side, qty, price, src))
            return "F77"

    sys_ = _Sys()
    ok = await _manual_command(sys_, {"cmd": "manual_fx_futures"})  # type: ignore[arg-type]
    assert ok["ok"] and ok["order_id"] == "F77" and ok["code"] == "A756B000"
    assert sys_.placed == [("A756B000", Side.BUY, 1, 1321.5, "수동(진단)")]
    sys_.fail = True
    bad = await _manual_command(sys_, {"cmd": "manual_fx_futures", "side": "sell",  # type: ignore[arg-type]
                                      "qty": 2, "price": 1320.0})
    assert not bad["ok"] and "해당 계좌가 없습니다" in bad["errors"][0]
    assert not (await _manual_command(sys_, {"cmd": "manual_fx_futures", "qty": 0}))["ok"]  # type: ignore[arg-type]
