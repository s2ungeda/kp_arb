"""잔고 조회 창 — LS·HL 실조회 결과를 문자열 표로 보여 준다. 계약 = DESIGN-balance-query.md.

    python -m kp_arb.balance_query_screen [--preview]   (운영은 메인 메뉴 "잔고 조회")

- [조회]를 누르면 뒷단 스레드가 코어 명령 ``balance_query``를 보내고(최대 40초 대기) 응답의
  문자열 표를 상자에 둔다. 화면은 after()로 그 상자만 읽어 그린다(CLAUDE.md L5 — 화면 스레드에서
  네트워크 금지). 장부(OrderBook)와는 무관 — 코어가 문자열만 돌려준다.
- 버튼은 누르는 즉시 잠기고 **1초 뒤** 풀린다(사용자 2026-11-13). 조회가 그보다 늦으면 다시
  눌러도 화면이 "조회 중"으로 막는다.
- 자동조회(체크박스 + 주기 30·60초): 켜면 바로 한 번, 그 뒤 주기마다 조회. 켜 있는 동안
  [조회] 버튼은 잠긴다. 앞 조회가 안 끝났으면 그 차례는 건너뛴다. 체크·주기는 창 상태(win_state
  fields)에 저장해 다시 열 때 그대로(사용자 2026-11-16 "화면저장이 안 된다") — 켜진 채 저장됐으면
  열자마자 자동조회가 돈다.
- 표(사용자 2026-11-16): 열 HL 퍼프 | LS 주식(신용)·주선(차근) | 체결차, 행 하이닉스·삼성·현대·합
  ('기준' 행은 같은 날 뺌 — 코어의 시동 복사본·응답 baseline은 남김). 포지션 칸은 부호 없이
  ``100(50)``, 방향은 색(롱 빨강·숏 파랑·0 검정). 체결차는 부호를 보이고 0이 아니면 바탕 강조.
  조회 전 '-', 실패한 출처는 '오류'.
"""
from __future__ import annotations

import sys
import threading
from typing import Any

from . import ui_theme as T
from . import win_state
from .balance_query import (
    AUTO_INTERVALS_S,
    ERROR_TEXT,
    NOT_QUERIED,
    ROW_NAMES,
    ROW_ORDER,
    auto_interval_s,
    balance_line,
    empty_table,
    status_text,
)
from .core_client import core_request_err, screen_log, watch_parent_exit

LOCK_MS = 1000           # 조회 버튼 잠김 시간(사용자 지시 1초)
REQUEST_TIMEOUT_S = 40.0  # LS 두 TR(10초 상한×재시도) + HL 한 번을 기다릴 여유
# 화면 열: (머리글, 바깥 값 키, 괄호 값 키 또는 None)
SCREEN_COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    ("퍼프", "hl", None), ("주식(신용)", "stock", "credit"), ("주선(차근)", "sf", "sf_next"),
    ("체결차", "diff", None))
_PREVIEW_ROWS = {  # --preview 표본(코어 없이 화면 확인용)
    "sk_hynix": {"hl": "-310", "stock": "+100", "credit": "+50", "sf": "+20", "sf_next": "+10",
                 "diff": "-10"},
    "samsung": {"hl": "+200", "stock": "0", "credit": "0", "sf": "-20", "sf_next": "0",
                "diff": "0"},
    "hyundai": {"hl": "0", "stock": "0", "credit": "0", "sf": "0", "sf_next": "0", "diff": "0"}}
_PREVIEW_SUM = {"hl": "+510", "stock": "+100", "credit": "+50", "sf": "+40", "sf_next": "+10",
                "diff": "-10"}
_PREVIEW_RESULT: dict[str, Any] = {
    "ok": True, "ts": "14:05:12", "errors": {}, "rows": _PREVIEW_ROWS, "sum": _PREVIEW_SUM,
    "baseline": {"rows": _PREVIEW_ROWS, "sum": _PREVIEW_SUM}}


