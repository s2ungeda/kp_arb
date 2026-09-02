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
from pydantic import ValidationError

from .domain.enums import Instrument, OrderType, Side, Underlying, Venue
from .domain.models import OrderIntent, Quote
from .fx_auction import FxAuctionSettings
from .hl_merge import merge_tick_options
from .manual_order import is_spot_stock, sellable_qty, short_sale_error
from .routing import account_for
from .strategy_core import (
    Block,
    CoreState,
    ScreenKind,
    ScreenState,
    parse_operating_hours,
    state_from_dict,
    validate_run,
)
from .ticks import tick_for

if TYPE_CHECKING:
    from .bootstrap import LiveSystem
    from .core_engine import RehearsalEngine
    from .fx_service import FxReportService

HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# 응답을 막지 않는 백그라운드 작업(manual_refresh 등)의 참조 보관 — 중간 GC 방지.
_BG_TASKS: set[asyncio.Task[None]] = set()
_olog = logging.getLogger("kp_arb.order")  # 수동 주문 발주·거부 기록 → logs/core_날짜.log


def _base_dir() -> Path:
    """상태·로그를 둘 기준 폴더. 배포판(exe)은 실행파일 옆, 개발은 프로젝트 루트.

    frozen에서 __file__ 기준을 쓰면 _internal 안에 파일이 생겨 배포 폴더 복사가
    막힌다(로그 잠김). 그래서 exe일 때는 sys.executable 옆을 쓴다.
    """
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# 입력값 저장 파일 (§6.2-0 상태 저장) — gitignore, 명령마다 갱신
STATE_PATH = _base_dir() / "core_state.json"


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


def _ws_order_warning(system: LiveSystem | None) -> str | None:
    """현재 WS 건강 기준 발주 경고 사유(없으면 None). 수동은 **경고만** — 차단 안 함(§2).
    무데이터 임계는 env ``KP_WS_MAX_IDLE_S``(기본 10초)로 조정. 자동 발주는 같은 게이트로
    하드 차단(추후). Phase 8-6."""
    if system is None:
        return None
    import os
    import time as _time

    from .ws_status import order_block_reason

    try:
        max_idle = float(os.environ.get("KP_WS_MAX_IDLE_S", "10"))
    except ValueError:
        max_idle = 10.0
    return order_block_reason(system.ws_statuses(), _time.monotonic(), max_idle)


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
        if cmd == "settings_global":  # 전체 공통설정(일일 한도·이자율·알람) — DESIGN-settings §4
            g = state.settings
            g.hl_daily_limit_usdc = float(
                body.get("hl_daily_limit_usdc", g.hl_daily_limit_usdc))
            g.fx_carry_rate = float(body.get("fx_carry_rate", g.fx_carry_rate))
            g.eq_carry_rate = float(body.get("eq_carry_rate", g.eq_carry_rate))
            for name, snd in (("sound_fill", g.sound_fill),
                              ("sound_error", g.sound_error), ("sound_ws", g.sound_ws)):
                raw = body.get(name)
                if isinstance(raw, dict):
                    snd.enabled = bool(raw.get("enabled", snd.enabled))
                    snd.path = str(raw.get("path", snd.path))
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


# 수동 주문창이 다루는 instrument와, 호가/현재가를 고를 시장 우선순위.
_MANUAL_INSTRUMENTS: tuple[Instrument, ...] = (
    Instrument.KR_STOCK, Instrument.KR_STOCK_FUTURE, Instrument.HL_PERP)
_MARKET_PREF: dict[Instrument, tuple[str, ...]] = {
    Instrument.KR_STOCK: ("uni", "krx", "nxt"),
    Instrument.KR_STOCK_FUTURE: ("krx", "uni", "nxt"),
    Instrument.HL_PERP: ("hl",),
}


def _first(mapping: dict[Any, Any], keys: list[Any]) -> Any:
    """keys 순서로 mapping을 훑어 처음 발견한 값(없으면 None)."""
    for k in keys:
        v = mapping.get(k)
        if v is not None:
            return v
    return None


