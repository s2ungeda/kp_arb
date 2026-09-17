"""주문 리스트/미체결 관리 화면 — 코어 클라이언트 (DESIGN-manual-order.md §6.3).

전체 미체결 주문(HL·LS 공통)을 표로 보고 **취소·정정**한다. 코어 명령
manual_cancel/manual_amend 사용(주문창과 별도 화면). 화면 스레드는 네트워크 금지 —
전송·폴링은 뒷단 스레드 + 큐, 화면은 저장된 결과만 after()로 읽는다.

표는 **ttk.Treeview**(2026-09-16 전환, 사용자 확정) — 코어가 당일 체결·취소를 전부 보내게 되면서
Label 그리드(셀마다 위젯)는 500행에 다시 그리기 178ms가 걸려 체결마다 창이 멈칫했다. Treeview는
수천 행도 몇 ms. 대신 색은 셀이 아니라 **행 단위**(매수 빨강/매도 파랑, HL 체결 창과 같은 규칙).
스타일은 ui_theme 토큰(DESIGN-ui.md).
"""
from __future__ import annotations

import queue
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
                source: str, tag: str = "") -> bool:
    """필터 5개(거래소·종목·매매·출처·세트)를 행 하나에 적용. 출처 '일반주문' = 자동M이 아닌 전부
    (일반주문창·따라가기·미상). 세트(2026-09-16)는 꼬리표 앞부분 일치 — "선정3"을 고르면 선정3진·
    선정3청 둘 다."""
    if filters.get("venue", "전체") != "전체" and venue != filters["venue"]:
        return False
    want_tag = filters.get("set", "전체")
    if want_tag != "전체" and not tag.startswith(want_tag):
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


def set_choices(tags: list[str]) -> list[str]:
    """세트 콤보 항목 — 표에 있는 자동M 꼬리표에서 세트 부분만("선정3진" → "선정3") 모아 정렬,
    앞에 '전체'. 순수(2026-09-16, 형식 2026-09-17: 상품·방향·세트·진/청)."""
    seen: set[str] = set()
    for t in tags:
        base = t.strip()
        if base.endswith(("진", "청")):
            base = base[:-1]
        if base:
            seen.add(base)
    return ["전체", *sorted(seen)]


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
    ("체결시각", 66, "center"),
    # 출처(자동M/일반/따라가기) · 세트(자동M 꼬리표 "선정3진", 2026-09-17) · 주문번호 순(사용자)
    ("출처", 52, "center"), ("세트", 62, "center"), ("주문번호", 104, "e"))
