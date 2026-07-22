"""전략 코어 프로세스 — 로컬 명령/조회 API (DESIGN §12 "코어 하나 + 여러 화면").

    python -m kp_arb.core_server        # 코어 시동 (API만 — LiveSystem 결합은 3단계)

화면(전략/모니터/웹)은 http://127.0.0.1:8787 로 명령을 보내고 상태를 읽는다.
- GET  /state    : PanelState 스냅샷(JSON)
- POST /command  : {"cmd": ...} — 아래 apply_command 참조

이 단계(2단계)는 상태·명령까지. 실제 발주(LiveSystem.place)·실포지션 반영은 3단계.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from typing import Any

from aiohttp import web

from .domain.enums import Instrument, Underlying
from .strategy_core import (
    Mode,
    OrderAction,
    PanelState,
    plan_order,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 8787


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


def make_app(state: PanelState) -> web.Application:
    """API 앱 조립 — 화면이 붙는 유일한 창구."""

    async def get_state(_request: web.Request) -> web.Response:
        return web.json_response(snapshot(state), dumps=_dumps)

    async def post_command(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"ok": False, "errors": ["JSON 본문 필요"]}, status=400, dumps=_dumps)
        result = apply_command(state, body if isinstance(body, dict) else {})
        return web.json_response(result, dumps=_dumps)

    app = web.Application()
    app.router.add_get("/state", get_state)
    app.router.add_post("/command", post_command)
    return app


def main() -> None:
    """코어 단독 시동 (2단계: API만). 종료는 Ctrl+C."""
    state = PanelState()
    web.run_app(make_app(state), host=HOST, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