def cell_color(text: str) -> str:
    """칸 글 색 — 부호 수량이면 sign_color, '오류'는 실패색, '-'(조회 전)는 보조색(순수)."""
    if text == ERROR_TEXT:
        return T.C_ERR
    try:
        return T.sign_color(float(text.replace(",", "")))
    except ValueError:
        return T.C_MUTED


def _unsigned(text: str) -> str:
    return text if text in (ERROR_TEXT, NOT_QUERIED) else text.lstrip("+-")


def saved_auto(fields: dict[str, Any]) -> tuple[bool, str]:
    """저장된 창 필드 → (자동조회 켬, 주기 글). 없거나 이상하면 (False, 첫 주기)(순수)."""
    auto = bool(fields.get("auto", False))
    interval = str(auto_interval_s(fields.get("interval", "")))
    return auto, interval


def display_cell(outer: str, inner: str | None, *, signed: bool = False) -> str:
    """칸에 보일 글 — 포지션은 부호 없이 ``100(50)``(사용자 2026-11-16 "부호 표시는 안 해도 됨"),
    체결차(signed)는 부호 그대로. '-'·'오류'는 그대로(순수)."""
    if signed:
        return outer
    text = _unsigned(outer)
    if inner is None or outer in (ERROR_TEXT, NOT_QUERIED):
        return text
    return f"{text}({_unsigned(inner)})"


def diff_bg(text: str) -> str:
    """체결차 칸 바탕 — 0이 아닌 수량이면 강조색, 그 밖(0·'-'·'오류')은 흰색(순수)."""
    try:
        return T.C_HILITE_BG if float(text.replace(",", "")) != 0 else "white"
    except ValueError:
        return "white"


