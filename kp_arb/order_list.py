"""주문 리스트/미체결 관리 화면 — 코어 클라이언트 (DESIGN-manual-order.md §6.3).

전체 미체결 주문(HL·LS 공통)을 표로 보고 **취소·정정**한다. 코어 명령
manual_cancel/manual_amend 사용(주문창과 별도 화면). 화면 스레드는 네트워크 금지 —
전송·폴링은 뒷단 스레드 + 큐, 화면은 저장된 결과만 after()로 읽는다.

표는 ttk.Treeview가 아니라 **Label 그리드**(스크롤 캔버스) — 셀 단위 색을 주려고. 지금은
'매매' 칸만 매수=빨강/매도=파랑, 나머지는 검정. 스타일은 ui_theme 토큰(DESIGN-ui.md).
"""
from __future__ import annotations

import queue
from collections.abc import Callable
from typing import Any, cast

from . import ui_theme as T
from . import win_state
from .core_client import (
    core_request,
    run_state_feed,
    stale_seconds,
    watch_parent_exit,
)
from .order_hl import _fmt_px, _fmt_qty


def _venue(instrument: str) -> str:
    """거래소 구분 — HL perp만 HL, 나머지(국내 주식/선물)는 LS."""
    return "HL" if instrument == "hl_perp" else "LS"


def _sym(underlying: object, instrument: str) -> str:
    """종목 표시 — 거래소는 별도 컬럼이라 여기선 종목명(+선물 태그)만."""
    return f"{underlying} 선물" if instrument == "kr_stock_future" else f"{underlying}"


# 주문상태 한글 표시 — '구분'의 '주문'과 헷갈리지 않게 상태는 한글로(accepted=접수 등).
_ST_KR = {"new": "신규", "accepted": "접수", "partial": "부분", "filled": "체결",
          "cancelled": "취소", "rejected": "거부"}

# 표 컬럼: (제목, 최소폭px, 정렬). '매매'(index 2)만 색을 준다.
# 행 유형별 채움: 주문=주문가·수량·접수 / 체결=원주문+체결 전부 / 취소=주문가·수량·접수.
_COLS: tuple[tuple[str, int, str], ...] = (
    ("거래소", 42, "center"), ("종목", 88, "w"), ("매매", 40, "center"),
    ("주문가", 70, "e"), ("수량", 52, "e"), ("체결가", 70, "e"),
    ("체결량", 52, "e"), ("상태", 44, "center"), ("접수시각", 66, "center"),
    ("체결시각", 66, "center"), ("주문번호", 104, "e"))
