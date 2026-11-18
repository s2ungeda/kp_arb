"""잔고 조회(LS·HL 실조회) — 결과를 문자열 표로. 계약 = DESIGN-balance-query.md(사용자 2026-11-13,
표 개정 2026-11-16).

장부(OrderBook)와 섞이지 않게, 게이트웨이 조회 결과를 **문자열 칸**으로만 만든다(``build_table``·
``fmt_qty``는 순수). ``query_balance``는 게이트웨이 세 출처(LS 주식·LS 선물·HL)를 부르기만 하고
장부·재동기 경로는 전혀 타지 않는다.
"""
from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from typing import Any

from .auto_m import HL_PER_SF
from .domain.enums import Account, Instrument, Underlying
from .domain.models import Position

# 응답 칸 키 — hl 퍼프 / stock 주식(현금+신용 합) / credit 신용 / sf 주선(최근+차근 합) /
# sf_next 차근 / diff 체결차
COLUMNS: tuple[str, ...] = ("hl", "stock", "credit", "sf", "sf_next", "diff")
# 행 순서는 사용자 표대로(하이닉스 → 삼성 → 현대)
ROW_ORDER: tuple[Underlying, ...] = (Underlying.SK_HYNIX, Underlying.SAMSUNG, Underlying.HYUNDAI)
ROW_NAMES: dict[str, str] = {"sk_hynix": "하이닉스", "samsung": "삼성", "hyundai": "현대"}
# 출처 → 그 출처가 채우는 칸(실패하면 이 칸과 체결차가 '오류')
SOURCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "hl": ("hl",), "ls_stock": ("stock", "credit"), "ls_deriv": ("sf", "sf_next")}
SOURCE_NAMES: dict[str, str] = {"hl": "HL", "ls_stock": "LS 주식", "ls_deriv": "LS 선물"}
NOT_QUERIED = "-"
ERROR_TEXT = "오류"
# 자동조회 주기 콤보(사용자 2026-11-13; 10초는 같은 날 뺌 — 조회 차선을 종일 쓰는 첫 기능이라 여유)
AUTO_INTERVALS_S: tuple[int, ...] = (30, 60)


def auto_interval_s(choice: object) -> int:
    """콤보 글 → 주기(초). 목록에 없는 값·빈 값은 첫 항목(30초)."""
    try:
        sec = int(str(choice).strip())
    except ValueError:
        return AUTO_INTERVALS_S[0]
    return sec if sec in AUTO_INTERVALS_S else AUTO_INTERVALS_S[0]


def fmt_qty(qty: float) -> str:
    """부호 있는 수량 글 — 롱 '+100', 숏 '-20', 없음 '0'. 소수는 있는 자리만(최대 4자리)."""
    q = round(float(qty), 4)  # 표시 자릿수보다 작은 조각(1e-5)은 '+0'이 아니라 '0'
    if q == 0:
        return "0"
    if q.is_integer():
        return f"{q:+,.0f}"
    return f"{q:+,.4f}".rstrip("0").rstrip(".")


# 원시 칸(게이트웨이 상품 → 칸) — 합치기 전
_RAW = ("hl", "stock_cash", "credit", "sf_near", "sf_next")


def _raw_column(p: Position, credit: bool) -> str | None:
    if p.instrument is Instrument.HL_PERP:
        return "hl"
    if p.instrument in (Instrument.KR_STOCK, Instrument.KR_ETF):
        return "credit" if credit else "stock_cash"
    if p.instrument is Instrument.KR_STOCK_FUTURE:
        return "sf_near"
    if p.instrument is Instrument.KR_STOCK_FUTURE_NEXT:
        return "sf_next"
    return None  # 원달러선물 등 — 이 표에 없음