def _ladder(quote: Quote | None, asks: bool) -> list[list[float]]:
    """Quote에서 호가창 [[가격, 잔량], ...] — 정렬(매도 오름/매수 내림)·중복가격 병합.

    HL 원시 피드가 정렬 안 되거나 같은 가격을 중복 제시할 수 있어, 여기서 가격별로
    잔량을 합치고 정렬한다(안 그러면 틱·현재가 강조가 틀어진다). 다단계 없으면 1호가로.
    """
    if quote is None:
        return []
    levels = quote.asks if asks else quote.bids
    if levels:
        merged: dict[float, float] = {}
        for p, q in levels:
            merged[p] = merged.get(p, 0.0) + q
        items = sorted(merged.items(), reverse=not asks)  # 매도 오름차순 / 매수 내림차순
        return [[p, q] for p, q in items]
    px = quote.ask if asks else quote.bid
    qty = (quote.ask_qty if asks else quote.bid_qty) or 0.0
    return [[px, qty]]


def _tick_size(
    instrument: Instrument, price: float | None, ladder: list[list[float]]
) -> float | None:
    """호가모드 ±틱용 틱 크기 — KR은 규칙(ticks.tick_for), HL/시세없음은 호가 간격 추정."""
    if instrument is not Instrument.HL_PERP and price:
        return float(tick_for(instrument, price))
    if len(ladder) >= 2:  # HL 등: 인접 두 호가 간격을 틱으로
        return abs(ladder[1][0] - ladder[0][0])
    return None


def manual_snapshot(system: LiveSystem | None) -> dict[str, Any]:
    """수동 주문창용 스냅샷 — 취급 종목별 호가·포지션·매도가능·잔고 + 전체 미체결.

    화면(일반 주문창)이 폴링해 표시만. 조회 폴링 없이 OrderBook·quotes 메모리 읽기.
    """
    if system is None:
        return {"connected": False, "symbols": {}, "open_orders": [],
                "fills": [], "cancels": []}
    ob = system.order_book
    pending_sell: dict[tuple[Underlying, Instrument], float] = {}
    open_orders: list[dict[str, Any]] = []
    for o in ob.open_orders():
        it = o.intent
        if it.side is Side.SELL:
            k = (it.underlying, it.instrument)
            pending_sell[k] = pending_sell.get(k, 0.0) + o.remaining_qty
        open_orders.append({
            "order_id": o.order_id, "underlying": it.underlying.value,
            "instrument": it.instrument.value, "side": it.side.value,
            "qty": it.qty, "remaining": o.remaining_qty,
            "price": it.price, "status": o.status.value,
            "time": o.placed_at,  # 접수 시각(HH:MM:SS) — 주문 리스트 '시각' 칸
        })
    symbols: dict[str, Any] = {}
    for u in Underlying:
        for inst in _MANUAL_INSTRUMENTS:
            mkeys = [(u, inst, m) for m in _MARKET_PREF[inst]]
            quote = _first(system.quotes, mkeys)
            account = account_for(inst) if inst.venue is Venue.LS else None
            held = ob.position_qty(u, inst, account)
            avg = ob.avg_price(u, inst, account)
            last = _first(system.trades, mkeys)
            bids = _ladder(quote, asks=False)
            asks = _ladder(quote, asks=True)
            has_pos = last is not None and held != 0
            entry: dict[str, Any] = {
                "bids": bids, "asks": asks,
                "position": held, "avg_price": avg, "last": last,
                "pnl": (last - avg) * held if has_pos else 0.0,   # 평가손익
                "eval": abs(held) * last if has_pos else 0.0,     # 평가금액
                "tick": _tick_size(inst, last, asks or bids),
                "liq": None,  # HL 청산가 — 기본 None, HL은 아래 detail에서 채움
            }
            if inst is Instrument.HL_PERP:  # 잔고표 오른쪽(B) — 오라클·펀딩 + 상세
                mark = system.hl_mark.get(u)
                entry["oracle"] = mark.oracle if mark is not None else None
                entry["funding_rate"] = system.hl_funding_rate.get(u)
                detail = system.hl_detail.get(u) or {}
                entry["margin"] = detail.get("margin")
                entry["cum_funding"] = detail.get("cum_funding")
                entry["leverage"] = detail.get("leverage")            # D: 현재 배수
                entry["leverage_cross"] = detail.get("leverage_cross")  # 교차 여부
                # max_leverage: 포지션 있으면 clearinghouse, 없으면 종목정보(시동 조회)
                info = system.instruments.get((u, inst))
                entry["max_leverage"] = (
                    detail.get("max_leverage")
                    if detail.get("max_leverage") is not None
                    else (info.max_leverage if info is not None else None))
                if detail.get("liq") is not None:
                    entry["liq"] = detail["liq"]
                # 호가단위(틱) 옵션 — 가격 자릿수 기반(순수). '적' 전에도 화면 콤보가 채워짐.
                _ref = last if last is not None else (
                    (asks[0][0] if asks else None) or (bids[0][0] if bids else None))
                entry["merge_ticks"] = [
                    {"tick": s, "n_sig_figs": nsf, "mantissa": mant}
                    for s, nsf, mant in merge_tick_options(float(_ref))
                ] if _ref else []
                # 현재 적용 중인 머지(단일 진실=코어) — 새 창·다른 창이 콤보를 맞추는 기준.
                _ma = (system.hl_merge_active(u)
                       if hasattr(system, "hl_merge_active") else None)
                entry["merge_active"] = (
                    {"n_sig_figs": _ma[0], "mantissa": _ma[1]} if _ma is not None else None)
            if is_spot_stock(inst):
                entry["sellable"] = sellable_qty(
                    held, pending_sell.get((u, inst), 0.0))
            if account is not None:
                entry["balance"] = ob.balance(account)
            symbols[f"{u.value}|{inst.value}"] = entry
    fills = list(getattr(system, "fills", []))[:50]  # 최신 우선(코어 보관), 최근 50건
    cancels = list(getattr(system, "cancels", []))[:50]
    # 원달러선물 동시호가 대응(§9.1) — 화면 콤보 코드·실행상태·발주내역.
    fx_codes = system.fx_futures_codes() if hasattr(system, "fx_futures_codes") else []
    fx_running = getattr(getattr(system, "fx_auction", None), "running", False)
    fx_hedges = list(getattr(system, "fx_hedges", []))[:50]
    return {"connected": True, "symbols": symbols, "open_orders": open_orders,
            "fills": fills, "cancels": cancels,
            "fx_auction": {"running": fx_running, "codes": fx_codes,
                           "hedges": fx_hedges}}