def table_rows(result: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """응답 → 화면 행(종목 3 + '합')의 칸 문자열. 조회 전·실패 응답이면 전부 '-'(순수).
    응답의 baseline(시동 복사본)은 화면에 안 그린다(사용자 2026-11-16 "기준 행은 빼자")."""
    if not result or not result.get("ok"):
        empty = empty_table()
        return {**empty["rows"], "sum": empty["sum"]}
    rows = {k: dict(v) for k, v in (result.get("rows") or {}).items()}
    rows["sum"] = dict(result.get("sum") or {})
    return rows


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
    auto_var = tk.BooleanVar(value=False)  # 저장값은 아래 restore_saved가 핸들러 연결 뒤 되살림
    chk_auto = tk.Checkbutton(top, text="자동조회", variable=auto_var)
    chk_auto.pack(side="left")
    tk.Label(top, text="주기").pack(side="left", padx=(8, 2))
    cmb_interval = ttk.Combobox(top, values=[str(s) for s in AUTO_INTERVALS_S], width=3,
                                state="readonly", font=T.FONT_NUM)  # 두 자리 숫자만큼(사용자)
    cmb_interval.set(str(AUTO_INTERVALS_S[0]))
    cmb_interval.pack(side="left")
    tk.Label(top, text="초").pack(side="left", padx=(2, 0))

    grid = tk.Frame(root, bg=T.C_BORDER, bd=1, relief="solid")
    grid.pack(padx=8, pady=4)
    head_bg = "#f0f0f0"
    opts: dict[str, Any] = {"padx": (0, 1), "pady": (0, 1), "sticky": "nsew"}
    # 머리 두 줄: HL | LS(2열 합침) | 체결차(2줄 합침) / 퍼프 | 주식(신용) | 주선(차근)
    tk.Label(grid, text="", width=9, bg=head_bg).grid(row=0, column=0, rowspan=2, **opts)
    tk.Label(grid, text="HL", bg=head_bg, font=T.FONT_LABEL).grid(row=0, column=1, **opts)
    tk.Label(grid, text="LS", bg=head_bg, font=T.FONT_LABEL).grid(
        row=0, column=2, columnspan=2, **opts)
    tk.Label(grid, text="체결차", bg=head_bg, font=T.FONT_LABEL).grid(
        row=0, column=4, rowspan=2, **opts)
    for col, (head, _k, _i) in enumerate(SCREEN_COLUMNS[:3], start=1):
        tk.Label(grid, text=head, width=11, bg=head_bg, font=T.FONT_LABEL).grid(
            row=1, column=col, **opts)
    cells: dict[tuple[str, str], tk.Label] = {}
    row_keys = [*(u.value for u in ROW_ORDER), "sum"]
    row_names = {**ROW_NAMES, "sum": "합"}
    for r, rk in enumerate(row_keys, start=2):
        tk.Label(grid, text=row_names[rk], width=9, bg=head_bg, anchor="e",
                 font=T.FONT_LABEL).grid(row=r, column=0, **opts)
        for col, (_head, key, _inner) in enumerate(SCREEN_COLUMNS, start=1):
            lbl = tk.Label(grid, text=NOT_QUERIED, width=11, bg="white", anchor="e",
                           font=T.FONT_NUM, fg=T.C_MUTED)
            lbl.grid(row=r, column=col, **opts)
            cells[(rk, key)] = lbl

    # 상태줄 한 줄: 왼쪽 조회 시각·결과, 오른쪽 '기준 300  잔고 : 500'(사용자 2026-11-17 "조회완료
    # 시간과 같은 라인에 두고 우측에") — 기준 = 시동 때 실조회한 토탈잔고(응답 baseline), 잔고 =
    # 이번 조회의 토탈잔고(합 행: 주식 + 주선×10 + HL). 둘 다 실조회 값(장부 아님). 11-11 메인 창 →
    # 11-16 이 창에서 뺌 → 11-17 다시.
    bar = tk.Frame(root)
    bar.pack(fill="x", padx=8, pady=(2, 8))
    status = tk.Label(bar, text=status_text(None), anchor="w", fg=T.C_MUTED, font=T.FONT_LABEL)
    status.pack(side="left")
    lbl_balance = tk.Label(bar, text=balance_line(None), anchor="e", font=T.FONT_LABEL)
    lbl_balance.pack(side="right")

    def show(result: dict[str, Any] | None) -> None:
        if result is None or result.get("ok"):  # 실패 응답(ok=False)은 표를 그대로 두고 상태줄만
            rows = table_rows(result)
            for (rk, key), lbl in cells.items():
                row = rows.get(rk) or {}
                outer = str(row.get(key, NOT_QUERIED))
                inner_key = next(i for _h, k, i in SCREEN_COLUMNS if k == key)
                inner = str(row.get(inner_key, NOT_QUERIED)) if inner_key else None
                signed = key == "diff"
                lbl.config(text=display_cell(outer, inner, signed=signed), fg=cell_color(outer),
                           bg=diff_bg(outer) if signed else "white")
        status.config(text=status_text(result),
                      fg=T.C_ERR if result and (not result.get("ok") or result.get("errors"))
                      else T.C_MUTED)
        if result is None or result.get("ok"):  # 실패 응답이면 앞 값 유지
            lbl_balance.config(text=balance_line(result))

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

    def persist_fields() -> None:  # 체크·주기 저장(사용자 2026-11-16 "화면저장이 안 된다")
        if not preview:
            win_state.save_fields("balance_query",
                                  {"auto": auto_var.get(), "interval": cmb_interval.get()})

    def on_auto_toggle() -> None:
        cancel_auto_job()
        if auto_var.get():
            btn.config(state="disabled")  # 자동조회 중엔 수동 버튼 잠김(사용자 지시)
            auto_tick()                   # 켜면 바로 한 번, 그 뒤 주기마다
        else:
            btn.config(state="normal")
        persist_fields()

    def on_interval_change(_event: object) -> None:
        if auto_var.get():  # 다음 조회부터 새 주기
            cancel_auto_job()
            auto_job["id"] = root.after(auto_interval_s(cmb_interval.get()) * 1000, auto_tick)
        persist_fields()

    chk_auto.config(command=on_auto_toggle)
    cmb_interval.bind("<<ComboboxSelected>>", on_interval_change)

    def restore_saved() -> None:
        auto, interval = saved_auto(win_state.saved_fields("balance_query"))
        cmb_interval.set(interval)
        if auto:
            auto_var.set(True)
            on_auto_toggle()  # 켜진 채 저장됐으면 열자마자 돈다(버튼 잠김·바로 1회)

    if not preview:
        restore_saved()
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
