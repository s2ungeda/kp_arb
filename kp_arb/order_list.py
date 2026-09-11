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
    tag = {"kr_stock_future": " 선물", "kr_stock_future_next": " 선물(차)"}.get(instrument, "")
    return f"{underlying}{tag}"


def _src_label(source: object) -> str:
    """출처 표시 — 자동M / 일반(일반주문창) / 따라가기 / '-'(미상: 시동 조회로 알게 된 주문 등)."""
    s = str(source or "")
    if s == "자동M":
        return "자동M"
    if s == "일반주문창":
        return "일반"
    return s or "-"


# 필터 콤보 항목(사용자 2026-09-11): 거래소·종목·매매·출처. "전체"는 필터 없음.
_F_VENUE = ("전체", "LS", "HL")
_F_UNDER: dict[str, str | None] = {"전체": None, "삼성": "samsung", "하이닉스": "sk_hynix",
                                   "현대차": "hyundai"}
_F_SIDE: dict[str, str | None] = {"전체": None, "매수": "buy", "매도": "sell"}
_F_SOURCE = ("전체", "자동M", "일반주문")


def row_visible(filters: dict[str, str], venue: str, underlying: str, side: str,
                source: str) -> bool:
    """필터 4개(거래소·종목·매매·출처)를 행 하나에 적용. 출처 '일반주문' = 자동M이 아닌 전부
    (일반주문창·따라가기·미상)."""
    if filters.get("venue", "전체") != "전체" and venue != filters["venue"]:
        return False
    want_u = _F_UNDER.get(filters.get("under", "전체"))
    if want_u is not None and underlying != want_u:
        return False
    want_s = _F_SIDE.get(filters.get("side", "전체"))
    if want_s is not None and side != want_s:
        return False
    src = filters.get("source", "전체")
    if src == "자동M" and source != "자동M":
        return False
    if src == "일반주문" and source == "자동M":
        return False
    return True


# 주문상태 한글 표시 — '구분'의 '주문'과 헷갈리지 않게 상태는 한글로(accepted=접수 등).
_TITLE = "주문 리스트 (미체결·취소·정정)"


def window_title(age: float | None, fails: int, hidden: int) -> str:
    """창 제목 — 기본 이름 + 조회 지연/미접속 + 필터로 숨긴 건수. 순수 로직.

    숨긴 건수는 0이면 안 붙인다. 지연 표시가 먼저, 숨김이 뒤.
    """
    stale = (age is not None and age > 3.0) or (age is None and fails > 0)
    parts = [_TITLE]
    if stale:
        parts.append(f"갱신 지연 {age:.0f}초" if age is not None else "코어 미접속")
    if hidden > 0:
        parts.append(f"필터로 {hidden}건 숨김")
    return " — ".join(parts)


_ST_KR = {"new": "신규", "accepted": "접수", "partial": "부분", "filled": "체결",
          "cancelled": "취소", "rejected": "거부"}