def _fx_command(fx_service: FxReportService | None, body: dict[str, Any]) -> dict[str, Any]:
    """FX 보고 감시 명령 (감시 화면 → 코어). fx_service 없으면 거부."""
    if fx_service is None:
        return _fail(["FX 보고 서비스 미가동 (코어 시세 미접속)"])
    cmd = body.get("cmd")
    if cmd == "fx_pause":
        fx_service.pause()
    elif cmd == "fx_resume":
        fx_service.resume()
    elif cmd == "fx_send_now":
        fx_service.request_send_now()
    elif cmd == "fx_clear_log":
        fx_service.clear_log()
    elif cmd == "fx_interval":
        fx_service.set_interval(float(body["seconds"]))
    else:
        return _fail([f"알 수 없는 FX 명령: {cmd!r}"])
    return _ok()

async def _manual_command(
    system: LiveSystem | None, body: dict[str, Any]
) -> dict[str, Any]:
    """수동 주문 명령 (일반 주문창 → 코어). DESIGN-manual-order.md §6.3.

    system(라이브) 없으면 거부. 공매도(국내 현물 매도 초과)는 OrderBook 잔고로 막고,
    나머지 검증(수량>0·가격·라우팅)은 OrderIntent가 한다. 실패 사유는 화면에 전달.
    """
    if system is None:
        return _fail(["코어 시세 미접속 — 수동 주문 불가"])
    cmd = body.get("cmd")
    if cmd == "manual_hl_merge":  # HL 호가단위 머지(종목별) — WS 재구독
        try:
            underlying = Underlying(str(body["underlying"]))
            nsf = body.get("n_sig_figs")
            mant = body.get("mantissa")
            n_sig_figs = int(nsf) if nsf is not None else None
            mantissa = int(mant) if mant is not None else None
        except (KeyError, ValueError) as exc:
            return _fail([f"잘못된 머지 인자: {exc}"])
        system.set_hl_aggregation(underlying, n_sig_figs, mantissa)
        return _ok()
    if cmd == "manual_refresh":  # 잔고/포지션 재조회 → OrderBook 재동기 ('적' 버튼)
        # refresh_snapshot은 LS/HL REST라 느려, 응답을 막으면 화면 core_request가
        # 타임아웃나 '코어 미접속'으로 뜬다. 백그라운드로 돌리고 즉시 OK — 결과는 폴링 반영.
        async def _bg_refresh() -> None:
            try:
                await system.refresh_snapshot()
            except Exception:  # noqa: BLE001 - 실패해도 화면은 계속(로그만)
                logging.getLogger("kp_arb.core").warning("수동 새로고침 실패", exc_info=True)

        task = asyncio.create_task(_bg_refresh())
        _BG_TASKS.add(task)  # 참조 유지(중간 GC 방지)
        task.add_done_callback(_BG_TASKS.discard)
        return _ok()
    if cmd == "manual_cancel":
        order_id = body.get("order_id")
        if not order_id:
            return _fail(["order_id 필요"])
        try:
            await system.cancel(str(order_id))
        except Exception as exc:  # noqa: BLE001 - 실패 사유를 화면에 그대로 전달
            _olog.warning("수동주문 취소 실패: #%s — %s", order_id, exc)  # #3 실패 로그
            return _fail([f"취소 실패: {exc}"])
        return _ok()
    if cmd == "manual_leverage":  # 레버리지·마진모드 변경(주문과 별개, §1-3)
        try:
            underlying = Underlying(str(body["underlying"]))
            leverage = int(body["leverage"])
            is_cross = bool(body["is_cross"])
        except (KeyError, ValueError) as exc:
            return _fail([f"잘못된 레버리지 인자: {exc}"])
        try:
            await system.update_leverage(underlying, leverage, is_cross=is_cross)
        except Exception as exc:  # noqa: BLE001 - 거부 사유(상한·증거금)를 화면에 전달
            _olog.warning("레버리지 변경 실패: %s %dx cross=%s — %s",  # #3 실패 로그
                          underlying.value, leverage, is_cross, exc)
            return _fail([f"레버리지 변경 실패: {exc}"])
        return _ok()
    if cmd == "manual_amend":
        order_id = body.get("order_id")
        raw = body.get("price")
        if not order_id or raw is None:
            return _fail(["order_id·price 필요"])
        # HL 정정 금지(DESIGN-manual-order 2026-08-11 — 크로싱 정정 시 주문 소실). 화면
        # 차단(order_list)에 더해 코어에서도 방어 — 다른 경로로 들어와도 modify가 안 나간다.
        target = system.order_book.order(str(order_id))
        if target is not None and target.intent.venue is Venue.HYPERLIQUID:
            return _fail(["HL은 정정 미지원 — 취소 후 신규 주문하세요"])
        reduce_only = bool(body.get("reduce_only", False))  # 정정 화면 체크(HL 전용)
        post_only = bool(body.get("post_only", False))
        try:
            new_id = await system.amend_price(
                str(order_id), float(raw),
                reduce_only=reduce_only, post_only=post_only)
        except Exception as exc:  # noqa: BLE001 - 실패 사유를 화면에 그대로 전달
            _olog.warning("수동주문 정정 실패: #%s @ %s — %s", order_id, raw, exc)  # #3
            return _fail([f"정정 실패: {exc}"])
        return _ok(order_id=new_id)
    if cmd == "manual_order":
        try:
            instrument = Instrument(str(body["instrument"]))
            underlying = Underlying(str(body["underlying"]))
            side = Side(str(body["side"]))
            order_type = OrderType(str(body.get("order_type", "limit")))
            qty = float(body["qty"])
            raw_price = body.get("price")
            price = float(raw_price) if raw_price is not None else None
            reduce_only = bool(body.get("reduce_only", False))
            post_only = bool(body.get("post_only", False))
        except (KeyError, ValueError) as exc:
            return _fail([f"잘못된 주문 인자: {exc}"])
        # 공매도 검증 — 국내 현물 매도만(선물·HL은 양방향)
        account = account_for(instrument) if instrument.venue is Venue.LS else None
        if is_spot_stock(instrument) and side is Side.SELL:
            held = system.order_book.position_qty(underlying, instrument, account)
            pending = sum(
                o.remaining_qty for o in system.order_book.open_orders()
                if o.intent.underlying == underlying
                and o.intent.instrument == instrument
                and o.intent.side is Side.SELL
            )
            err = short_sale_error(instrument, side, qty, sellable_qty(held, pending))
            if err:
                _olog.warning("수동주문 거부(공매도): %s %s %s %g @ %s — %s",
                              underlying.value, instrument.value, side.value, qty, price, err)
                return _fail([err])
        try:
            intent = OrderIntent(
                venue=instrument.venue, underlying=underlying, instrument=instrument,
                side=side, qty=qty, order_type=order_type, price=price,
                reduce_only=reduce_only, post_only=post_only, source="일반주문창")
        except ValidationError as exc:
            _olog.warning("수동주문 거부(검증): %s %s %s %g @ %s — %s",
                          underlying.value, instrument.value, side.value, qty, price,
                          exc.errors()[0]["msg"])
            return _fail([f"주문 검증 실패: {exc.errors()[0]['msg']}"])
        _desc = (f"{underlying.value} {instrument.value} {side.value} {qty:g} @ {price}"
                 f"{' reduce' if reduce_only else ''}{' post' if post_only else ''}")
        try:
            order_id = await system.place(intent)
        except Exception as exc:  # noqa: BLE001 - 게이트웨이 거부/오류를 화면에 전달
            _olog.warning("수동주문 거부(발주): %s — %s", _desc, exc)
            system.bump_error()  # 알람용 — 메인창이 에러 사운드 재생(DESIGN-settings §2)
            return _fail([f"주문 실패: {exc}"])
        warn = _ws_order_warning(system)  # 수동은 경고만(§2) — 발주는 됨
        _olog.info("수동주문 접수: %s → #%s%s", _desc, order_id,
                   f" (경고: {warn})" if warn else "")
        return _ok(order_id=order_id, warnings=[warn] if warn else [])
    if cmd == "fx_auction_start":  # 원달러선물 동시호가 대응 감시 시작(§9.1)
        try:
            windows = tuple((str(a), str(b)) for a, b in (body.get("windows") or []))
            settings = FxAuctionSettings(
                windows=windows,
                fx_code=str(body["fx_code"]),
                price=float(body["price"]),
                tick=int(body["tick"]),
                hedge_ratio=float(body["hedge_ratio"]) / 100.0)  # % → 비율
        except (KeyError, ValueError, TypeError) as exc:
            return _fail([f"잘못된 대응주문 설정: {exc}"])
        system.start_fx_auction(settings)
        return _ok()
    if cmd == "fx_auction_stop":
        system.stop_fx_auction()
        return _ok()
    return _fail([f"알 수 없는 수동 명령: {cmd!r}"])