_ROW_H = 20  # Treeview 행 높이(px) — FONT_LABEL 9pt 기준


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """주문 리스트 창 실행."""
    import threading
    import time
    import tkinter as tk
    from tkinter import ttk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료 (고아 방지)
    root = tk.Tk()
    from .core_client import log_screen_timing
    log_screen_timing(root, __name__)  # 시동 계측: 화면 시작·표시 시각(screen 로그)
    root.title(_TITLE)
    root.resizable(True, True)  # 크기 조절 — 표 행만 확장(root.rowconfigure weight)
    # 기본 크기 — 컬럼 전부 + 여유가 보이는 폭(사용자 2026-09-11). 최소폭 고정을 없애면서 자연
    # 크기가 아주 작아졌으므로 명시. 위치는 win_state가 복원, 크기는 매번 이 값.
    root.geometry("890x370")  # 세트 칸(62px) 추가만큼 넓힘(2026-09-16)
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

    # ===== 레이아웃 (grid: 필터[0] 고정, 표[1]만 세로 확장(헤더는 Treeview 자체), 상태바[2]) =====
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

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
    # 세트 콤보(2026-09-16) — 출처 옆. 항목은 지금 표에 있는 자동M 꼬리표("정3"·"역1")로 채움
    _combo("set", ("전체",), 8, span=2, sticky="w", width=6)

    # --- 표: ttk.Treeview (row 1, 확장) — 컬럼 헤더는 Treeview 자체(세로 스크롤에 안 밀림).
    # 2026-09-16 Label 그리드에서 전환: 코어가 당일 체결·취소를 전부 보내므로(수백 행) 셀마다
    # 위젯인 그리드는 새 체결마다 전 칸을 다시 써 0.2초씩 멈칫했다. 색은 행 단위(매수 빨강/매도
    # 파랑 — HL 체결 창과 같은 규칙), 선택은 Treeview 기본(파란 행).
    table = tk.Frame(root)
    table.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 2))
    style = ttk.Style()
    style.configure("OL.Treeview", font=T.FONT_LABEL, rowheight=_ROW_H)
    style.configure("OL.Treeview.Heading", font=T.FONT_LABEL)
    col_ids = [f"c{i}" for i in range(len(_COLS))]
    tree = ttk.Treeview(table, columns=col_ids, show="headings", style="OL.Treeview",
                        selectmode="browse")
    for cid, (title, w, a) in zip(col_ids, _COLS, strict=True):
        tree.heading(cid, text=title)
        # width=minwidth=최소폭(헤더와 동일), stretch → 창을 넓히면 컬럼도 늘어남
        tree.column(cid, width=w, minwidth=w, anchor=cast(Any, a), stretch=True)
    tree.tag_configure("buy", foreground=T.C_BUY)
    tree.tag_configure("sell", foreground=T.C_SELL)
    vsb = ttk.Scrollbar(table, orient="vertical", command=tree.yview)

    def _xview(*args: Any) -> None:  # 표·필터 줄을 같이 가로 스크롤
        tree.xview(*args)
        fbar.xview(*args)

    hsb = ttk.Scrollbar(table, orient="horizontal", command=_xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    vsb.pack(side="right", fill="y")
    hsb.pack(side="bottom", fill="x")
    tree.pack(side="left", fill="both", expand=True)

    def _on_tree_resize(e: Any) -> None:
        # 필터 줄은 표 폭 + 스크롤바 칸(18)으로 같이 맞춰 콤보가 컬럼 위에 머문다.
        fbar.itemconfigure(_fwin, width=max(e.width, sum(w for _t, w, _a in _COLS)) + 18)

    tree.bind("<Configure>", _on_tree_resize)

    row_vals: dict[str, tuple[Any, ...]] = {}  # iid → 값 튜플(선택 편의용)
    sel: dict[str, str | None] = {"iid": None}

    def _on_select(_e: object = None) -> None:
        chosen = tree.selection()
        sel["iid"] = chosen[0] if chosen else None

    tree.bind("<<TreeviewSelect>>", _on_select)

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
                               str(row.get("source") or ""), str(row.get("tag") or ""))

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
                             _src_label(o.get("source")), str(o.get("tag") or ""),
                             str(o.get("order_id")))))
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
                             _src_label(f.get("source")), str(f.get("tag") or ""),
                             str(f.get("order_id", "")))))
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
                             # 상태: 코어가 준 status(cancelled/rejected → 취소/거부), 없으면 취소
                             _ST_KR.get(str(c.get("status") or ""), "취소"),
                             c.get("accept_time", ""), "",  # 접수시각·체결시각(공백)
                             _src_label(c.get("source")), str(c.get("tag") or ""),
                             str(c.get("order_id", "")))))
        # 유형 체크를 끈 종류도 "숨김"에 넣는다 — 주문 체크를 끄고 잊으면 미체결이 안 보인다.
        unchecked = ((0 if show_orders.get() else len(data.get("open_orders") or []))
                     + (0 if show_fills.get() else len(data.get("fills") or []))
                     + (0 if show_cancels.get() else len(data.get("cancels") or [])))
        state_box["_hidden"] = (total - len(out)) + unchecked
        return out

    def _refresh_set_combo() -> None:
        # 세트 콤보 항목을 지금 데이터의 꼬리표로 — 바뀔 때만. 고른 값이 사라지면 '전체'로
        data = state_box["data"] or {}
        tags = [str(r.get("tag") or "") for key in ("open_orders", "fills", "cancels")
                for r in (data.get(key) or []) if isinstance(r, dict)]
        choices = set_choices(tags)
        cb = combos.get("set")
        if cb is None or list(cb["values"]) == choices:
            return
        cb.config(values=choices)
        if cb.get() not in choices:
            cb.set("전체")

    def _render() -> None:
        _refresh_set_combo()
        rows = _rows()
        sig = tuple((iid, *vals) for iid, _, vals in rows)
        if sig == state_box.get("_sig"):
            return  # 변화 없으면 다시 안 그림(선택·스크롤 유지)
        state_box["_sig"] = sig
        keep = sel["iid"]
        top = tree.yview()[0]  # 다시 채운 뒤 스크롤 위치 유지
        tree.delete(*tree.get_children(""))
        row_vals.clear()
        for iid, side, vals in rows:
            try:
                tree.insert("", "end", iid=iid, values=vals, tags=(side,))
            except tk.TclError:  # 같은 iid가 두 줄(주문번호 중복) — 자동 iid로
                iid = tree.insert("", "end", values=vals, tags=(side,))
            row_vals[iid] = vals
        state_box["_nvis"] = len(rows)
        if keep in row_vals:  # 선택 행이 남아 있으면 선택 유지, 사라졌으면 해제
            tree.selection_set(keep)
        else:
            sel["iid"] = None
        if rows:
            tree.yview_moveto(top)

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
