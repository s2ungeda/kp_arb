"""전략 코어 프로세스 — 로컬 명령/조회 API (DESIGN §12 "코어 하나 + 여러 화면").

    python -m kp_arb.core_server        # 코어 시동 (API만 — LiveSystem 결합은 3단계)

화면(전략/모니터/웹)은 http://127.0.0.1:8787 로 명령을 보내고 상태를 읽는다.
- GET  /state    : PanelState 스냅샷(JSON)
- POST /command  : {"cmd": ...} — 아래 apply_command 참조

이 단계(2단계)는 상태·명령까지. 실제 발주(LiveSystem.place)·실포지션 반영은 3단계.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from .domain.enums import Instrument, Underlying
from .strategy_core import (
    Mode,
    OrderAction,
    PanelState,
    plan_order,
    state_from_dict,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
# 입력값 저장 파일 (§6.2-1 "입력값은 저장") — gitignore, 명령마다 갱신
STATE_PATH = Path(__file__).resolve().parent.parent / "core_state.json"


def snapshot(state: PanelState) -> dict[str, Any]:
    """상태 스냅샷 — JSON 직렬화 가능한 dict (enum은 값 문자열)."""
    return dataclasses.asdict(state)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def apply_command(  # noqa: PLR0911 - 명령 분기표
    state: PanelState, body: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """명령 1건 적용 — 순수 로직(HTTP와 분리, 단위 테스트 대상).

    응답: {"ok": bool, "errors": [사유...], ...명령별 추가 필드}.
    """
    cmd = body.get("cmd")
    try:
        if cmd == "set_mode":
            state.mode = Mode(str(body["mode"]))
            for s in state.sets:  # 모드 전환 시 자동 상태 초기화 (안전)
                s.started = False
                s.paused = False
            return {"ok": True, "errors": []}
        if cmd == "select":
            if "underlying" in body:
                state.underlying = Underlying(str(body["underlying"]))
            if "counterpart" in body:
                state.counterpart = Instrument(str(body["counterpart"]))
            return {"ok": True, "errors": []}
        if cmd == "venues":
            state.ls_enabled = bool(body.get("ls", state.ls_enabled))
            state.hl_enabled = bool(body.get("hl", state.hl_enabled))
            return {"ok": True, "errors": []}
        if cmd == "monitor_qty":
            state.monitor_qty = int(body["qty"])
            return {"ok": True, "errors": []}
        if cmd == "set_inputs":
            target = state.sets[int(body["set"])].inputs
            target.total_qty = int(body.get("total", target.total_qty))
            target.per_order_qty = int(body.get("per", target.per_order_qty))
            if "entry" in body:
                v = body["entry"]
                target.entry_threshold = None if v is None else float(v)
            if "exit" in body:
                v = body["exit"]
                target.exit_threshold = None if v is None else float(v)
            return {"ok": True, "errors": []}
        if cmd == "start":
            errors = state.start_set(int(body["set"]), bool(body["value"]))
            return {"ok": not errors, "errors": errors}
        if cmd == "pause":
            state.pause_set(int(body["set"]), bool(body["value"]))
            return {"ok": True, "errors": []}
        if cmd == "options":
            opts = state.options
            opts.max_retries = int(body.get("max_retries", opts.max_retries))
            opts.retry_interval_s = float(body.get("retry_interval_s", opts.retry_interval_s))
            opts.wait_timer_s = float(body.get("wait_timer_s", opts.wait_timer_s))
            if "order_window" in body:
                start, end = body["order_window"]
                opts.order_window = (str(start), str(end))
            opts.stock_credit = bool(body.get("stock_credit", opts.stock_credit))
            return {"ok": True, "errors": []}
        if cmd == "shutdown":
            # 안전종료 1단계: 전 세트 자동 정지. 미체결 전량 취소·기록 마무리는
            # LiveSystem 결합(3단계 후반) 때 이 자리에 채운다. 강제 킬 없음(§6.2).
            for s in state.sets:
                s.started = False
                s.paused = False
            return {"ok": True, "errors": [],
                    "note": "자동 정지 완료 — 프로세스 종료 예약"}
        if cmd == "manual_order":
            if state.mode is not Mode.MANUAL:
                return {"ok": False, "errors": ["수동 주문은 수동 모드에서만"]}
            action = OrderAction(str(body["action"]))
            inputs = state.sets[int(body["set"])].inputs
            moment = now if now is not None else datetime.now()
            # 실포지션은 3단계(LiveSystem 결합)에서 — 지금은 0으로 계획만 검증
            plan, errors = plan_order(
                action, state.counterpart, int(body.get("position", 0)), inputs,
                mode=state.mode, ls_enabled=state.ls_enabled,
                hl_enabled=state.hl_enabled, now=moment.time(),
                options=state.options)
            if plan is None:
                return {"ok": False, "errors": errors}
            return {"ok": True, "errors": [],
                    "plan": dataclasses.asdict(plan),
                    "note": "3단계 전 — 계획만 반환, 발주 안 함"}
    except (KeyError, ValueError, IndexError) as exc:
        return {"ok": False, "errors": [f"잘못된 명령 인자: {exc!r}"]}
    return {"ok": False, "errors": [f"알 수 없는 명령: {cmd!r}"]}


def save_state(path: Path, state: PanelState) -> None:
    """상태를 JSON 파일로 저장 — 실패(잠김 등)는 무시(다음 명령 때 재시도)."""
    try:
        path.write_text(_dumps(snapshot(state)), encoding="utf-8")
    except OSError:
        pass


def load_state(path: Path) -> PanelState:
    """저장 파일에서 복원 — 없거나 깨졌으면 기본값. 자동 시작 상태는 복원 안 함."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PanelState()
    return state_from_dict(data) if isinstance(data, dict) else PanelState()