_LS_MON_INSTS = (Instrument.KR_STOCK, Instrument.KR_STOCK_FUTURE)


def _disp_pct(price: float | None, base: float | None) -> float | None:
    """(가격 − 기준) ÷ 기준 × 100. 입력 없거나 기준 0이면 None."""
    if price is None or base is None or base == 0:
        return None
    return (price - base) / base * 100.0


def monitor_snapshot(
    system: LiveSystem | None, kr_qty: int, en_thr: float, ex_thr: float
) -> dict[str, Any]:
    """시세 모니터 화면용 스냅샷 — LS/HL 표 + 괴리보드(est 포함) + 환율·잔고·장운영.

    est(진입/청산 예상체결·주문가)는 화면 입력(수량·기준%)을 코어가 받아 계산 —
    로직(est_pair_prices)을 코어에 한 벌만 둔다. 모니터는 이 스냅샷을 렌더만 한다.
    """
    if system is None:
        return {"connected": False}
    from .domain.enums import Account

    fx_used, fx_src = system.usdkrw_effective()
    out: dict[str, Any] = {
        "connected": True,
        "fx": {"used": fx_used, "src": fx_src, "futures": system.usdkrw_futures},
        "phase": system.session.phase_for(Underlying.SAMSUNG).value,
        "halt": {"stock": system.session.halt_for("1"),
                 "futures": system.session.halt_for("5")},  # §8 정지 사유(없으면 null)
        "balances": {"stock": system.order_book.balance(Account.KR_STOCK),
                     "deriv": system.order_book.balance(Account.KR_DERIV)},
        "ls": [], "hl": [], "board": [],
    }

    def _merged(u: Underlying, inst: Instrument) -> tuple[Any, Any, Any, Any]:
        cands = [q for m in ("uni", "krx", "nxt")
                 if (q := system.quotes.get((u, inst, m))) is not None]
        if not cands:
            return None, None, None, None
        ba = min(cands, key=lambda q: q.ask)   # 매도는 낮은 쪽, 매수는 높은 쪽
        bb = max(cands, key=lambda q: q.bid)
        return ba.ask, ba.ask_qty, bb.bid, bb.bid_qty

    for u in Underlying:
        for inst in _LS_MON_INSTS:
            ask, ask_qty, bid, bid_qty = _merged(u, inst)
            last = (system.trades.get((u, inst, "uni"))
                    or system.trades.get((u, inst, "krx")))
            theory = (system.stock_futures_theory(u)
                      if inst is Instrument.KR_STOCK_FUTURE else None)
            out["ls"].append({
                "underlying": u.value, "instrument": inst.value,
                "ask": ask, "ask_qty": ask_qty, "bid": bid, "bid_qty": bid_qty,
                "last": last, "expected": system.expected_prices.get((u, inst)),
                "theory": theory, "disp": _disp_pct(last, theory)})
        hq = system.quotes.get((u, Instrument.HL_PERP, "hl"))
        mk = system.hl_mark.get(u)
        hl_last = system.trades.get((u, Instrument.HL_PERP, "hl"))
        oracle = mk.oracle if mk is not None else None
        mark = mk.price if mk is not None else None
        out["hl"].append({
            "underlying": u.value,
            "ask": hq.ask if hq else None, "bid": hq.bid if hq else None,
            "last": hl_last, "oracle": oracle, "mark": mark,
            "last_vs_oracle": _disp_pct(hl_last, oracle),  # (현재가−오라클)/오라클
            "mark_vs_oracle": _disp_pct(mark, oracle),
            "funding_prev": system.hl_funding_prev.get(u),
            "funding_next": system.hl_funding_rate.get(u)})

    for (u, inst), p in system.disparity_board(kr_qty).items():
        est_bid, est_ask, px_en, px_ex = system.est_pair_prices(
            u, inst, kr_qty, en_thr, ex_thr)
        out["board"].append({
            "underlying": u.value, "instrument": inst.value,
            "entry": p.spread.entry, "exit": p.spread.exit,
            "hl_last_d": p.hl_last, "kr_last_d": p.kr_last,
            "est_bid": est_bid, "est_ask": est_ask,
            "px_entry": px_en, "px_exit": px_ex})
    return out