def balance_numbers(lots: Iterable[tuple[Position, bool]]) -> dict[str, dict[str, float]]:
    """(포지션, 신용 여부) → {종목키: {hl, stock(현금+신용), credit, sf(최근+차근), sf_next, diff}}.

    diff(체결차) = stock + sf × HL_PER_SF + hl — 부호 그대로(exec §8 체결차 식과 같음, 사용자
    2026-11-16 "1 ok").
    """
    raw: dict[str, dict[str, float]] = {u.value: dict.fromkeys(_RAW, 0.0) for u in ROW_ORDER}
    for p, credit in lots:
        col = _raw_column(p, credit)
        row = raw.get(p.underlying.value)
        if col is None or row is None:
            continue
        row[col] += p.signed_qty
    out: dict[str, dict[str, float]] = {}
    for key, r in raw.items():
        stock = r["stock_cash"] + r["credit"]
        sf = r["sf_near"] + r["sf_next"]
        out[key] = {"hl": r["hl"], "stock": stock, "credit": r["credit"], "sf": sf,
                    "sf_next": r["sf_next"], "diff": stock + sf * HL_PER_SF + r["hl"]}
    return out


def sum_numbers(rows: dict[str, dict[str, float]]) -> dict[str, float]:
    """'합' 행 — 토탈잔고 구성(종목·상품별 절대값 합, DESIGN-balance-watch §0). 체결차만 부호 합."""
    total = dict.fromkeys(COLUMNS, 0.0)
    for r in rows.values():
        for col in COLUMNS:
            total[col] += r[col] if col == "diff" else abs(r[col])
    return total


def build_table(lots: Iterable[tuple[Position, bool]], *,
                failed: Iterable[str] = ()) -> dict[str, Any]:
    """(포지션, 신용 여부) 목록 → {"rows": {종목키: {칸: 글}}, "sum": {칸: 글}} — 전부 문자열.

    failed: 조회 실패한 출처 키("hl"·"ls_stock"·"ls_deriv") — 그 출처의 칸과 체결차는 '오류'.
    """
    nums = balance_numbers(lots)
    bad: set[str] = set()
    for src in failed:
        bad.update(SOURCE_COLUMNS.get(src, ()))
    if bad:
        bad.add("diff")  # 세 출처가 다 있어야 계산되는 칸
    rows = {key: {c: (ERROR_TEXT if c in bad else fmt_qty(v)) for c, v in r.items()}
            for key, r in nums.items()}
    total = {c: (ERROR_TEXT if c in bad else fmt_qty(v)) for c, v in sum_numbers(nums).items()}
    return {"rows": rows, "sum": total}


def empty_table() -> dict[str, Any]:
    """조회 전 표 — 전부 '-'."""
    return {"rows": {u.value: dict.fromkeys(COLUMNS, NOT_QUERIED) for u in ROW_ORDER},
            "sum": dict.fromkeys(COLUMNS, NOT_QUERIED)}


PRIORITY_ROWS = 4  # 우선수수료 탭 행 수(DESIGN-hl-priority-fee §2·§3 = strategy_core 구간 수)


def priority_rows_from_settings(settings: object) -> list[tuple[bool, str, str, str]]:
    """코어 설정(`/state`의 settings.hl_priority_fee.windows) → 탭 4행 (체크, 시작, 종료, bp 글).
    없거나 모양이 틀리면 빈 행. bp 0은 "0"(순수)."""
    rows: list[tuple[bool, str, str, str]] = []
    wins: Any = None
    if isinstance(settings, dict):
        fee = settings.get("hl_priority_fee")
        wins = fee.get("windows") if isinstance(fee, dict) else None
    for i in range(PRIORITY_ROWS):
        w = (wins[i] if isinstance(wins, list) and i < len(wins) and isinstance(wins[i], dict)
             else {})
        bp = w.get("bp", 0.0)
        bp_text = (f"{float(bp):g}" if isinstance(bp, int | float) and not isinstance(bp, bool)
                   else "")
        rows.append((bool(w.get("enabled", False)), str(w.get("start", "") or ""),
                     str(w.get("end", "") or ""), bp_text))
    return rows