def make_app(
    state: PanelState,
    on_shutdown: Callable[[], None] | None = None,
    save: Callable[[], None] | None = None,
) -> web.Application:
    """API 앱 조립 — 화면이 붙는 유일한 창구. on_shutdown = 종료 훅, save = 저장 훅."""

    async def get_state(_request: web.Request) -> web.Response:
        return web.json_response(snapshot(state), dumps=_dumps)

    async def post_command(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"ok": False, "errors": ["JSON 본문 필요"]}, status=400, dumps=_dumps)
        payload = body if isinstance(body, dict) else {}
        result = apply_command(state, payload)
        if result.get("ok") and save:
            save()  # 입력값 저장 — 재시작 시 그대로 복원 (§6.2-1)
        if payload.get("cmd") == "shutdown" and result.get("ok") and on_shutdown:
            # 응답을 먼저 보내고 잠시 뒤 종료 (화면이 결과를 받을 시간)
            asyncio.get_running_loop().call_later(0.2, on_shutdown)
        return web.json_response(result, dumps=_dumps)

    app = web.Application()
    app.router.add_get("/state", get_state)
    app.router.add_post("/command", post_command)
    return app


async def _serve() -> None:
    state = load_state(STATE_PATH)  # 마지막 입력값 복원 (자동 시작은 항상 꺼짐)
    stop = asyncio.Event()
    runner = web.AppRunner(make_app(
        state, on_shutdown=stop.set, save=lambda: save_state(STATE_PATH, state)))
    await runner.setup()
    site = web.TCPSite(runner, HOST, DEFAULT_PORT)
    await site.start()
    print(f"코어 시동 — http://{HOST}:{DEFAULT_PORT} (안전종료는 메인 화면에서)")
    await stop.wait()
    await runner.cleanup()
    print("코어 안전종료 완료")


def main() -> None:
    """코어 단독 시동. 종료는 메인 화면의 안전종료(또는 Ctrl+C)."""
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