def make_app(
    state: CoreState,
    on_shutdown: Callable[[], None] | None = None,
    save: Callable[[], None] | None = None,
    system: LiveSystem | None = None,
    engine: RehearsalEngine | None = None,
    fx_service: FxReportService | None = None,
) -> web.Application:
    """API 앱 조립 — 화면이 붙는 유일한 창구. on_shutdown = 종료 훅, save = 저장 훅."""
    if system is not None:  # 저장된 공통설정을 시동 시 LiveSystem에 주입
        system.set_hl_daily_limit(state.settings.hl_daily_limit_usdc)
        system.set_carry_rates(state.settings.fx_carry_rate, state.settings.eq_carry_rate)

    async def get_state(_request: web.Request) -> web.Response:
        payload = snapshot(state)
        payload["live"] = live_snapshot(state, system, engine)
        payload["fx"] = (fx_service.snapshot() if fx_service is not None
                         else {"connected": False})
        payload["ws"] = ([s.to_dict() for s in system.ws_statuses()]
                         if system is not None else [])  # WS 세션 현황(Phase 8-3)
        # 알람용 이벤트 카운터 + 당일 HL 체결액(메인창 사운드·설정창 표시) — DESIGN-settings §4.
        payload["fill_seq"] = system.fill_seq if system is not None else 0
        payload["error_seq"] = system.error_seq if system is not None else 0
        payload["hl_daily_filled"] = (
            system.hl_daily_filled_today() if system is not None else 0.0)
        # 시동 로드 실패(종목/잔고/포지션/주문) — 있으면 메인창이 "…재접속하세요" 팝업 후 종료.
        payload["load_errors"] = (
            [system.startup_load_error]
            if system is not None and system.startup_load_error else [])
        return web.json_response(payload, dumps=_dumps)

    async def post_command(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"ok": False, "errors": ["JSON 본문 필요"]}, status=400, dumps=_dumps)
        payload = body if isinstance(body, dict) else {}
        cmd = payload.get("cmd")
        # fx_auction_* 는 fx_* 보다 **먼저** 검사(둘 다 "fx_"로 시작 — 순서 중요).
        if isinstance(cmd, str) and (cmd.startswith("manual_")
                                     or cmd.startswith("fx_auction_")):
            result = await _manual_command(system, payload)
            return web.json_response(result, dumps=_dumps)
        if isinstance(cmd, str) and cmd.startswith("fx_"):
            result = _fx_command(fx_service, payload)
            return web.json_response(result, dumps=_dumps)
        result = apply_command(state, payload)
        if result.get("ok") and save:
            save()  # 입력값 저장 — 재시작 시 복원 (§6.2-0)
        if payload.get("cmd") == "settings_global" and result.get("ok") and system:
            system.set_hl_daily_limit(state.settings.hl_daily_limit_usdc)  # 한도 즉시 반영
            system.set_carry_rates(  # 이자율 즉시 반영(이론가 재계산에 반영)
                state.settings.fx_carry_rate, state.settings.eq_carry_rate)
        if payload.get("cmd") == "shutdown" and result.get("ok") and on_shutdown:
            # 응답을 먼저 보내고 잠시 뒤 종료 (화면이 결과를 받을 시간)
            asyncio.get_running_loop().call_later(0.2, on_shutdown)
        return web.json_response(result, dumps=_dumps)

    async def get_manual_state(_request: web.Request) -> web.Response:
        # 수동 주문창 전용 폴링(호가·미체결·포지션·잔고) — /state를 무겁게 안 하려 분리.
        return web.json_response(manual_snapshot(system), dumps=_dumps)

    async def get_monitor(request: web.Request) -> web.Response:
        # 시세 모니터 전용 — est 입력(수량·기준%)을 쿼리로 받아 괴리보드 est까지 계산.
        try:
            qty = int(request.query.get("qty", "1"))
            en = float(request.query.get("en", "0")) / 100.0
            ex = float(request.query.get("ex", "0")) / 100.0
        except ValueError:
            qty, en, ex = 1, 0.0, 0.0
        return web.json_response(monitor_snapshot(system, qty, en, ex), dumps=_dumps)

    app = web.Application()
    app.router.add_get("/state", get_state)
    app.router.add_get("/manual_state", get_manual_state)
    app.router.add_get("/monitor", get_monitor)
    app.router.add_post("/command", post_command)
    return app