# 표 컬럼: (제목, 최소폭px, 정렬). '매매'(index 2)만 색을 준다.
# 행 유형별 채움: 주문=주문가·수량·접수 / 체결=원주문+체결 전부 / 취소=주문가·수량·접수.
_COLS: tuple[tuple[str, int, str], ...] = (
    ("거래소", 50, "center"), ("종목", 88, "w"), ("매매", 50, "center"),  # 콤보 글자 안 잘리게
    ("주문가", 70, "e"), ("수량", 52, "e"), ("체결가", 70, "e"),
    ("체결량", 52, "e"), ("상태", 44, "center"), ("접수시각", 66, "center"),
    ("체결시각", 66, "center"), ("주문번호", 104, "e"),
    ("출처", 52, "center"))  # 출처(자동M/일반/따라가기) — 맨 끝(앞 칸 index 유지, 2026-09-11)
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
    root.title(_TITLE)
    root.resizable(True, True)  # 크기 조절 — 표 행만 확장(root.rowconfigure weight)
    # 기본 크기 — 컬럼 전부 + 여유가 보이는 폭(사용자 2026-09-11). 최소폭 고정을 없애면서 자연
    # 크기가 아주 작아졌으므로 명시. 위치는 win_state가 복원, 크기는 매번 이 값.
    root.geometry("820x370")
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

    # ===== 레이아웃 (grid: 필터[0]·컬럼 헤더[1] 고정, 표[2]만 세로 확장, 상태바[3]) =====
    root.columnconfigure(0, weight=1)
    root.rowconfigure(2, weight=1)

    # --- 필터 줄 (row 0) — 표 컬럼과 같은 격자에 놓아 콤보가 해당 컬럼 위에 오게(이름 라벨 없이,
    # 사용자 2026-09-11). 표의 세로 스크롤바 폭만큼 고정 칸을 끝에 두어 늘어나는 폭을 표와 맞춘다.
    # 필터 줄도 표처럼 캔버스 안에 두어 창을 좁히면 표와 함께 가로로 가려진다(창 최소폭을 안 만듦,
    # 사용자 2026-09-11). 가로 스크롤은 표의 스크롤바 하나로 둘을 같이 움직인다.
    fbar = tk.Canvas(root, highlightthickness=0, width=1, height=1)
    fbar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
    filt = tk.Frame(fbar)
    _fwin = fbar.create_window((0, 0), window=filt, anchor="nw")
    filt.bind("<Configure>", lambda e: fbar.configure(
        height=e.height, scrollregion=fbar.bbox("all")))
    for c, (_t, w, _a) in enumerate(_COLS):
        filt.columnconfigure(c, minsize=w, weight=w)
    filt.columnconfigure(len(_COLS), minsize=18, weight=0)  # ≈ 스크롤바 폭
    # 필터(유형 체크 3개 + 항목 콤보 4개)는 마지막 값을 저장·복원한다(없으면 전부 표시).
    # 복원된 필터가 미체결을 가리고 있으면 창 제목에 "필터로 n건 숨김"이 보인다(실측 2026-09-11:
    # 지난번 필터가 복원돼 시동 조회 미체결 8건이 안 보였고 조회를 안 하는 줄 알았음).
    _saved = win_state.saved_fields("order_list")
    show_orders = tk.BooleanVar(value=bool(_saved.get("show_orders", True)))
    show_fills = tk.BooleanVar(value=bool(_saved.get("show_fills", True)))
    show_cancels = tk.BooleanVar(value=bool(_saved.get("show_cancels", True)))

    # 항목 필터 콤보 4개(거래소·종목·매매·출처, 사용자 2026-09-11) — 마지막 값 복원
    combos: dict[str, ttk.Combobox] = {}

    def _filters() -> dict[str, str]:
        return {k: cb.get() for k, cb in combos.items()}

    def _on_filter(_e: object = None) -> None:
        win_state.save_fields("order_list", {
            "show_orders": show_orders.get(),
            "show_fills": show_fills.get(),
            "show_cancels": show_cancels.get(),
            **{f"f_{k}": v for k, v in _filters().items()}})
        _rerender()

    def _combo(key: str, values: tuple[str, ...], col: int, span: int = 1,
               sticky: str = "ew", width: int = 4) -> None:
        cb = ttk.Combobox(filt, values=list(values), width=width, state="readonly")
        saved_v = _saved.get(f"f_{key}")
        cb.set(saved_v if saved_v in values else "전체")
        cb.grid(row=0, column=col, columnspan=span, sticky=sticky, padx=(0, 4))
        cb.bind("<<ComboboxSelected>>", _on_filter)
        combos[key] = cb

    _combo("venue", _F_VENUE, 0)             # '거래소' 컬럼 위
    _combo("under", tuple(_F_UNDER), 1)      # '종목' 컬럼 위
    _combo("side", tuple(_F_SIDE), 2)        # '매매' 컬럼 위
    checks = tk.Frame(filt)                  # 유형 체크 3개 — 주문가~체결가 컬럼 자리
    checks.grid(row=0, column=3, columnspan=3, sticky="w")
    tk.Checkbutton(checks, text="주문", variable=show_orders,
                   command=_on_filter).pack(side="left")
    tk.Checkbutton(checks, text="체결", variable=show_fills,
                   command=_on_filter).pack(side="left")
    tk.Checkbutton(checks, text="취소", variable=show_cancels,
                   command=_on_filter).pack(side="left")
    _combo("source", _F_SOURCE, 6, span=2, sticky="w", width=8)  # 출처 — 컬럼 자리와 무관

    # --- 컬럼 헤더 (row 1) — 세로 스크롤에 안 밀리게 표 밖에 따로 두고, 가로만 표와 같이 움직인다
    # (사용자 2026-09-11: 스크롤하면 컬럼 제목이 사라짐).
    hbar = tk.Canvas(root, highlightthickness=0, width=1, height=1, bg=_GRID_LINE)
    hbar.grid(row=1, column=0, sticky="ew", padx=6, pady=(2, 0))
    head = tk.Frame(hbar, bg=_GRID_LINE)
    _hwin = hbar.create_window((0, 0), window=head, anchor="nw")
    head.bind("<Configure>", lambda e: hbar.configure(
        height=e.height, scrollregion=hbar.bbox("all")))
    for c, (title, w, _a) in enumerate(_COLS):
        head.columnconfigure(c, minsize=w, weight=w)
        tk.Label(head, text=title, font=T.FONT_LABEL, bg=_HDR_BG).grid(
            row=0, column=c, sticky="nsew", padx=(0, 1), pady=(0, 1))  # 1px 틈=구분선

    # --- 표: Label 그리드 + 스크롤 캔버스 (row 2, 확장) ---
    table = tk.Frame(root)
    table.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 2))
    canvas = tk.Canvas(table, highlightthickness=0, bg=_NORM_BG, width=1, height=1)
    vsb = ttk.Scrollbar(table, orient="vertical", command=canvas.yview)

    def _xview(*args: Any) -> None:  # 표·컬럼 헤더·필터 줄을 같이 가로 스크롤
        canvas.xview(*args)
        hbar.xview(*args)
        fbar.xview(*args)

    hsb = ttk.Scrollbar(table, orient="horizontal", command=_xview)
    canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    vsb.pack(side="right", fill="y")
    hsb.pack(side="bottom", fill="x")
    canvas.pack(side="left", fill="both", expand=True)
    grid = tk.Frame(canvas, bg=_GRID_LINE)  # 이 회색이 셀 틈(1px)으로 비쳐 구분선이 됨
    _gwin = canvas.create_window((0, 0), window=grid, anchor="nw")
    def _fit_scroll(_e: Any = None) -> None:
        # 내용이 캔버스보다 작으면 위로 붙여 둔다 — 휠로 밀리면 표가 아래로 내려가 위가 비는
        # 현상(사용자 2026-09-11 캡처) 방지.
        canvas.configure(scrollregion=canvas.bbox("all"))
        if grid.winfo_reqheight() <= canvas.winfo_height():
            canvas.yview_moveto(0)

    grid.bind("<Configure>", _fit_scroll)

    def _on_canvas_resize(e: Any) -> None:
        # 넓으면 캔버스 폭까지 채워 셀이 늘어나고(weight), 좁으면 자연폭 유지 → 가로 스크롤.
        # 필터 줄은 표 폭 + 스크롤바 칸(18)으로 같이 맞춰 콤보가 컬럼 위에 머문다.
        gw = max(e.width, grid.winfo_reqwidth())
        canvas.itemconfigure(_gwin, width=gw)
        hbar.itemconfigure(_hwin, width=gw)  # 헤더는 표와 같은 폭(세로 스크롤바 자리는 빈 채)
        fbar.itemconfigure(_fwin, width=gw + 18)
        _fit_scroll()  # 창을 키워 내용이 다 보이게 되면 위로 붙임

    canvas.bind("<Configure>", _on_canvas_resize)
    def _wheel(e: Any) -> None:
        lo, hi = canvas.yview()
        if lo <= 0.0 and hi >= 1.0:
            return  # 내용이 다 보이면 스크롤할 게 없음(표가 밀려 내려가지 않게)
        canvas.yview_scroll(int(-e.delta / 120), "units")

    canvas.bind_all("<MouseWheel>", _wheel)
    for c, (_title, w, _a) in enumerate(_COLS):
        # weight → 창을 넓히면 컬럼(셀)도 폭에 비례해 늘어남. minsize는 최소폭(헤더와 동일).
        grid.columnconfigure(c, minsize=w, weight=w)

    cells: list[list[tk.Label]] = []          # 재사용 행 풀 — cells[r] = 컬럼 수만큼 Label
    row_iid: list[str] = []                    # 각 행의 현재 iid(주문번호/__체결·__취소)
    row_vals: dict[str, tuple[Any, ...]] = {}  # iid → 값 튜플(정정/선택 편의용)
    sel: dict[str, str | None] = {"iid": None}

    def _highlight() -> None:
        nvis = int(state_box.get("_nvis") or 0)
        for r in range(nvis):
            bg = _SEL_BG if sel["iid"] and row_iid[r] == sel["iid"] else _NORM_BG
            for lbl in cells[r]:
                lbl.configure(bg=bg)

    def _select_row(r: int) -> None:
        if r < len(row_iid) and row_iid[r]:
            sel["iid"] = row_iid[r]
            _highlight()

    def _clicker(i: int) -> Callable[[Any], None]:
        return lambda _e: _select_row(i)  # 행 index 고정 — 재사용 행이라 클릭 시 현재 iid

    def _ensure_rows(n: int) -> None:
        while len(cells) < n:
            rr = len(cells)
            labels: list[tk.Label] = []
            for c, (_t, _w, a) in enumerate(_COLS):
                lbl = tk.Label(grid, font=T.FONT_LABEL, bg=_NORM_BG,
                               anchor=cast(Any, a), padx=3)
                lbl.grid(row=rr, column=c, sticky="nsew",  # 헤더는 표 밖(hbar)에 있음
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

    # 정정(정정가 입력·선택 정정)은 화면에서 뺐다(사용자 2026-09-11: 쓸 일이 거의 없음). 코어 명령
    # manual_amend는 그대로 있어 필요하면 다시 붙일 수 있다. 취소 버튼만 필터 줄 오른쪽 끝에.
    tk.Button(filt, text="선택 취소", command=do_cancel).grid(
        row=0, column=len(_COLS) - 2, columnspan=2, sticky="e", padx=(0, 2))

    # --- 상태바 (row 2, 맨 아래) ---
    status = tk.Label(root, text="-", anchor="w", relief="groove", width=1)
    status.grid(row=3, column=0, sticky="ew", padx=6, pady=(2, 6))

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

    stale_box: dict[str, Any] = {"on": False, "title": None}

    def _update_staleness() -> None:
        # 조회가 밀리면 창 제목에 표시 + 상태줄에 시작/복구 1회 알림(마지막 데이터는 유지).
        # 필터로 숨긴 건수도 제목에 — 안 보이는 주문이 "없는 것"이 아님을 알 수 있게.
        age = stale_seconds(state_box, time.time())
        fails = int(state_box.get("fails", 0) or 0)
        stale = (age is not None and age > 3.0) or (age is None and fails > 0)
        if stale and not stale_box["on"]:
            stale_box["on"] = True
            set_status("코어 조회 실패 — 마지막 데이터로 표시 중", err=True)
        elif not stale and stale_box["on"]:
            stale_box["on"] = False
            set_status("코어 조회 복구")
        title = window_title(age, fails, int(state_box.get("_hidden") or 0))
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
        flt = _filters()
        total = 0  # 유형 체크가 켜진 행 전부 — 콤보로 걸러진 수 = total - len(out)

        def keep(row: dict[str, Any]) -> bool:
            nonlocal total
            total += 1
            return row_visible(flt, _venue(str(row.get("instrument"))),
                               str(row.get("underlying")), str(row.get("side")),
                               str(row.get("source") or ""))

        if show_orders.get():
            # 미체결은 코어가 접수시각 내림차순(**새 주문이 위**)으로 보낸다 — 체결·취소와 같이
            # 최신 우선(사용자 2026-09-11). 화면에서 뒤집으면 시동 조회분(거래소 응답 순서)이
            # 거꾸로 보인다.
            for o in data.get("open_orders") or []:
                if not keep(o):
                    continue
                buy = o.get("side") == "buy"
                inst = str(o.get("instrument"))
                stk = _ST_KR.get(str(o.get("status")), o.get("status"))
                out.append((str(o.get("order_id")), "buy" if buy else "sell",
                            (_venue(inst), _sym(o.get("underlying"), inst),
                             "매수" if buy else "매도",
                             _fmt_px(o.get("price")), _fmt_qty(o.get("qty")),
                             "", "",  # 체결가·체결량 공백(미체결)
                             stk, o.get("time", ""), "",  # 접수시각·체결시각(공백)
                             str(o.get("order_id")), _src_label(o.get("source")))))
        if show_fills.get():
            for i, f in enumerate(data.get("fills") or []):
                if not keep(f):
                    continue
                buy = f.get("side") == "buy"
                inst = str(f.get("instrument"))
                out.append((f"__fill{i}", "buy" if buy else "sell",
                            (_venue(inst), _sym(f.get("underlying"), inst),
                             "매수" if buy else "매도",
                             _fmt_px(f.get("order_price")),  # 원주문 주문가
                             _fmt_qty(f.get("order_qty")),   # 원주문 수량
                             _fmt_px(f.get("price")), _fmt_qty(f.get("qty")),
                             "체결", f.get("accept_time", ""), f.get("time", ""),
                             str(f.get("order_id", "")), _src_label(f.get("source")))))
        if show_cancels.get():
            for i, c in enumerate(data.get("cancels") or []):
                if not keep(c):
                    continue
                buy = c.get("side") == "buy"
                inst = str(c.get("instrument"))
                out.append((f"__cancel{i}", "buy" if buy else "sell",
                            (_venue(inst), _sym(c.get("underlying"), inst),
                             "매수" if buy else "매도",
                             _fmt_px(c.get("price")), _fmt_qty(c.get("qty")),
                             "", "",  # 체결가·체결량 공백(취소행)
                             "취소", c.get("accept_time", ""), "",  # 접수시각·체결시각(공백)
                             str(c.get("order_id", "")), _src_label(c.get("source")))))
        # 유형 체크를 끈 종류도 "숨김"에 넣는다 — 주문 체크를 끄고 잊으면 미체결이 안 보인다.
        unchecked = ((0 if show_orders.get() else len(data.get("open_orders") or []))
                     + (0 if show_fills.get() else len(data.get("fills") or []))
                     + (0 if show_cancels.get() else len(data.get("cancels") or [])))
        state_box["_hidden"] = (total - len(out)) + unchecked
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
        # 행이 하나도 없으면 tk 프레임이 마지막 크기를 유지해 회색 덩어리로 남는다(필터로 전부
        # 걸러진 실측 2026-09-11) → 캔버스 창 높이를 1로. 행이 있으면 0(=자연 높이)으로 되돌린다.
        canvas.itemconfigure(_gwin, height=1 if not rows else 0)
        _fit_scroll()
        state_box["_nvis"] = len(rows)
        if sel["iid"] not in row_vals:  # 선택 행이 사라졌으면 해제
            sel["iid"] = None
        _highlight()

    def _rerender() -> None:
        state_box["_sig"] = None  # 필터 바뀜 → 강제 재그림
        _render()

    # 최소 크기 — 세로만. 가로는 필터 줄도 표와 함께 가로 스크롤 캔버스 안이라 얼마든지 좁힐 수
    # 있다(컬럼이 가려져도 됨, 사용자 2026-09-11).
    root.update_idletasks()
    root.minsize(240, 200)

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
