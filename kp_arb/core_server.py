"""전략 코어 프로세스 — 접속·판정·명령의 본체 (DESIGN §12 "코어 하나 + 여러 화면").

    코어 시작/안전종료는 메인 화면(main.bat)에서. 단독: python -m kp_arb.core_server

시동 시 LS/HL에 접속(LiveSystem)하고 리허설 판정 루프(7-3a — 발주 없음)를 돌린다.
접속 실패(키 없음 등)여도 API는 계속 떠서 화면 조작·입력은 가능("시세 없음" 표시).
로그는 콘솔 + logs/core_날짜.log 파일에 남는다.

화면(자동T/자동M 주문·모니터·웹)은 http://127.0.0.1:8787 로 접속한다.
- GET  /state    : CoreState 스냅샷 + live(신호·현재가·환율·가상포지션)
- POST /command  : {"cmd": ..., "screen": "autoT"|"autoM", ...} — apply_command 참조
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .domain.enums import Instrument, Underlying
from .strategy_core import (
    Block,
    CoreState,
    ScreenKind,
    ScreenState,
    parse_operating_hours,
    state_from_dict,
    validate_run,
)

if TYPE_CHECKING:
    from .bootstrap import LiveSystem
    from .core_engine import RehearsalEngine

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
        if cmd == "per_qty":  # 1회주문수량 — 진입/청산 별도 (block 지정)
            screen = _screen_of(state, body)
            qty = int(body["qty"])
            if Block(str(body["block"])) is Block.ENTRY:
                screen.entry_per_qty = qty
            else:
                screen.exit_per_qty = qty
            return _ok()
        if cmd == "ls_order":  # 세트별 LS주문 체크 — 해제 시 HL 주문만 (§6.2-2)
            screen = _screen_of(state, body)
            block = Block(str(body["block"]))
            screen.sets_of(block)[int(body["set"])].ls_order = bool(body["value"])
            return _ok()
        if cmd == "set_threshold":
            # 기준값은 자유 입력 (0 경고·±1% 한계 제거 — 사용자 확정 2026-07-24)
            screen = _screen_of(state, body)
            block = Block(str(body["block"]))
            raw = body["value"]
            screen.sets_of(block)[int(body["set"])].threshold = (
                None if raw is None else float(raw))
            return _ok()
        if cmd == "set_target":
            screen = _screen_of(state, body)
            block = Block(str(body["block"]))
            screen.sets_of(block)[int(body["set"])].target_qty = int(body["value"])
            return _ok()
        if cmd == "reset_fired":  # 세트 진입수량(발주 누적) 초기화 — 리허설 재시작용
            screen = _screen_of(state, body)
            block = Block(str(body["block"]))
            screen.sets_of(block)[int(body["set"])].fired_qty = 0
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
            if "operating_hours" in body:  # 운영시간 덮어쓰기 — 형식 검증 후 저장
                hours = str(body["operating_hours"]).strip()
                parse_operating_hours(hours)  # 틀리면 ValueError → 거부
                s.operating_hours = hours
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


def live_snapshot(
    state: CoreState,
    system: LiveSystem | None,
    engine: RehearsalEngine | None,
) -> dict[str, Any]:
    """화면 표시용 실시간 수치 — 신호(est 스프레드)·현재가·환율·가상포지션 (7-3a)."""
    if system is None:
        return {"connected": False, "rehearsal": True, "screens": {}}
    fx, fx_src = system.usdkrw_effective()
    screens: dict[str, Any] = {}
    for kind, screen in state.screens.items():
        u = screen.underlying
        instrument = kind.counterpart
        entry, exit_ = system.pair_signal(
            u, instrument, screen.entry_per_qty, screen.exit_per_qty)
        if instrument is Instrument.KR_STOCK:
            kr_last = system.stock_last(u)
        else:
            kr_last = (system.trades.get((u, instrument, "uni"))
                       or system.trades.get((u, instrument, "krx")))
        runtime = engine.runtime.get(kind) if engine is not None else None
        screens[kind.value] = {
            "entry": entry,
            "exit": exit_,
            "kr_last": kr_last,
            "hl_last": system.trades.get((u, Instrument.HL_PERP, "hl")),
            "fx": fx,
            "fx_src": fx_src,
            "position": runtime.virtual_position if runtime is not None else 0,
        }
    return {"connected": True, "rehearsal": True, "screens": screens}


def make_app(
    state: CoreState,
    on_shutdown: Callable[[], None] | None = None,
    save: Callable[[], None] | None = None,
    system: LiveSystem | None = None,
    engine: RehearsalEngine | None = None,
) -> web.Application:
    """API 앱 조립 — 화면이 붙는 유일한 창구. on_shutdown = 종료 훅, save = 저장 훅."""

    async def get_state(_request: web.Request) -> web.Response:
        payload = snapshot(state)
        payload["live"] = live_snapshot(state, system, engine)
        return web.json_response(payload, dumps=_dumps)

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


def _setup_logging() -> logging.Logger:
    """콘솔 + logs/core_날짜.log 파일 로그 (7-3a — 판정·발주 추적용)."""
    import time

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
        file_handler: logging.Handler = logging.FileHandler(
            log_dir / f"core_{time.strftime('%Y%m%d')}.log", encoding="utf-8")
    except OSError:
        file_handler = logging.NullHandler()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )
    return logging.getLogger("kp_arb.core")


async def _fx_report_loop(system: LiveSystem, log: logging.Logger) -> None:
    """HL 명목·환율을 외부 #2(Dalin broadcast)로 주기 전송 (7-4). 값 바뀔 때만.

    total_coin = HL 보유 Σ(평균단가×수량), token="Meme". 실패해도 코어를 멈추지 않는다.
    """
    from .fx import hl_coin_notional
    from .fx_reporter import FXExposureReporter
    from .signallink import SignalLinkSink

    sink = SignalLinkSink(system_name="kp-arb")
    await sink.start()
    reporter = FXExposureReporter(sink, token="Meme", notional_fn=hl_coin_notional)
    try:
        while True:
            await asyncio.sleep(2.0)
            positions = system.order_book.positions()
            fx, _ = system.usdkrw_effective()
            sent = await reporter.report_if_changed(positions, fx or 0.0)
            if sent is not None:
                log.info("FX 보고 → #2: total_coin=%.0f fx=%.2f ok=%s",
                         sent.total_coin, sent.fx, reporter.last_sent_ok)
    except asyncio.CancelledError:
        await sink.stop()
        raise


async def _serve() -> None:
    import aiohttp

    log = _setup_logging()
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    state = load_state(STATE_PATH)  # 마지막 입력값 복원 (실행 상태는 항상 꺼짐)
    stop = asyncio.Event()
    async with aiohttp.ClientSession() as http:
        # LS/HL 접속 — 실패해도 API는 계속(화면 조작·입력 가능, 시세만 없음)
        system = None
        engine = None
        tasks: list[asyncio.Task[None]] = []
        try:
            from .bootstrap import bootstrap_live
            from .core_engine import RehearsalEngine

            system = await bootstrap_live(http)
            await system.start()
            engine = RehearsalEngine(state, system)
            tasks.append(asyncio.create_task(engine.run()))
            tasks.append(asyncio.create_task(_fx_report_loop(system, log)))
            log.info("LiveSystem 결합 완료 — 리허설 판정 + FX 보고 시작 (발주 없음)")
        except Exception:  # noqa: BLE001 - 키 없음/네트워크 등
            log.exception("LiveSystem 시동 실패 — API만 운영 (시세 없음)")

        # access_log=None: 화면 폴링(GET /state 1초)이 로그를 도배하지 않게
        runner = web.AppRunner(make_app(
            state, on_shutdown=stop.set,
            save=lambda: save_state(STATE_PATH, state),
            system=system, engine=engine), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, HOST, DEFAULT_PORT)
        await site.start()
        log.info("코어 시동: http://%s:%s (안전종료는 메인 화면에서)", HOST, DEFAULT_PORT)
        await stop.wait()
        for task in tasks:
            task.cancel()
        await runner.cleanup()
        log.info("코어 안전종료 완료")  # 미체결 전량 취소는 7-3b에서 이 앞에


def main() -> None:
    """코어 단독 시동. 종료는 메인 화면의 안전종료(또는 Ctrl+C)."""
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