class _DailyFileHandler(logging.FileHandler):
    """자정에 파일을 바꾸는 로그 핸들러 — 항상 ``logs/core_<오늘>.log`` 에 쓴다.

    표준 TimedRotatingFileHandler는 활성 파일이 날짜 없는 이름(core.log)이고 회전분에만
    날짜가 붙어 '파일 이름=당일 날짜' 요구와 반대다. 그래서 기록할 때 날짜가 바뀌면 스스로
    오늘 날짜 파일로 갈아탄다 — 24시간 무중단이라 시작 시각 날짜에 고정되면 안 됨(Phase 8).
    """

    def __init__(self, log_dir: Path, prefix: str = "core") -> None:
        self._dir = log_dir
        self._prefix = prefix
        self._day = self._today()
        super().__init__(self._path(self._day), encoding="utf-8")

    @staticmethod
    def _today() -> str:
        import time

        return time.strftime("%Y%m%d")

    def _path(self, day: str) -> str:
        return str((self._dir / f"{self._prefix}_{day}.log").resolve())

    def emit(self, record: logging.LogRecord) -> None:
        day = self._today()
        if day != self._day:  # 자정 넘김 → 오늘 파일로 갈아탄다
            self._day = day
            self.baseFilename = self._path(day)
            if self.stream is not None:
                self.stream.close()
            self.stream = self._open()
        super().emit(record)


