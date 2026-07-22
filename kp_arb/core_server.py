"""전략 코어 프로세스 — 로컬 명령/조회 API (DESIGN §12 "코어 하나 + 여러 화면").

    코어 시작/안전종료는 메인 화면(main.bat)에서. 단독: python -m kp_arb.core_server

화면(자동T/자동M 주문·모니터·웹)은 http://127.0.0.1:8787 로 접속한다.
- GET  /state    : CoreState 스냅샷(JSON)
- POST /command  : {"cmd": ..., "screen": "autoT"|"autoM", ...} — apply_command 참조

이 단계는 상태·명령까지. 실제 발주·시세(LiveSystem 결합)는 다음 단계.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from .domain.enums import Underlying
from .strategy_core import (
    Block,
    CoreState,
    ScreenKind,
    ScreenState,
    state_from_dict,
    threshold_check,
    validate_run,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
# 입력값 저장 파일 (§6.2-0 상태 저장) — gitignore, 명령마다 갱신
STATE_PATH = Path(__file__).resolve().parent.parent / "core_state.json"


def snapshot(state: CoreState) -> dict[str, Any]:
    """상태 스냅샷 — JSON 직렬화 가능한 dict (StrEnum은 값 문자열)."""
    return dataclasses.asdict(state)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def _screen_of(state: CoreState, body: dict[str, Any]) -> ScreenState:
    return state.screens[ScreenKind(str(body["screen"]))]


def _ok(**extra: Any) -> dict[str, Any]:
    return {"ok": True, "errors": [], "warnings": [], **extra}


def _fail(errors: list[str]) -> dict[str, Any]:
    return {"ok": False, "errors": errors, "warnings": []}


def apply_command(  # noqa: PLR0911 - 명령 분기표
    state: CoreState, body: dict[str, Any]
) -> dict[str, Any]:
    """명령 1건 적용 — 순수 로직(HTTP와 분리, 단위 테스트 대상).

    응답: {"ok", "errors", "warnings", ...}. 경고는 화면이 확인창으로 보여준다.
    """
    cmd = body.get("cmd")
    try:
        if cmd == "select":
            _screen_of(state, body).underlying = Underlying(str(body["underlying"]))
            return _ok()
        if cmd == "per_qty":
            _screen_of(state, body).per_order_qty = int(body["qty"])
            return _ok()
        if cmd == "ls_order":  # 블록별 LS주문 체크 — 해제 시 HL 주문만 (§6.2-2)
            screen = _screen_of(state, body)
            value = bool(body["value"])
            if Block(str(body["block"])) is Block.ENTRY:
                screen.ls_order_entry = value
            else:
                screen.ls_order_exit = value
            return _ok()
        if cmd == "set_threshold":
            screen = _screen_of(state, body)
            block = Block(str(body["block"]))
            raw = body["value"]
            if raw is None:
                screen.sets_of(block)[int(body["set"])].threshold = None
                return _ok()
            threshold = float(raw)
            errors, warnings = threshold_check(block, threshold)
            if errors:
                return _fail(errors)  # 입력 자체 거부 (±1% 한계)
            screen.sets_of(block)[int(body["set"])].threshold = threshold
            return {"ok": True, "errors": [], "warnings": warnings}
        if cmd == "set_target":
            screen = _screen_of(state, body)
            block = Block(str(body["block"]))
            screen.sets_of(block)[int(body["set"])].target_qty = int(body["value"])
            return _ok()
        if cmd == "run":  # 실행 버튼 토글 — 켤 때 검증, 끄면 정지(취소는 결합 후)
            screen = _screen_of(state, body)
            block = Block(str(body["block"]))
            index = int(body["set"])
            value = bool(body["value"])
            if value:
                errors = validate_run(screen, block, index)
                if errors:
                    return _fail(errors)
            screen.sets_of(block)[index].running = value
            return _ok()
        if cmd == "settings":
            s = _screen_of(state, body).settings
            s.kr_margin_ticks = int(body.get("kr_margin_ticks", s.kr_margin_ticks))
            s.hl_margin_pct = float(body.get("hl_margin_pct", s.hl_margin_pct))
            s.delay_ms = int(body.get("delay_ms", s.delay_ms))
            s.pre_order_range_ticks = int(
                body.get("pre_order_range_ticks", s.pre_order_range_ticks))
            s.max_position = int(body.get("max_position", s.max_position))
            s.daily_limit_100m = float(body.get("daily_limit_100m", s.daily_limit_100m))
            return _ok()
        if cmd == "fx_month":  # 환율 표시용 원달러선물 월물 (§6.2-7)
            choice = str(body["choice"])
            if choice not in ("near", "next"):
                return _fail([f"fx_month는 near/next: {choice!r}"])
            state.fx_month = choice
            return _ok()
        if cmd == "shutdown":
            # 안전종료 1단계: 전 세트 실행 해제. 미체결 전량 취소·기록 마무리는
            # LiveSystem 결합 때 이 자리에 채운다. 강제 킬 없음(§6.2-0).
            for screen in state.screens.values():
                for spread_set in screen.entry_sets + screen.exit_sets:
                    spread_set.running = False
            return _ok(note="전 세트 정지 — 프로세스 종료 예약")
    except (KeyError, ValueError, IndexError) as exc:
        return _fail([f"잘못된 명령 인자: {exc!r}"])
    return _fail([f"알 수 없는 명령: {cmd!r}"])


def save_state(path: Path, state: CoreState) -> None:
    """상태를 JSON 파일로 저장 — 실패(잠김 등)는 무시(다음 명령 때 재시도)."""
    try:
        path.write_text(_dumps(snapshot(state)), encoding="utf-8")
    except OSError:
        pass


def load_state(path: Path) -> CoreState:
    """저장 파일에서 복원 — 없거나 깨졌으면 기본값. 실행 상태는 복원 안 함."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CoreState()
    return state_from_dict(data) if isinstance(data, dict) else CoreState()


def make_app(
    state: CoreState,
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
            save()  # 입력값 저장 — 재시작 시 복원 (§6.2-0)
        if payload.get("cmd") == "shutdown" and result.get("ok") and on_shutdown:
            # 응답을 먼저 보내고 잠시 뒤 종료 (화면이 결과를 받을 시간)
            asyncio.get_running_loop().call_later(0.2, on_shutdown)
        return web.json_response(result, dumps=_dumps)

    app = web.Application()
    app.router.add_get("/state", get_state)
    app.router.add_post("/command", post_command)
    return app


async def _serve() -> None:
    state = load_state(STATE_PATH)  # 마지막 입력값 복원 (실행 상태는 항상 꺼짐)
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
