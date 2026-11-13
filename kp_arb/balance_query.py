"""잔고 조회(LS·HL 실조회) — 결과를 문자열 표로. 계약 = DESIGN-balance-query.md(사용자 2026-11-13).

장부(OrderBook)와 섞이지 않게, 게이트웨이 조회 결과를 **문자열 칸**으로만 만든다(``build_cells``·
``fmt_qty``는 순수). ``query_balance``는 게이트웨이 세 출처(LS 주식·LS 선물·HL)를 부르기만 하고
장부·재동기 경로는 전혀 타지 않는다.
"""
from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from typing import Any

from .domain.enums import Account, Instrument, Underlying
from .domain.models import Position

# 열 키(화면 순서) — HL 퍼프 | LS 주식(현금) | 신용 | 주선(최근) | 주선(차선)
COLUMNS: tuple[str, ...] = ("hl", "stock", "credit", "sf_near", "sf_next")
COLUMN_TITLES: dict[str, str] = {"hl": "퍼프", "stock": "주식", "credit": "신용",
                                 "sf_near": "주선(최근)", "sf_next": "주선(차선)"}
# 행 순서는 목업대로(하이닉스 → 삼성 → 현대)
ROW_ORDER: tuple[Underlying, ...] = (Underlying.SK_HYNIX, Underlying.SAMSUNG, Underlying.HYUNDAI)
ROW_NAMES: dict[str, str] = {"sk_hynix": "하이닉스", "samsung": "삼성", "hyundai": "현대"}
# 출처 → 그 출처가 채우는 열(실패하면 이 열만 '오류')
SOURCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "hl": ("hl",), "ls_stock": ("stock", "credit"), "ls_deriv": ("sf_near", "sf_next")}
SOURCE_NAMES: dict[str, str] = {"hl": "HL", "ls_stock": "LS 주식", "ls_deriv": "LS 선물"}
NOT_QUERIED = "-"
ERROR_TEXT = "오류"
AUTO_INTERVALS_S: tuple[int, ...] = (10, 30, 60)  # 자동조회 주기 콤보(사용자 2026-11-13)


def auto_interval_s(choice: object) -> int:
    """콤보 글 → 주기(초). 목록에 없는 값·빈 값은 첫 항목(10초)."""
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


def _column_of(p: Position, credit: bool) -> str | None:
    if p.instrument is Instrument.HL_PERP:
        return "hl"
    if p.instrument in (Instrument.KR_STOCK, Instrument.KR_ETF):
        return "credit" if credit else "stock"
    if p.instrument is Instrument.KR_STOCK_FUTURE:
        return "sf_near"
    if p.instrument is Instrument.KR_STOCK_FUTURE_NEXT:
        return "sf_next"
    return None  # 원달러선물 등 — 이 표에 없음


def build_cells(lots: Iterable[tuple[Position, bool]], *,
                failed: Iterable[str] = ()) -> dict[str, dict[str, str]]:
    """(포지션, 신용 여부) 목록 → {종목키: {열: 글}}. 같은 칸은 부호 수량을 합친다.

    failed: 조회 실패한 출처 키("hl"·"ls_stock"·"ls_deriv") — 그 출처의 열은 '오류'로 둔다.
    """
    sums: dict[tuple[str, str], float] = {}
    for p, credit in lots:
        col = _column_of(p, credit)
        if col is None:
            continue
        key = (p.underlying.value, col)
        sums[key] = sums.get(key, 0.0) + p.signed_qty
    cells = {u.value: {c: fmt_qty(sums.get((u.value, c), 0.0)) for c in COLUMNS}
             for u in ROW_ORDER}
    for src in failed:
        for col in SOURCE_COLUMNS.get(src, ()):
            for row in cells.values():
                row[col] = ERROR_TEXT
    return cells


def empty_cells() -> dict[str, dict[str, str]]:
    """조회 전 표 — 전부 '-'."""
    return {u.value: dict.fromkeys(COLUMNS, NOT_QUERIED) for u in ROW_ORDER}


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


async def query_balance(ls: Any, hl: Any | None, *,
                        now: float | None = None) -> dict[str, Any]:
    """세 출처를 각각 조회해 문자열 표로. 출처마다 독립 — 하나가 실패해도 나머지는 채운다.

    장부·재동기 경로를 타지 않는다(DESIGN-balance-query §0). 돌려주는 값은 전부 문자열·사유.
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
    return {"ok": True, "ts": ts, "cells": build_cells(lots, failed=errors), "errors": errors}