def _setup_logging() -> logging.Logger:
    """콘솔 + logs/core_날짜.log 파일 로그 (7-3a — 판정·발주 추적용). 자정 롤오버(Phase 8)."""
    import sys

    log_dir = _base_dir() / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
        file_handler: logging.Handler = _DailyFileHandler(log_dir)
    except OSError:
        file_handler = logging.NullHandler()
    handlers: list[logging.Handler] = [file_handler]
    # 콘솔 없이(pythonw·CREATE_NO_WINDOW) 돌면 sys.stderr가 None이라 StreamHandler가
    # 터진다 — 콘솔이 있을 때만 콘솔 출력, 없으면 파일 로그만.
    if sys.stderr is not None:
        handlers.insert(0, logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )
    # 거래소별 주문 로그 — HL/LS 각각 별도 파일(hl_order_날짜.log / ls_order_날짜.log),
    # 자정 롤오버. root로 전파 안 함(propagate=False) → core.log·콘솔과 분리(중복 없음).
    for name, prefix in (("kp_arb.order.hl", "hl_order"), ("kp_arb.order.ls", "ls_order")):
        olg = logging.getLogger(name)
        olg.setLevel(logging.INFO)
        olg.propagate = False
        if not any(isinstance(h, _DailyFileHandler) for h in olg.handlers):
            try:
                olg.addHandler(_DailyFileHandler(log_dir, prefix=prefix))
            except OSError:
                pass
    # WS 주문 원본 로그 — 거래소별(ws_hl/ws_ls), 수신 시각 필요 → 타임스탬프 포매터.
    # HL 핑/퐁 왕복(hl_ping)도 같은 형식 — 끊김 직전 연결유지 신호가 정상이었는지 추적.
    for name, prefix in (("kp_arb.wsraw.hl", "ws_hl"), ("kp_arb.wsraw.ls", "ws_ls"),
                         ("kp_arb.pingpong.hl", "hl_ping")):
        wlg = logging.getLogger(name)
        wlg.setLevel(logging.INFO)
        wlg.propagate = False
        if not any(isinstance(h, _DailyFileHandler) for h in wlg.handlers):
            try:
                wh = _DailyFileHandler(log_dir, prefix=prefix)
                wh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                wlg.addHandler(wh)
            except OSError:
                pass
    return logging.getLogger("kp_arb.core")


