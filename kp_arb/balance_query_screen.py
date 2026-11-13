"""잔고 조회 창 — LS·HL 실조회 결과를 문자열 표로 보여 준다. 계약 = DESIGN-balance-query.md.

    python -m kp_arb.balance_query_screen [--preview]   (운영은 메인 메뉴 "잔고 조회")

- [조회]를 누르면 뒷단 스레드가 코어 명령 ``balance_query``를 보내고(최대 40초 대기) 응답의
  문자열 표(``cells``)를 상자에 둔다. 화면은 after()로 그 상자만 읽어 그린다(CLAUDE.md L5 —
  화면 스레드에서 네트워크 금지). 장부(OrderBook)와는 무관 — 코어가 문자열만 돌려준다.
- 버튼은 누르는 즉시 잠기고 **1초 뒤** 풀린다(사용자 2026-11-13). 조회가 그보다 늦으면 다시
  눌러도 화면이 "조회 중"으로 막는다.
- 자동조회(체크박스 + 주기 10·30·60초): 켜면 바로 한 번, 그 뒤 주기마다 조회. 켜 있는 동안
  [조회] 버튼은 잠긴다. 앞 조회가 안 끝났으면 그 차례는 건너뛴다. 저장 안 함(열면 꺼짐).
- 표: 행 하이닉스·삼성·현대(목업 순서), 열 HL 퍼프 | LS 주식·신용·주선(최근)·주선(차선).
  값 색은 부호(+빨강 −파랑 0검정), 조회 전 '-', 실패한 출처 열은 '오류'.
"""
from __future__ import annotations

import sys
import threading
from typing import Any

from . import ui_theme as T
from . import win_state
from .balance_query import (
    AUTO_INTERVALS_S,
    COLUMN_TITLES,
    COLUMNS,
    ERROR_TEXT,
    ROW_NAMES,
    ROW_ORDER,
    auto_interval_s,
    empty_cells,
    status_text,
)
from .core_client import core_request_err, screen_log, watch_parent_exit

LOCK_MS = 1000           # 조회 버튼 잠김 시간(사용자 지시 1초)
REQUEST_TIMEOUT_S = 40.0  # LS 두 TR(10초 상한×재시도) + HL 한 번을 기다릴 여유
_PREVIEW_RESULT: dict[str, Any] = {  # --preview 표본(코어 없이 화면 확인용)
    "ok": True, "ts": "14:05:12", "errors": {},
    "cells": {"sk_hynix": {"hl": "+100", "stock": "0", "credit": "+50", "sf_near": "-10",
                           "sf_next": "0"},
              "samsung": {"hl": "-200", "stock": "+200", "credit": "0", "sf_near": "0",
                          "sf_next": "+5"},
              "hyundai": {"hl": "0", "stock": "0", "credit": "0", "sf_near": "0",
                          "sf_next": "0"}}}