def priority_rows_payload(rows: list[tuple[bool, str, str, str]]) -> dict[str, Any]:
    """탭 4행 → `settings_global` 명령 본문(`hl_priority_fee` 키만). bp 빈칸·오타는 0(= IOC만),
    시각은 다듬어 그대로(검사는 코어 — 틀리면 코어가 거부). 순수."""
    wins: list[dict[str, Any]] = []
    for enabled, start, end, bp_text in rows[:PRIORITY_ROWS]:
        try:
            bp = float(bp_text.strip().replace(",", ""))
        except ValueError:
            bp = 0.0
        wins.append({"enabled": bool(enabled), "start": start.strip(), "end": end.strip(),
                     "bp": bp})
    return {"cmd": "settings_global", "hl_priority_fee": {"windows": wins}}


def total_of(sum_row: dict[str, Any] | None) -> float | None:
    """'합' 행(문자열) → 토탈잔고 = Σ|주식| + Σ|주식선물|×10 + Σ|HL|(DESIGN-balance-watch §0의 옛
    정의, 실조회 값). 세 칸 중 하나라도 조회 전·오류면 None. 순수."""
    if not sum_row:
        return None
    try:
        stock = float(str(sum_row["stock"]).replace(",", ""))
        sf = float(str(sum_row["sf"]).replace(",", ""))
        hl = float(str(sum_row["hl"]).replace(",", ""))
    except (KeyError, ValueError, TypeError):
        return None
    return abs(stock) + abs(sf) * HL_PER_SF + abs(hl)


def balance_line(result: dict[str, Any] | None) -> str:
    """상태줄 오른쪽 '기준 300  잔고 : 500'(사용자 2026-11-17): 기준 = 시동 때 실조회한 토탈잔고
    (응답의 baseline), 잔고 = 이번 조회의 토탈잔고. 조회 전·실패면 '-'. 순수."""
    from .balance_watch import format_main_line

    if not result or not result.get("ok"):
        return format_main_line(None, None)
    base = result.get("baseline") or {}
    return format_main_line(total_of(base.get("sum")), total_of(result.get("sum")))


def status_text(result: dict[str, Any] | None) -> str:
    """상태줄 글 — 조회 전 / 완료 / 실패 출처."""
    if not result:
        return "조회 전 — [조회]를 누르면 LS·HL에 실조회합니다"
    if not result.get("ok"):
        errs = result.get("errors") or []
        return "조회 실패: " + "; ".join(str(e) for e in errs) if errs else "조회 실패"
    ts = result.get("ts") or "-"
    errors = result.get("errors") or {}
    if not errors:
        return f"{ts} 조회 완료"
    bad = ", ".join(f"{SOURCE_NAMES.get(k, k)}({str(v)[:60]})" for k, v in errors.items())
    return f"{ts} 일부 실패 — {bad}"


async def query_balance(ls: Any, hl: Any | None, *, now: float | None = None,
                        baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    """세 출처를 각각 조회해 문자열 표로. 출처마다 독립 — 하나가 실패해도 나머지는 채운다.

    장부·재동기 경로를 타지 않는다(DESIGN-balance-query §0). 돌려주는 값은 전부 문자열·사유.
    baseline: 코어가 시동 때 떠 둔 같은 표(문자열) — 응답에 그대로 싣는다(없으면 None).
    """
    lots: list[tuple[Position, bool]] = []
    errors: dict[str, str] = {}

    async def _ls(src: str, account: Account) -> None:
        try:
            detail: Sequence[tuple[Position, bool]] = await ls.get_positions_detail(account)
            lots.extend(detail)
        except Exception as exc:  # noqa: BLE001 - 출처별 실패는 사유로만(다른 출처는 계속)
            errors[src] = f"{type(exc).__name__}: {exc}"[:200]

    await _ls("ls_stock", Account.KR_STOCK)
    await _ls("ls_deriv", Account.KR_DERIV)
    if hl is None:
        errors["hl"] = "HL 연결 없음"
    else:
        try:
            lots.extend((p, False) for p in await hl.get_positions())
        except Exception as exc:  # noqa: BLE001
            errors["hl"] = f"{type(exc).__name__}: {exc}"[:200]
    ts = time.strftime("%H:%M:%S", time.localtime(now if now is not None else time.time()))
    table = build_table(lots, failed=errors)
    return {"ok": True, "ts": ts, "rows": table["rows"], "sum": table["sum"],
            "baseline": baseline, "errors": errors}