async def _serve() -> None:
    import aiohttp

    log = _setup_logging()
    try:
        from dotenv import load_dotenv

        # exe 옆(_base_dir)의 .env를 명시적으로 읽는다 — 배포판은 cwd가 exe 폴더가
        # 아닐 수 있어 인자 없는 load_dotenv()로는 못 찾을 수 있다. 파일 없으면 조용히 무시.
        load_dotenv(_base_dir() / ".env")
    except ImportError:
        pass
    state = load_state(STATE_PATH)  # 마지막 입력값 복원 (실행 상태는 항상 꺼짐)
    stop = asyncio.Event()
    async with aiohttp.ClientSession() as http:
        # LS/HL 접속 — 실패해도 API는 계속(화면 조작·입력 가능, 시세만 없음)
        system = None
        engine = None
        fx_service = None
        tasks: list[asyncio.Task[None]] = []
        try:
            from .bootstrap import bootstrap_live
            from .core_engine import RehearsalEngine
            from .fx_service import FxReportService

            system = await bootstrap_live(http)
            await system.start()
            engine = RehearsalEngine(state, system)
            fx_service = FxReportService(system)
            tasks.append(asyncio.create_task(engine.run()))
            tasks.append(asyncio.create_task(fx_service.run()))
            log.info("LiveSystem 결합 완료 — 리허설 판정 + FX 보고 시작 (발주 없음)")
        except Exception:  # noqa: BLE001 - 키 없음/네트워크 등
            log.exception("LiveSystem 시동 실패 — API만 운영 (시세 없음)")

        # access_log=None: 화면 폴링(GET /state 1초)이 로그를 도배하지 않게
        runner = web.AppRunner(make_app(
            state, on_shutdown=stop.set,
            save=lambda: save_state(STATE_PATH, state),
            system=system, engine=engine, fx_service=fx_service), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, HOST, DEFAULT_PORT)
        await site.start()
        log.info("코어 시동: http://%s:%s (안전종료는 메인 화면에서)", HOST, DEFAULT_PORT)
        await stop.wait()
        if system is not None:
            await system.stop()  # WS(_guarded_ws)·시동 태스크 명시 취소 — 종료 중 재접속 방지
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