def cell_color(text: str) -> str:
    """칸 글 색 — 부호 수량이면 sign_color, '오류'는 실패색, '-'(조회 전)는 보조색(순수)."""
    if text == ERROR_TEXT:
        return T.C_ERR
    try:
        return T.sign_color(float(text.replace(",", "")))
    except ValueError:
        return T.C_MUTED


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """창 실행."""
    import tkinter as tk
    from tkinter import ttk

    preview = "--preview" in sys.argv
    if not preview:
        watch_parent_exit()  # 메인이 죽으면 이 창도 종료(고아 방지)
    box: dict[str, Any] = {"result": None, "seq": 0, "busy": False}

    def query_worker() -> None:  # 뒷단 스레드 — 코어 명령 1회, 응답을 상자에
        data, err = core_request_err("/command", {"cmd": "balance_query"},
                                     timeout=REQUEST_TIMEOUT_S)
        if data is None:
            data = {"ok": False, "errors": [f"코어 응답 없음 — {err or '원인 미상'}"]}
            screen_log().warning("잔고 조회 실패 — %s", err)
        box["result"] = data
        box["seq"] += 1
        box["busy"] = False

    root = tk.Tk()
    from .core_client import log_screen_timing
    log_screen_timing(root, __name__)
    T.apply_base(root)
    root.title("잔고 조회 (LS·HL 실조회)")
    root.resizable(False, False)
    win_state.attach(root, "balance_query")

    top = tk.Frame(root)
    top.pack(fill="x", padx=8, pady=(8, 2))
    btn = tk.Button(top, text="조회", width=8)
    btn.pack(side="right")
    auto_var = tk.BooleanVar(value=False)  # 저장 안 함 — 창을 열면 꺼짐
    chk_auto = tk.Checkbutton(top, text="자동조회", variable=auto_var)
    chk_auto.pack(side="left")
    tk.Label(top, text="주기").pack(side="left", padx=(8, 2))
    cmb_interval = ttk.Combobox(top, values=[str(s) for s in AUTO_INTERVALS_S], width=4,
                                state="readonly", font=T.FONT_NUM)
    cmb_interval.set(str(AUTO_INTERVALS_S[0]))
    cmb_interval.pack(side="left")
    tk.Label(top, text="초").pack(side="left", padx=(2, 0))

    grid = tk.Frame(root, bg=T.C_BORDER, bd=1, relief="solid")
    grid.pack(padx=8, pady=4)
    head_bg = "#f0f0f0"
    opts: dict[str, Any] = {"padx": (0, 1), "pady": (0, 1), "sticky": "nsew"}
    # 머리 두 줄: HL | LS(4열 합침) / 퍼프 | 주식 | 신용 | 주선(최근) | 주선(차선)
    tk.Label(grid, text="", width=9, bg=head_bg).grid(row=0, column=0, rowspan=2, **opts)
    tk.Label(grid, text="HL", bg=head_bg, font=T.FONT_LABEL).grid(row=0, column=1, **opts)
    tk.Label(grid, text="LS", bg=head_bg, font=T.FONT_LABEL).grid(
        row=0, column=2, columnspan=4, **opts)
    for col, key in enumerate(COLUMNS, start=1):
        tk.Label(grid, text=COLUMN_TITLES[key], width=10, bg=head_bg,
                 font=T.FONT_LABEL).grid(row=1, column=col, **opts)
    cells: dict[tuple[str, str], tk.Label] = {}
    for r, u in enumerate(ROW_ORDER, start=2):
        tk.Label(grid, text=ROW_NAMES[u.value], width=9, bg=head_bg, anchor="e",
                 font=T.FONT_LABEL).grid(row=r, column=0, **opts)
        for col, key in enumerate(COLUMNS, start=1):
            lbl = tk.Label(grid, text="-", width=10, bg="white", anchor="e", font=T.FONT_NUM,
                           fg=T.C_MUTED)
            lbl.grid(row=r, column=col, **opts)
            cells[(u.value, key)] = lbl

    status = tk.Label(root, text=status_text(None), anchor="w", fg=T.C_MUTED,
                      font=T.FONT_LABEL)
    status.pack(fill="x", padx=8, pady=(2, 8))

    def show(result: dict[str, Any] | None) -> None:
        table = (result or {}).get("cells") if result and result.get("ok") else None
        if not isinstance(table, dict):
            table = empty_cells() if result is None else None
        if table is not None:  # 실패 응답(ok=False)은 표를 그대로 두고 상태줄만 바꾼다
            for (u, key), lbl in cells.items():
                text = str((table.get(u) or {}).get(key, "-"))
                lbl.config(text=text, fg=cell_color(text))
        status.config(text=status_text(result),
                      fg=T.C_ERR if result and (not result.get("ok") or result.get("errors"))
                      else T.C_MUTED)

    def unlock() -> None:
        if auto_var.get():
            return  # 자동조회 중엔 버튼을 잠근 채 둔다
        try:
            btn.config(state="normal")
        except tk.TclError:
            pass

    def start_query() -> bool:
        """조회 1회 시작(뒷단 스레드). 앞 조회가 안 끝났으면 False."""
        if preview:
            box["result"] = _PREVIEW_RESULT
            box["seq"] += 1
            return True
        if box["busy"]:
            return False
        box["busy"] = True
        status.config(text="조회 중 ...", fg=T.C_MUTED)
        threading.Thread(target=query_worker, daemon=True).start()
        return True

    def on_query() -> None:
        btn.config(state="disabled")  # 잠김 → 1초 뒤 풀림(조회 응답과 무관)
        root.after(LOCK_MS, unlock)
        if not start_query():
            status.config(text="조회 중 — 잠시 뒤 다시", fg=T.C_ERR)

    btn.config(command=on_query)

    auto_job: dict[str, str | None] = {"id": None}  # 예약된 after() 아이디

    def cancel_auto_job() -> None:
        if auto_job["id"] is not None:
            try:
                root.after_cancel(auto_job["id"])
            except tk.TclError:
                pass
            auto_job["id"] = None

    def auto_tick() -> None:
        auto_job["id"] = None
        if not auto_var.get():
            return
        start_query()  # 앞 조회가 안 끝났으면 이번 차례는 건너뜀(겹침 없음)
        auto_job["id"] = root.after(auto_interval_s(cmb_interval.get()) * 1000, auto_tick)

    def on_auto_toggle() -> None:
        cancel_auto_job()
        if auto_var.get():
            btn.config(state="disabled")  # 자동조회 중엔 수동 버튼 잠김(사용자 지시)
            auto_tick()                   # 켜면 바로 한 번, 그 뒤 주기마다
        else:
            btn.config(state="normal")

    def on_interval_change(_event: object) -> None:
        if auto_var.get():  # 다음 조회부터 새 주기
            cancel_auto_job()
            auto_job["id"] = root.after(auto_interval_s(cmb_interval.get()) * 1000, auto_tick)

    chk_auto.config(command=on_auto_toggle)
    cmb_interval.bind("<<ComboboxSelected>>", on_interval_change)
    shown = {"seq": 0}

    def poll() -> None:
        try:
            if box["seq"] != shown["seq"]:
                shown["seq"] = box["seq"]
                show(box["result"])
        except tk.TclError:
            return
        except Exception:  # noqa: BLE001 - 1회 실패는 기록만 하고 계속
            screen_log().exception("잔고 조회 화면 갱신 실패 — 계속")
        finally:
            try:
                root.after(200, poll)
            except tk.TclError:
                pass

    if preview:
        root.after(300, on_query)  # 표본값을 바로 채워 캡처
    poll()
    while True:
        try:
            root.mainloop()
            break
        except KeyboardInterrupt:
            try:
                root.winfo_exists()
            except tk.TclError:
                break


if __name__ == "__main__":
    main()