_SIDE_COL = 2  # 색을 주는 유일한 칸(매매)
_NORM_BG = "white"
_SEL_BG = "#cce5ff"   # 선택 행 바탕
_HDR_BG = "#f0f0f0"   # 헤더 바탕(연회색)
_GRID_LINE = "#c8c8c8"  # 셀 사이 1px 구분선 — 프레임 bg가 틈으로 비침(시세 모니터와 동일)


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """주문 리스트 창 실행."""
    import threading
    import time
    import tkinter as tk
    from tkinter import ttk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료 (고아 방지)
    root = tk.Tk()
    root.title("주문 리스트 (미체결·취소·정정)")
    root.resizable(True, True)  # 크기 조절 — 표 행만 확장(root.rowconfigure weight)
    win_state.attach(root, "order_list")
    T.apply_base(root)

    # --- 명령 전송: 큐 → 전송 스레드 → 결과 큐 → 화면 루프 ---
    jobs: queue.Queue[tuple[dict[str, Any], str]] = queue.Queue()
    results: queue.Queue[tuple[str, dict[str, Any] | None]] = queue.Queue()

    def sender() -> None:
        while True:
            payload, label = jobs.get()
            results.put((label, core_request("/command", payload, timeout=10.0)))

    threading.Thread(target=sender, daemon=True).start()

    def send(payload: dict[str, Any], label: str) -> None:
        jobs.put((payload, label))

    # --- 상태 폴링: /manual_state → state_box (화면은 읽기만) ---
    state_box: dict[str, Any] = {"data": None}

    def poller() -> None:
        # 실시간(DESIGN §12.1): 메인이 기록하는 공유메모리를 0.1초마다 읽고, 없거나 낡으면
        # 기존 0.5초 HTTP 조회로 폴백. 실패해도 마지막 데이터 유지(merge_poll).
        run_state_feed(state_box, log_tag="주문리스트")

    threading.Thread(target=poller, daemon=True).start()

    # ===== 레이아웃 (grid: 표 행[1]만 세로 확장, 나머지 고정) =====
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

    # --- 유형 필터 (row 0) ---
    filt = tk.Frame(root)
    filt.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
    tk.Label(filt, text="표시").pack(side="left")
    _saved = win_state.saved_fields("order_list")  # 마지막 값 복원(없으면 전부 켬)
    show_orders = tk.BooleanVar(value=bool(_saved.get("show_orders", True)))
    show_fills = tk.BooleanVar(value=bool(_saved.get("show_fills", True)))
    show_cancels = tk.BooleanVar(value=bool(_saved.get("show_cancels", True)))

    def _on_filter() -> None:
        win_state.save_fields("order_list", {
            "show_orders": show_orders.get(),
            "show_fills": show_fills.get(),
            "show_cancels": show_cancels.get()})
        _rerender()

    tk.Checkbutton(filt, text="주문", variable=show_orders,
                   command=_on_filter).pack(side="left")
    tk.Checkbutton(filt, text="체결", variable=show_fills,
                   command=_on_filter).pack(side="left")
    tk.Checkbutton(filt, text="취소", variable=show_cancels,
                   command=_on_filter).pack(side="left")

    # --- 표: Label 그리드 + 스크롤 캔버스 (row 1, 확장) ---
    table = tk.Frame(root)
    table.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 2))
    canvas = tk.Canvas(table, highlightthickness=0, bg=_NORM_BG)
    vsb = ttk.Scrollbar(table, orient="vertical", command=canvas.yview)
    hsb = ttk.Scrollbar(table, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    vsb.pack(side="right", fill="y")
    hsb.pack(side="bottom", fill="x")
    canvas.pack(side="left", fill="both", expand=True)
    grid = tk.Frame(canvas, bg=_GRID_LINE)  # 이 회색이 셀 틈(1px)으로 비쳐 구분선이 됨
    _gwin = canvas.create_window((0, 0), window=grid, anchor="nw")
    grid.bind("<Configure>",
              lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    # 넓으면 캔버스 폭까지 채워 셀이 늘어나고(weight), 좁으면 자연폭 유지 → 가로 스크롤.
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
        _gwin, width=max(e.width, grid.winfo_reqwidth())))
    canvas.bind_all("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
    for c, (title, w, _a) in enumerate(_COLS):
        # weight → 창을 넓히면 컬럼(셀)도 폭에 비례해 늘어남. minsize는 최소폭.
        grid.columnconfigure(c, minsize=w, weight=w)
        tk.Label(grid, text=title, font=T.FONT_LABEL, bg=_HDR_BG).grid(
            row=0, column=c, sticky="nsew", padx=(0, 1), pady=(0, 1))  # 1px 틈=구분선

    cells: list[list[tk.Label]] = []          # 재사용 행 풀 — cells[r] = 9개 Label
    row_iid: list[str] = []                    # 각 행의 현재 iid(주문번호/__체결·__취소)
    row_vals: dict[str, tuple[Any, ...]] = {}  # iid → 값 튜플(정정/선택 편의용)
    sel: dict[str, str | None] = {"iid": None}

    def _highlight() -> None:
        nvis = int(state_box.get("_nvis") or 0)
        for r in range(nvis):
            bg = _SEL_BG if sel["iid"] and row_iid[r] == sel["iid"] else _NORM_BG
            for lbl in cells[r]:
                lbl.configure(bg=bg)

    def _fill_price_on_select() -> None:
        # 미체결 선택 시 정정가 칸이 **비어 있을 때만** 그 가격을 채운다(입력 보존).
        iid = sel["iid"]
        if not iid or iid.startswith("__") or e_price.get().strip():
            return
        vals = row_vals.get(iid)
        if vals and len(vals) >= 4 and vals[3] not in ("", "-"):  # 주문가(index 3)
            e_price.delete(0, "end")
            e_price.insert(0, str(vals[3]).replace(",", ""))

    def _select_row(r: int) -> None:
        if r < len(row_iid) and row_iid[r]:
            sel["iid"] = row_iid[r]
            _highlight()
            _fill_price_on_select()

    def _clicker(i: int) -> Callable[[Any], None]:
        return lambda _e: _select_row(i)  # 행 index 고정 — 재사용 행이라 클릭 시 현재 iid

    def _ensure_rows(n: int) -> None:
        while len(cells) < n:
            rr = len(cells)
            labels: list[tk.Label] = []
            for c, (_t, _w, a) in enumerate(_COLS):
                lbl = tk.Label(grid, font=T.FONT_LABEL, bg=_NORM_BG,
                               anchor=cast(Any, a), padx=3)
                lbl.grid(row=rr + 1, column=c, sticky="nsew",  # +1: 0행은 헤더
                         padx=(0, 1), pady=(0, 1))  # 1px 회색 구분선(프레임 bg 비침)
                lbl.bind("<Button-1>", _clicker(rr))
                labels.append(lbl)
            cells.append(labels)
            row_iid.append("")

    def set_status(text: str, err: bool = False) -> None:
        status.config(text=text[:90], fg=T.C_ERR if err else T.C_ZERO)

    def _selected_oid() -> str | None:
        iid = sel["iid"]
        return None if not iid or iid.startswith("__") else iid  # 체결·취소행 제외

    def do_cancel() -> None:
        oid = _selected_oid()
        if oid is None:
            set_status("취소할 미체결을 선택하세요", err=True)
            return
        send({"cmd": "manual_cancel", "order_id": oid}, "취소")

    def do_amend() -> None:
        oid = _selected_oid()
        if oid is None:
            set_status("정정할 미체결 주문을 선택하세요", err=True)
            return
        # HL은 정정 미지원(크로싱 시 원주문 소실 위험) — LS만 정정. 거래소 칸(index 0)으로 판별.
        vals = row_vals.get(oid)
        if vals and str(vals[0]) == "HL":
            set_status("정정 불가 — HL은 정정 미지원(취소 후 신규 주문)", err=True)
            return
        price = e_price.get().strip()
        if not price:
            set_status("정정가를 입력하세요", err=True)
            return
        try:
            new_px = float(price)
        except ValueError:
            set_status("정정가가 숫자가 아님", err=True)
            return
        send({"cmd": "manual_amend", "order_id": oid, "price": new_px}, "정정")

    # --- 정정가 입력 + 버튼 — 표시 체크박스와 같은 줄(filt) 오른쪽에 배치 ---
    ctrl = tk.Frame(filt)
    ctrl.pack(side="right")
    tk.Label(ctrl, text="정정가").pack(side="left")
    e_price = tk.Entry(ctrl, width=10, justify="right", font=T.FONT_NUM)
    e_price.pack(side="left", padx=(2, 8))
    tk.Button(ctrl, text="선택 정정", command=do_amend).pack(side="left")
    tk.Button(ctrl, text="선택 취소", command=do_cancel).pack(side="left", padx=4)
    tk.Label(ctrl, text="(LS 만 정정 가능)", fg=T.C_MUTED).pack(side="left", padx=(6, 0))

    # --- 상태바 (row 2, 맨 아래) ---
    status = tk.Label(root, text="-", anchor="w", relief="groove", width=1)
    status.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 6))

    # ===== 화면 갱신 (네트워크 없음 — 폴링 결과만 읽어 그림) =====
    def _reschedule(fn: Any, ms: int) -> None:
        try:
            root.after(ms, fn)
        except tk.TclError:
            pass  # 창 닫힘

    def drain_results() -> None:
        try:
            while True:
                label, result = results.get_nowait()
                if result is None:
                    set_status(f"{label} 실패 — 코어 미접속", err=True)
                elif not result.get("ok"):
                    set_status(f"{label} 거부 — {'; '.join(result.get('errors', []))}",
                               err=True)
                else:
                    oid = result.get("order_id")
                    set_status(f"{label} 접수됨" + (f" (#{oid})" if oid else ""))
        except queue.Empty:
            pass
        _reschedule(drain_results, 200)

    _TITLE = "주문 리스트 (미체결·취소·정정)"
    stale_box: dict[str, Any] = {"on": False, "title": None}

    def _update_staleness() -> None:
        # 조회가 밀리면 창 제목에 표시 + 상태줄에 시작/복구 1회 알림(마지막 데이터는 유지).
        age = stale_seconds(state_box, time.time())
        fails = int(state_box.get("fails", 0) or 0)
        stale = (age is not None and age > 3.0) or (age is None and fails > 0)
        if stale and not stale_box["on"]:
            stale_box["on"] = True
            set_status("코어 조회 실패 — 마지막 데이터로 표시 중", err=True)
        elif not stale and stale_box["on"]:
            stale_box["on"] = False
            set_status("코어 조회 복구")
        if stale:
            tail = f" — 갱신 지연 {age:.0f}초" if age is not None else " — 코어 미접속"
        else:
            tail = ""
        title = _TITLE + tail
        if title != stale_box["title"]:
            stale_box["title"] = title
            root.title(title)

    def refresh() -> None:
        try:
            _update_staleness()
            _render()
        except Exception:  # noqa: BLE001 - 갱신 오류로 창이 죽지 않게
            pass
        _reschedule(refresh, 150)  # 데이터가 0.1초 단위로 오니 그리기도 촘촘히(변화 없으면 스킵)

    def _rows() -> list[tuple[str, str, tuple[Any, ...]]]:
        # (iid, side, 값11) — 필터로 골라 한 표에. 주문(미체결)→체결→취소 순.
        # 열: 거래소·종목·매매·주문가·수량·체결가·체결량·상태·접수시각·체결시각·주문번호.
        data = state_box["data"] or {}
        out: list[tuple[str, str, tuple[Any, ...]]] = []
        if show_orders.get():
            for o in data.get("open_orders") or []:
                buy = o.get("side") == "buy"
                inst = str(o.get("instrument"))
                stk = _ST_KR.get(str(o.get("status")), o.get("status"))
                out.append((str(o.get("order_id")), "buy" if buy else "sell",
                            (_venue(inst), _sym(o.get("underlying"), inst),
                             "매수" if buy else "매도",
                             _fmt_px(o.get("price")), _fmt_qty(o.get("qty")),
                             "", "",  # 체결가·체결량 공백(미체결)
                             stk, o.get("time", ""), "",  # 접수시각·체결시각(공백)
                             str(o.get("order_id")))))
        if show_fills.get():
            for i, f in enumerate(data.get("fills") or []):
                buy = f.get("side") == "buy"
                inst = str(f.get("instrument"))
                out.append((f"__fill{i}", "buy" if buy else "sell",
                            (_venue(inst), _sym(f.get("underlying"), inst),
                             "매수" if buy else "매도",
                             _fmt_px(f.get("order_price")),  # 원주문 주문가
                             _fmt_qty(f.get("order_qty")),   # 원주문 수량
                             _fmt_px(f.get("price")), _fmt_qty(f.get("qty")),
                             "체결", f.get("accept_time", ""), f.get("time", ""),
                             str(f.get("order_id", "")))))
        if show_cancels.get():
            for i, c in enumerate(data.get("cancels") or []):
                buy = c.get("side") == "buy"
                inst = str(c.get("instrument"))
                out.append((f"__cancel{i}", "buy" if buy else "sell",
                            (_venue(inst), _sym(c.get("underlying"), inst),
                             "매수" if buy else "매도",
                             _fmt_px(c.get("price")), _fmt_qty(c.get("qty")),
                             "", "",  # 체결가·체결량 공백(취소행)
                             "취소", c.get("accept_time", ""), "",  # 접수시각·체결시각(공백)
                             str(c.get("order_id", "")))))
        return out

    def _render() -> None:
        rows = _rows()
        sig = tuple((iid, *vals) for iid, _, vals in rows)
        if sig == state_box.get("_sig"):
            return  # 변화 없으면 다시 안 그림(선택·스크롤 유지)
        state_box["_sig"] = sig
        _ensure_rows(len(rows))
        row_vals.clear()
        for r, (iid, side, vals) in enumerate(rows):
            row_iid[r] = iid
            row_vals[iid] = vals
            for c, text in enumerate(vals):
                fg = (T.C_BUY if side == "buy" else T.C_SELL) if c == _SIDE_COL \
                    else T.C_ZERO  # 매매 칸만 색, 나머지 검정
                cells[r][c].configure(text=text, fg=fg)
                cells[r][c].grid()  # 숨겼던 행 되살림
        for r in range(len(rows), len(cells)):  # 남는 행 숨김
            row_iid[r] = ""
            for lbl in cells[r]:
                lbl.grid_remove()
        state_box["_nvis"] = len(rows)
        if sel["iid"] not in row_vals:  # 선택 행이 사라졌으면 해제
            sel["iid"] = None
        _highlight()

    def _rerender() -> None:
        state_box["_sig"] = None  # 필터 바뀜 → 강제 재그림
        _render()

    # 최소 크기 — 가로는 **상단 컨트롤이 들어갈 폭**까지만(표는 좁아지면 가로 스크롤).
    root.update_idletasks()
    root.minsize(filt.winfo_reqwidth() + 24, 200)

    drain_results()
    refresh()
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
