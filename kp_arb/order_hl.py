"""HL 일반 주문창 (수동) — 코어 클라이언트 (DESIGN-manual-order.md §6.3).

Hyperliquid perp 전용 수동 주문창(LS는 별도 화면 `order_ls`). 델파이 원본 레이아웃 —
좌(입력+잔고) / 우(호가창) 2분할. 지정가만. 화면은 명령·표시만, 판단·주문은 코어.
**화면 스레드 네트워크 금지** — 전송·폴링은 뒷단 스레드 + 큐, 화면은 결과만 after()로 읽는다.
코어 명령(manual_order/amend/cancel/hl_merge/refresh)·스냅샷(/manual_state)은 LS 창과 공용.
"""
from __future__ import annotations

import queue
from typing import Any

from . import win_state
from .core_client import (
    core_request,
    core_request_err,
    merge_poll,
    screen_log,
    stale_seconds,
    watch_parent_exit,
)
from .order_autot import UNDER_MAP, is_decimal_text

INSTRUMENT = "hl_perp"  # 이 창은 HL perp 전용
UNDERLYINGS = ("삼성", "하이닉스", "현대차")
# 호가단위(틱) 옵션은 **코어가 계산**해 manual_snapshot의 sym["merge_ticks"]로 준다(§5.10)
# — 화면은 '적' 전에도 그 목록으로 콤보를 채운다(라이브 가격 의존 제거).


def _fmt(v: Any, digits: int = 0) -> str:
    """수량·금액 표시 — None은 '-', 천단위 콤마."""
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_qty(v: Any) -> str:
    """수량 표시 — HL은 소수(0.179), 정수면 정수. 소수부 있으면 표시(끝 0 제거)."""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f):
        return f"{f:,.0f}"
    return f"{f:,.3f}".rstrip("0").rstrip(".")


def _fmt_px(v: Any, decimals: int | None = None) -> str:
    """가격 표시 — decimals 주면 그 자리수로 통일(호가 정렬용), 없으면 소수부 유무로 자동."""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if decimals is not None:
        return f"{f:,.{decimals}f}"
    if f == int(f):
        return f"{f:,.0f}"
    return f"{f:,.4f}".rstrip("0").rstrip(".")


def _sign_color(v: Any) -> str:
    """부호별 글자색 — 양수(매수/이익)=빨강, 음수(매도/손실)=파랑, 0·None=검정."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "black"
    if f > 0:
        return "#c00000"
    if f < 0:
        return "#0000c0"
    return "black"


def _hl_decimals(price: Any) -> int:
    """HL 가격 소수 자리수 — 유효숫자 5자리 규칙(정수부 자리수 기준). 종목별 사실상 고정
    이라 호가가 바뀌어도 소수점이 흔들리지 않는다. price None/이상은 2로."""
    try:
        p = abs(float(price))
    except (TypeError, ValueError):
        return 2
    digits = len(str(int(p))) if p >= 1 else 1
    return max(0, 5 - digits)


def _hoga_signature(rows: list[tuple[Any, ...]]) -> tuple[Any, ...]:
    """호가 다시그리기 판단용 — (구분, 가격, 건수, 잔량)."""
    return tuple((tag, price, cnt, qty) for tag, price, cnt, qty in rows)


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """HL 일반 주문창 실행."""
    import threading
    import time
    import tkinter as tk
    from tkinter import ttk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료 (고아 방지)
    root = tk.Tk()
    root.title("HL 일반주문")
    root.resizable(False, False)
    win_state.attach(root, "order_hl")
    # 기본 폰트 11(사용자 확정 — 폰트는 유지, 컴팩트는 폭·여백으로). rowheight는 아래에서 재설정.
    root.option_add("*Font", ("Malgun Gothic", 11))
    ttk.Style().configure("Treeview", font=("Malgun Gothic", 11), rowheight=22)
    vcmd_dec = (root.register(is_decimal_text), "%P")  # HL은 수량·가격 모두 소수 허용

    # --- 명령 전송: 큐 → 전송 스레드 → 결과 큐 → 화면 루프 ---
    jobs: queue.Queue[tuple[dict[str, Any], str, str | None]] = queue.Queue()
    results: queue.Queue[tuple[str, dict[str, Any] | None, str | None]] = queue.Queue()

    def sender() -> None:
        while True:
            payload, label, detail = jobs.get()
            # 주문/정정/취소는 HL REST 왕복이라 느릴 수 있다 — 타임아웃을 넉넉히(10s).
            # 짧으면 성공해도 '코어 미접속'으로 오인해 중복주문 위험.
            results.put((label, core_request("/command", payload, timeout=10.0), detail))

    threading.Thread(target=sender, daemon=True).start()

    def send(payload: dict[str, Any], label: str, detail: str | None = None) -> None:
        # detail: 주문이면 "매수 167.5 10" 식 — 결과 로그에 성공/거부와 함께 표시.
        jobs.put((payload, label, detail))

    # --- 상태 폴링: /manual_state → state_box (화면은 읽기만) ---
    state_box: dict[str, Any] = {"data": None}

    def poller() -> None:
        while True:
            # 실패해도 마지막 데이터 유지(merge_poll) — 잔고·호가가 통째로 비지 않게.
            data, err = core_request_err("/manual_state", timeout=2.0)
            msg = merge_poll(state_box, data, err, time.time())
            if msg is not None:
                screen_log().warning("일반주문 %s", msg)
            time.sleep(0.5)

    threading.Thread(target=poller, daemon=True).start()

    # ===== 좌(입력) | 중(오더북) | 우(잔고 10줄) 3분할 =====
    _BOLD = ("Malgun Gothic", 11, "bold")   # 숫자·입력은 볼드
    top = tk.Frame(root)
    top.pack(side="top", fill="both", padx=4, pady=4)
    left = tk.Frame(top)
    left.pack(side="left", anchor="n")
    mid = tk.Frame(top)      # 오더북
    mid.pack(side="left", anchor="n", padx=(4, 0))
    rbal = tk.Frame(top)     # 잔고 10줄
    rbal.pack(side="left", anchor="n", padx=(4, 0))

    def _row() -> tk.Frame:
        f = tk.Frame(left)
        f.pack(fill="x", pady=1)
        return f

    # 상단 — 종목/적 + 호가단위·Unified(오른쪽 끝=주문버튼 Right) + Cross(왼쪽 아래)
    head = tk.Frame(left)
    head.pack(fill="x", pady=1)
    head.grid_columnconfigure(2, weight=1)  # col2 확장 → 호가단위·Unified를 우측 끝으로
    cb_under = ttk.Combobox(head, values=UNDERLYINGS, width=7, state="readonly")
    cb_under.set("삼성")
    cb_under.grid(row=0, column=0, sticky="w")
    btn_apply = ttk.Button(head, text="적", width=3)
    btn_apply.grid(row=0, column=1, padx=(2, 0), sticky="ns")
    cb_merge = ttk.Combobox(head, values=[], width=4, state="readonly",  # 호가단위(틱) 축소
                            font=("Malgun Gothic", 9))
    cb_merge.grid(row=0, column=2, sticky="e")  # 오른쪽 끝 = 주문버튼 Right
    merge_map: dict[str, tuple[int | None, int | None]] = {}
    # 종목별 마지막 호가단위(틱) — 화면 저장·복원용. 콤보는 '적' 후에야 채워지므로 값을
    # 여기 보관했다가 _populate_merge에서 그 종목 것만 되살린다(종목 바꿔도 각자 유지).
    merge_by_sym: dict[str, str] = {}
    _small = ("Malgun Gothic", 8)  # Cross·Unified 폰트 1 확대
    btn_lev = tk.Button(head, text="Cross  5x", font=_small, padx=1, pady=1, bd=1)  # 높이 2↑
    btn_lev.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 0))
    lbl_mmode = tk.Label(head, text="Unified", fg="gray30", font=_small)
    lbl_mmode.grid(row=1, column=2, sticky="e", pady=(1, 0), ipady=1)  # 높이 2↑, 우측 끝

    # 매수/매도 — 한 줄(가로). 목업 원래 배치(잔고 칸 높이에 맞추려 각자 줄로 복원).
    srow = _row()
    side_var = tk.StringVar(value="buy")
    tk.Radiobutton(srow, text="매수", variable=side_var, value="buy",
                   fg="#c00000").pack(side="left")
    tk.Radiobutton(srow, text="매도", variable=side_var, value="sell",
                   fg="#0000c0").pack(side="left", padx=(16, 0))

    # 수량 — 자체 줄
    qrow = _row()
    tk.Label(qrow, text="수량", width=3, anchor="w").pack(side="left")
    e_qty = tk.Entry(qrow, width=10, justify="right", validate="key",
                     validatecommand=vcmd_dec, font=_BOLD)
    e_qty.pack(side="left", padx=(2, 0))

    # 단가(+틱 스핀) — 자체 줄. grid로 틱 자리를 항상 예약 → 호가 모드에서 폭 안 늘어남.
    prow = _row()
    tk.Label(prow, text="단가", width=3, anchor="w").grid(row=0, column=0, sticky="w")
    e_price = tk.Entry(prow, width=10, justify="right", validate="key",
                       validatecommand=vcmd_dec, font=_BOLD)
    e_price.grid(row=0, column=1, sticky="w", padx=(2, 0))
    tick_var = tk.IntVar(value=0)
    sp_tick = tk.Spinbox(prow, from_=-20, to=20, width=3, textvariable=tick_var,
                         justify="right", font=_BOLD)  # 호가 모드에서만 보임
    sp_tick.grid(row=0, column=2, padx=(3, 0))
    # 틱 자리 예약폭은 아래 update_idletasks 후 스핀 실제 폭으로 확정(숨겨도 폭 완전 고정).

    # 호가/가격 모드 (default 가격) — 목업 순서(호가·가격)
    mrow = _row()
    mode_var = tk.StringVar(value="price")  # "hoga" | "price"
    tk.Radiobutton(mrow, text="호가", variable=mode_var, value="hoga").pack(side="left")
    tk.Radiobutton(mrow, text="가격", variable=mode_var, value="price").pack(
        side="left", padx=(12, 0))

    def _toggle_tick(*_: Any) -> None:
        # 가격 모드: 틱 스핀 숨김(자리는 grid minsize로 유지 → 폭 불변) / 호가 모드: 보임.
        if mode_var.get() == "hoga":
            sp_tick.grid()
        else:
            sp_tick.grid_remove()

    mode_var.trace_add("write", _toggle_tick)
    _toggle_tick()

    # 체크박스(세로) + 매수(크게) 버튼 — 둘 다 arow 바닥에 정렬(anchor="s")
    arow = _row()
    checks = tk.Frame(arow)
    checks.pack(side="left", anchor="s")
    reduce_var = tk.BooleanVar(value=False)
    post_var = tk.BooleanVar(value=False)
    oneclick_var = tk.BooleanVar(value=True)  # 안전 잠금 — 체크돼야만 발송(기본 체크)
    tk.Checkbutton(checks, text="Rdce", variable=reduce_var).pack(anchor="w")
    tk.Checkbutton(checks, text="Post", variable=post_var).pack(anchor="w")
    tk.Checkbutton(checks, text="주문", variable=oneclick_var).pack(anchor="w")
    btn_order = tk.Button(arow, text="매수주문", width=9,
                          fg="white", bg="#c00000", activeforeground="white",
                          activebackground="#a00000", font=("Malgun Gothic", 14, "bold"))
    btn_order.pack(side="left", anchor="s", padx=(8, 0), ipady=8)

    # 중: 오더북 — 숫자 볼드·1축소, 잔량 폭 3자리 확대. (격자선은 렌더 확인 후)
    ttk.Style().configure("Treeview", font=("Malgun Gothic", 10, "bold"), rowheight=22)
    hoga = ttk.Treeview(mid, columns=("price", "cnt", "qty"), show="",
                        height=10, selectmode="browse")
    hoga.column("price", width=73, anchor="center")  # 호가 가운데
    hoga.column("cnt", width=30, anchor="w")          # 미체결 건수 "(n)" — 좌측 정렬
    hoga.column("qty", width=85, anchor="e")          # 잔량 — 우측 정렬(그대로)
    hoga.tag_configure("ask", background="#eef2ff", foreground="#0000c0")
    hoga.tag_configure("bid", background="#fff0f2", foreground="#c00000")
    hoga.tag_configure("cur", background="#fff6b0")
    hoga.pack()

    # 우: 잔고 10줄 — 잔고·PNL은 흰 박스(중요), 나머지는 그냥. 라벨 2축소, 값 볼드.
    bal_val: dict[str, tk.Label] = {}
    _BAL_ORDER = ("잔고", "PNL", "진입가", "평가액($)", "Liq_Prc",
                  "Margin", "Funding", "Oracle", "FundRate", "CountDown")
    # 디바운스 조회(clearinghouse) 항목 — 체결 즉시 계산이 안 돼 500ms 조회로 갱신. 라벨을
    # 회색으로 구분(나머지는 체결 이벤트·시세로 빠르게 갱신).
    _QUERY_FIELDS = {"Liq_Prc", "Margin", "Funding"}
    _tfont = ("Malgun Gothic", 9)
    _BAL_HL_BG = "#ffe680"  # 잔고·PNL 강조 바탕(노란색) — 색글자(빨강/파랑/검정)와 대비
    for i, name in enumerate(_BAL_ORDER):
        important = name in ("잔고", "PNL")
        if important:
            cell = tk.Frame(rbal, bg=_BAL_HL_BG, highlightbackground="gray50",
                            highlightthickness=1)
            cbg = _BAL_HL_BG
        else:
            cell = tk.Frame(rbal)
            cbg = cell.cget("bg")
        cell.grid(row=i, column=0, sticky="we", pady=1, padx=1)
        lbl_fg = "gray45" if name in _QUERY_FIELDS else "black"  # 조회 항목 회색 구분
        tk.Label(cell, text=name, width=9, anchor="w", font=_tfont, bg=cbg,
                 fg=lbl_fg).pack(side="left", padx=(3, 2))   # width 9 — CountDown 안 잘리게
        v = tk.Label(cell, text="-", width=9, anchor="e", font=_BOLD, bg=cbg)
        v.pack(side="right", padx=(0, 3))
        bal_val[name] = v

    # 상태바 — 회색·2축소·자동 확장(길어짐)
    status = tk.Label(root, text="-", anchor="w", relief="groove", width=1,
                      font=_tfont, fg="gray30")
    status.pack(side="top", fill="x", padx=4, pady=(0, 4))

    def set_status(text: str, err: bool = False, *, ok: bool = False) -> None:
        # 거부·실패=빨강 / 성공=검정 / 일반 안내=회색
        if err:
            fg = "#8b0000"
        elif ok:
            fg = "black"
        else:
            fg = "gray30"
        status.config(text=text[:120], fg=fg)

    # ===== 동작 =====
    def sym_key() -> str:
        return f"{UNDER_MAP[cb_under.get()]}|{INSTRUMENT}"

    # '적'으로 활성화한 종목만 하단 표시·주문 (콤보만 바꾼다고 안 바뀜 — 델파이 SetSymbol)
    active: dict[str, Any] = {"key": None, "underlying": None, "name": None}
    # 종목별 마지막으로 아는 레버리지(clearinghouse 값 또는 팝업에서 적용한 값) — 포지션
    # 없으면 조회로 못 읽어서, 팝업 재오픈 시 이 값으로 초기화(§D 낙관적).
    lev_applied: dict[str, dict[str, Any]] = {}

    def active_symbol() -> dict[str, Any]:
        if active["key"] is None:
            return {}
        data = state_box["data"] or {}
        return ((data.get("symbols") or {}).get(active["key"])) or {}

    def do_apply() -> None:
        active.update(key=sym_key(), underlying=UNDER_MAP[cb_under.get()],
                      name=cb_under.get())
        send({"cmd": "manual_refresh"}, "적용·조회")  # 잔고/포지션 재조회(OrderBook 재동기)
        refresh_side()
        _refresh_merge_combo()          # 활성 종목 호가단위 콤보 보장(이미 채워졌으면 유지)
        if cb_merge.get():
            on_merge(None)              # 활성 종목 오더북을 현재(저장된) 호가단위로 집계
        set_status(f"{cb_under.get()} 적용 — 조회 중")

    _merge_shown: dict[str, str | None] = {"under": None}  # 콤보에 채워진 종목(재populate 판단)
    _merge_pending: dict[str, str | None] = {"label": None, "under": None}  # 내가 방금 바꾼 머지

    def _selected_symbol() -> dict[str, Any]:
        # 선택(콤보) 종목의 스냅샷 — '적' 전에도 읽을 수 있게 active가 아닌 cb_under 기준.
        under = UNDER_MAP.get(cb_under.get())
        data = state_box["data"] or {}
        return ((data.get("symbols") or {}).get(f"{under}|{INSTRUMENT}")) or {}

    def _core_merge_label() -> str | None:
        """코어가 적용 중인 머지(merge_active)에 맞는 콤보 라벨 — 단일 진실=코어. merge_map 필요."""
        ma = _selected_symbol().get("merge_active")
        if not ma:
            return None
        key = (ma.get("n_sig_figs"), ma.get("mantissa"))
        return next((s for s, v in merge_map.items() if v == key), None)

    def _refresh_merge_combo() -> None:
        # 코어가 준 sym["merge_ticks"]로 콤보를 채운다('적' 전에도). 종목이 바뀌거나 처음
        # 채울 때만 set — 사용자가 고른 값은 유지(매 틱 덮어쓰기 방지).
        under = UNDER_MAP.get(cb_under.get())
        if under is None:
            return
        if _merge_shown["under"] == under and cb_merge["values"]:
            return  # 이미 이 종목으로 채워짐 — 유지
        ticks = _selected_symbol().get("merge_ticks") or []
        if not ticks:
            return  # 아직 가격 미수신 — 다음 폴링에 재시도
        merge_map.clear()
        vals: list[str] = []
        for t in ticks:
            s = str(t.get("tick"))
            vals.append(s)
            merge_map[s] = (t.get("n_sig_figs"), t.get("mantissa"))
        cb_merge["values"] = vals
        # 우선순위: 코어 현재 적용값(merge_active) > 창 저장값(win_state) > 최소 틱.
        # → 새 창은 코어 적용값을 그대로 보여준다(단일 진실).
        want = merge_by_sym.get(under)  # 이 종목의 저장값
        chosen = _core_merge_label() or (want if want in vals else vals[0])
        cb_merge.set(chosen)
        merge_by_sym[under] = chosen    # 표시값과 일치(자동저장이 이 dict을 씀)
        _merge_shown["under"] = under

    def _sync_merge_from_core() -> None:
        """다른 창이 바꾼 머지를 콤보에 반영(호가창은 이미 코어가 머지해 보냄 → 라벨만).
        내가 방금 바꾼 건 코어 확인 전엔 유지 — 플리커/경합 방지."""
        under = active["underlying"] or UNDER_MAP.get(cb_under.get())
        if under is None or _merge_shown["under"] != under or not merge_map:
            return
        core_label = _core_merge_label()
        if core_label is None:
            return
        if _merge_pending["label"] is not None and _merge_pending["under"] == under:
            if core_label == _merge_pending["label"]:
                _merge_pending["label"] = None  # 코어가 내 변경 확인 → 해제
            return  # 확인 전엔 콤보 유지
        if core_label != cb_merge.get():  # 다른 창이 바꿈 → 라벨 동기화
            cb_merge.set(core_label)
            merge_by_sym[under] = core_label

    def do_order() -> None:
        if not oneclick_var.get():  # 안전 잠금 — '주문' 체크돼야만 발송(사용자 확정)
            set_status("'주문' 미체크 — 안 나감", err=True)
            return
        if active["underlying"] is None:
            set_status("먼저 '적'으로 종목을 적용하세요", err=True)
            return
        try:
            qty = float(e_qty.get().strip())  # HL은 소수 수량 허용
        except ValueError:
            qty = 0.0
        if qty <= 0:
            set_status("수량을 입력하세요", err=True)
            return
        price = e_price.get().strip()
        if not price:
            set_status("지정가는 단가를 입력하세요", err=True)
            return
        side_kr = "매수" if side_var.get() == "buy" else "매도"
        send({
            "cmd": "manual_order", "instrument": INSTRUMENT,
            "underlying": active["underlying"], "side": side_var.get(),
            "order_type": "limit", "qty": qty, "price": float(price),
            "reduce_only": reduce_var.get(), "post_only": post_var.get(),
        }, "주문", f"{side_kr} {price} {qty:g}")

    # 델파이 원본(UNormalOrderEx.SetPrice) 기능: 호가모드는 **클릭한 호가 레벨(측+번호)**을
    # 기억하고, 그 레벨의 **현재가** + ±(틱 스핀 × 호가단위)를 단가에 넣는다(매수 +, 매도 −).
    # 시세가 갱신되면 그 레벨의 현재가가 바뀌므로 단가도 매 갱신 재계산된다(레벨 추적).
    _hoga_lvl: dict[str, Any] = {"side": None, "idx": None}  # 추적 레벨(없으면 side=None)

    def _hoga_unit() -> tuple[float, int]:
        # 오프셋 단위 = 선택 호가단위(틱). 없으면 종목 최소틱.
        ts = cb_merge.get().replace(",", "")
        try:
            return float(ts), (len(ts.split(".")[1]) if "." in ts else 0)
        except (ValueError, IndexError):
            dec = _hl_decimals(_ref_price(active_symbol()))
            return 10.0 ** (-dec), dec

    def _apply_hoga_offset() -> None:
        # 추적 중인 레벨의 **현재가**를 기준으로 ±(틱 스핀 × 호가단위) 적용. 레벨이 없거나
        # 사라졌으면 그대로 둔다. 매 시세 갱신(refresh)에서도 불려 단가가 레벨을 따라간다.
        side = _hoga_lvl["side"]
        idx = _hoga_lvl["idx"]
        if side is None or idx is None:
            return
        levels = list((active_symbol().get("asks" if side == "ask" else "bids")) or [])
        if idx >= len(levels):
            return
        base = float(levels[idx][0])
        tick, tdec = _hoga_unit()
        sign = 1 if side_var.get() == "buy" else -1  # 매수 +, 매도 − (델파이 SetPrice)
        price = base + sign * tick_var.get() * tick
        e_price.delete(0, "end")
        e_price.insert(0, _fmt_px(price, tdec).replace(",", ""))

    def on_hoga_click(_e: Any) -> None:
        sel = hoga.selection()
        if not sel:
            return
        vals = hoga.item(sel[0], "values")  # (가격, 건수, 잔량) — 가격은 0번
        if len(vals) < 1 or vals[0] in ("", "-"):
            return
        if mode_var.get() == "price":
            _hoga_lvl["side"] = None            # 가격모드: 추적 안 함 — 클릭가 그대로
            e_price.delete(0, "end")
            e_price.insert(0, str(vals[0]).replace(",", ""))
            return
        # 호가모드: 클릭 행이 매도(ask)/매수(bid) 몇 번째 레벨인지 기억(가격값 아님).
        # 표시는 위=역순 asks, 아래=bids 순 → 행번호로 측·index 환산.
        children = hoga.get_children()
        try:
            row = children.index(sel[0])
        except ValueError:
            return
        n_ask = int(state_box.get("_n_ask") or 0)
        if row < n_ask:
            _hoga_lvl.update(side="ask", idx=n_ask - 1 - row)  # 위쪽은 asks 역순
        else:
            _hoga_lvl.update(side="bid", idx=row - n_ask)
        _apply_hoga_offset()

    hoga.bind("<<TreeviewSelect>>", on_hoga_click)

    def _on_mode_change(*_: Any) -> None:
        # 가격→호가 전환 순간: 주문 방향의 1호가를 자동으로 추적 대상으로 잡는다
        # (매수→매수1호가 bids[0] / 매도→매도1호가 asks[0]). 이후는 그 레벨을 따라감.
        if mode_var.get() != "hoga":
            return
        _hoga_lvl.update(side="bid" if side_var.get() == "buy" else "ask", idx=0)
        _apply_hoga_offset()

    mode_var.trace_add("write", _on_mode_change)

    def _on_tick_spin() -> None:
        # 틱 스핀 업/다운 → 마지막 클릭 기준에 오프셋 재적용(호가 모드에서만).
        if mode_var.get() == "hoga":
            _apply_hoga_offset()

    sp_tick.config(command=_on_tick_spin)

    def _on_tick_change(*_: Any) -> None:
        # 직접 입력 포함 −20~20으로 강제 제한(화살표는 from_/to로, 타이핑은 여기서 클램프).
        try:
            v = tick_var.get()
        except tk.TclError:
            return  # 빈칸·'-' 등 입력 중 — 확정 시 다시 트리거됨
        c = max(-20, min(20, v))
        if c != v:
            tick_var.set(c)               # 이 set이 다시 트리거 → 아래 오프셋 재적용
            return
        if mode_var.get() == "hoga":
            _apply_hoga_offset()

    tick_var.trace_add("write", _on_tick_change)

    def on_merge(_e: Any) -> None:
        under = active["underlying"] or UNDER_MAP[cb_under.get()]
        merge_by_sym[under] = cb_merge.get()  # 이 종목의 선택 기억(화면 저장·재적용용)
        _merge_pending["label"] = cb_merge.get()  # 코어 확인 전까진 동기화가 덮지 않게
        _merge_pending["under"] = under
        nsf, mant = merge_map.get(cb_merge.get(), (None, None))
        send({"cmd": "manual_hl_merge", "underlying": under,
              "n_sig_figs": nsf, "mantissa": mant}, "머지")

    def open_leverage_popup() -> None:
        # 레버리지·마진모드 설정 팝업(§1-3) — 주문과 별개 액션. 성공 시 닫고, 실패 시 유지+사유.
        if active["underlying"] is None:
            set_status("먼저 '적'으로 종목을 적용하세요", err=True)
            return
        sym = active_symbol()
        # 현재값 — clearinghouse(포지션 있을 때) 우선, 없으면 마지막 적용값(lev_applied)
        _applied = lev_applied.get(active["underlying"], {})
        cur_lev = sym.get("leverage")
        cur_lev = cur_lev if cur_lev is not None else _applied.get("leverage")
        cur_cross = sym.get("leverage_cross")
        cur_cross = cur_cross if cur_cross is not None else _applied.get("cross")
        pop = tk.Toplevel(root)
        pop.title(f"{active['name']} 레버리지")
        pop.resizable(False, False)
        pop.transient(root)
        # 레버리지 버튼 바로 아래에 뜨게 — 버튼 화면 좌표 + 높이
        pop.geometry(
            f"+{btn_lev.winfo_rootx()}+{btn_lev.winfo_rooty() + btn_lev.winfo_height()}")
        mode_v = tk.StringVar(value="isolated" if cur_cross is False else "cross")
        tk.Radiobutton(pop, text="교차(Cross)", variable=mode_v, value="cross").grid(
            row=0, column=0, sticky="w", padx=6, pady=(6, 2))
        tk.Radiobutton(pop, text="격리(Isolated)", variable=mode_v, value="isolated").grid(
            row=0, column=1, sticky="w", padx=6, pady=(6, 2))
        tk.Label(pop, text="배수").grid(row=1, column=0, sticky="e", padx=6)
        lev_e = tk.Spinbox(pop, from_=1, to=10, width=5, justify="right")  # 1~10만
        lev_e.delete(0, "end")
        lev_e.insert(0, str(max(1, min(10, int(cur_lev) if cur_lev else 5))))
        lev_e.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        tk.Label(pop, text="(1~10)", fg="gray40").grid(row=1, column=2, padx=4)
        msg = tk.Label(pop, text="", fg="#8b0000")
        msg.grid(row=2, column=0, columnspan=3, padx=6, pady=(2, 0))

        def apply_lev() -> None:
            try:
                lev = int(lev_e.get().strip())
            except ValueError:
                msg.config(text="배수는 정수", fg="#8b0000")
                return
            if not 1 <= lev <= 10:  # 사용자 확정 — 1~10만
                msg.config(text="배수는 1~10", fg="#8b0000")
                return
            is_cross = mode_v.get() == "cross"
            msg.config(text="적용 중 ...", fg="gray30")

            def worker() -> None:
                result = core_request("/command", {  # 화면 스레드 아님(뒷단 스레드)
                    "cmd": "manual_leverage", "underlying": active["underlying"],
                    "leverage": lev, "is_cross": is_cross}, timeout=10.0)

                def done() -> None:
                    if result is None:
                        msg.config(text="코어 미접속", fg="#8b0000")
                    elif not result.get("ok"):
                        msg.config(text="; ".join(result.get("errors", [])), fg="#8b0000")
                    else:  # 성공 — 캡션 즉시 갱신(포지션 없으면 clearinghouse가 값을 안 줘서
                        # 폴링으론 못 바뀜. 방금 적용한 값으로 바로 반영).
                        mode = "Cross" if is_cross else "Isolated"
                        btn_lev.config(text=f"{mode}  {lev}x")
                        lev_applied[active["underlying"]] = {  # 재오픈 시 이 값으로 초기화
                            "leverage": lev, "cross": is_cross}
                        set_status(f"레버리지 {'교차' if is_cross else '격리'} {lev}x 적용됨")
                        pop.destroy()

                try:
                    pop.after(0, done)
                except tk.TclError:
                    pass  # 팝업 닫힘

            threading.Thread(target=worker, daemon=True).start()

        tk.Button(pop, text="적용", command=apply_lev, width=6).grid(
            row=3, column=0, padx=6, pady=6)
        tk.Button(pop, text="취소", command=pop.destroy, width=6).grid(
            row=3, column=1, padx=6, pady=6, sticky="w")

    cb_merge.bind("<<ComboboxSelected>>", on_merge)
    btn_order.config(command=do_order)
    btn_apply.config(command=do_apply)
    btn_lev.config(command=open_leverage_popup)

    def on_bal_click(_e: Any = None) -> None:
        # 잔고(포지션) 클릭 → 그 수량(절댓값)을 수량 에디트에 넣는다(청산 편의). 0/없으면 무시.
        pos = active_symbol().get("position")
        if pos is None:
            return
        try:
            q = abs(float(pos))
        except (TypeError, ValueError):
            return
        if q <= 0:
            return
        e_qty.delete(0, "end")
        e_qty.insert(0, _fmt_qty(q).replace(",", ""))

    bal_val["잔고"].config(cursor="hand2")  # 클릭 가능 표시
    bal_val["잔고"].bind("<Button-1>", on_bal_click)

    def refresh_side() -> None:
        buy = side_var.get() == "buy"
        # 2줄: 종목명 / 매수주문·매도주문. 배경 매수 빨강·매도 파랑, 흰 글씨.
        name = active["name"] or cb_under.get()  # '적' 전이면 콤보 선택값
        btn_order.config(text=f"{name}\n{'매수' if buy else '매도'}주문",
                         bg="#c00000" if buy else "#0000c0",
                         activebackground="#a00000" if buy else "#000090")

    def _on_side_change(*_: Any) -> None:
        refresh_side()
        if mode_var.get() == "hoga":
            _apply_hoga_offset()  # 매수/매도 부호(+/−) 반영해 호가 기준가 재적용
    side_var.trace_add("write", _on_side_change)
    cb_under.bind("<<ComboboxSelected>>", lambda *_: refresh_side())  # 버튼 종목명 즉시 갱신
    refresh_side()

    # ===== 화면 갱신 (네트워크 없음 — 폴링 결과만 읽어 그림) =====
    def _reschedule(fn: Any, ms: int) -> None:
        try:
            root.after(ms, fn)
        except tk.TclError:
            pass  # 창 닫힘

    def drain_results() -> None:
        try:
            while True:
                label, result, detail = results.get_nowait()
                if result is None:
                    set_status(f"{label} 실패 — 코어 미접속", err=True)
                elif not result.get("ok"):
                    reason = "; ".join(result.get("errors", []))
                    if detail:  # 주문 거부 : 매도 167.5 10 (거부사유)
                        set_status(f"주문 거부 : {detail} ({reason})", err=True)
                    else:
                        set_status(f"{label} 거부 — {reason}", err=True)
                else:
                    oid = result.get("order_id")
                    tail = f" (#{oid})" if oid else ""
                    # 하단 로그는 주문 관련만 — WS 무데이터 경고는 여기 표시 안 함(메인창
                    # WS표에서 확인). 발주 성공은 검정.
                    if detail:  # 주문 성공 : 매수 163.45 0.14 (#주문번호)
                        set_status(f"주문 성공 : {detail}" + tail, ok=True)
                    else:
                        set_status(f"{label} 접수됨" + tail, ok=True)
        except queue.Empty:
            pass
        _reschedule(drain_results, 200)

    def _ref_price(sym: dict[str, Any]) -> Any:
        if sym.get("last") is not None:
            return sym.get("last")
        asks = sym.get("asks") or []
        bids = sym.get("bids") or []
        return (asks[0][0] if asks else None) or (bids[0][0] if bids else None)

    _TITLE = "HL 일반주문"
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
            set_status("코어 조회 복구", ok=True)
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
            _refresh_merge_combo()  # 호가단위 콤보 채움('적' 전에도, 선택 종목 기준)
            _sync_merge_from_core()  # 다른 창이 바꾼 코어 머지를 콤보에 반영
            sym = active_symbol()
            dec = _hl_decimals(_ref_price(sym))
            # 잔고(=포지션)·PNL 우선, 나머지. 포맷: 진입가 3자리·PNL 1자리·마진/펀딩 0자리
            pos = sym.get("position")   # 잔고(=포지션 부호): 매수 빨강 / 매도 파랑
            bal_val["잔고"].config(text=_fmt_qty(pos), fg=_sign_color(pos))
            pnl = sym.get("pnl")        # PNL: 이익 빨강 / 손실 파랑 / 0 검정
            bal_val["PNL"].config(text=_fmt(pnl, 1), fg=_sign_color(pnl))
            bal_val["평가액($)"].config(text=_fmt(sym.get("eval")))
            bal_val["진입가"].config(text=_fmt_px(sym.get("avg_price"), 3))
            bal_val["Liq_Prc"].config(text=_fmt_px(sym.get("liq"), dec))
            bal_val["Margin"].config(text=_fmt(sym.get("margin"), 0))
            # Funding: HL cumFunding.sinceOpen은 '지불한 펀딩'(+ = 지불/비용)이라 사이트
            # "Funding"(손익 관점, 지불 = −)과 부호가 반대 → 뒤집어 맞춘다(= −cumFunding).
            # 실측·문서로 확인(clearinghouseState 소스는 맞음, 규약만 반대). 사이트처럼 2자리.
            cf = sym.get("cum_funding")
            bal_val["Funding"].config(text=_fmt(-cf if cf is not None else None, 2))
            bal_val["Oracle"].config(text=_fmt_px(sym.get("oracle"), dec))
            rate = sym.get("funding_rate")
            bal_val["FundRate"].config(
                text=f"{rate * 100:.4f}%" if rate is not None else "-")
            secs = 3600 - (time.localtime().tm_min * 60 + time.localtime().tm_sec)
            bal_val["CountDown"].config(text=f"{secs // 60:02d}:{secs % 60:02d}")
            # 레버리지 버튼 캡션 — 포지션 있으면 clearinghouse 값이 진실(§1-3). lev_applied도 갱신.
            lev = sym.get("leverage")
            if lev is not None:
                cross = bool(sym.get("leverage_cross"))
                btn_lev.config(text=f"{'Cross' if cross else 'Isolated'}  {int(lev)}x")
                if active["underlying"]:
                    lev_applied[active["underlying"]] = {"leverage": int(lev), "cross": cross}
            # 호가모드 단가는 추적 레벨의 현재가로 매 갱신 재계산(_fill_hoga 뒤 _apply_hoga_offset)
            # 내 미체결이 있는 호가에 "(건수)" 표시 — 활성 종목 미체결을 가격별 집계
            my_ords: dict[str, int] = {}
            for o in (state_box["data"] or {}).get("open_orders") or []:
                if (o.get("underlying") == active["underlying"]
                        and o.get("instrument") == INSTRUMENT):
                    ps = _fmt_px(o.get("price"), dec)
                    my_ords[ps] = my_ords.get(ps, 0) + 1
            _fill_hoga(sym, dec, my_ords)
            if mode_var.get() == "hoga":
                _apply_hoga_offset()  # 추적 중인 호가 레벨의 현재가로 단가 재계산(따라감)
        except Exception:  # noqa: BLE001 - 갱신 오류로 창이 죽지 않게 (버벅임 방지)
            pass
        _reschedule(refresh, 500)  # 호가창 갱신 주기 500ms (§1-4)

    def _fill_hoga(sym: dict[str, Any], dec: int, my_ords: dict[str, int]) -> None:
        asks = list(sym.get("asks") or [])[:5]  # 5호가 (§1-4)
        bids = list(sym.get("bids") or [])[:5]
        state_box["_n_ask"] = len(asks)  # 위쪽 ask 행 수 — 호가클릭 행→레벨 환산 기준
        last = sym.get("last")
        last_s = _fmt_px(last, dec) if last is not None else None

        def _cnt(price_s: str) -> str:  # 내 미체결 건수 "(n)" — 좌측 칼럼(없으면 빈칸)
            n = my_ords.get(price_s)
            return f"({n})" if n else ""

        # 매도(파랑) 위 → 매수(빨강) 아래. 현재가와 같은 호가만 노랑 바탕(별도 현재가 행 없음).
        draw: list[tuple[str, Any, str, str]] = []
        for p, q in reversed(asks):
            ps = _fmt_px(p, dec)
            draw.append(("cur" if ps == last_s else "ask", ps, _cnt(ps), _fmt_qty(q)))
        for p, q in bids:
            ps = _fmt_px(p, dec)
            draw.append(("cur" if ps == last_s else "bid", ps, _cnt(ps), _fmt_qty(q)))
        if _hoga_signature(draw) == state_box.get("_hsig"):
            return
        state_box["_hsig"] = _hoga_signature(draw)
        hoga.delete(*hoga.get_children())
        for tag, ps, cs, qs in draw:
            hoga.insert("", "end", values=(ps, cs, qs), tags=(tag,))

    # 오더북(10행) 높이를 좌측 입력열·잔고열 중 더 큰 쪽에 맞춘다 — 그 높이를 10등분해
    # 행높이로(호가 간격↑). 픽셀 추측 없이 정렬되고, 폰트·DPI가 달라도 따라간다. (사용자 요청)
    root.update_idletasks()
    _ref_h = max(left.winfo_reqheight(), rbal.winfo_reqheight())
    if _ref_h > 0:
        ttk.Style().configure("Treeview", rowheight=max(20, _ref_h // 10))
    # 틱 스핀 자리를 스핀 실제 폭(+패딩 3)으로 예약 → 호가/가격 전환에도 좌측 칸 폭 불변.
    _tick_w = sp_tick.winfo_reqwidth()
    if _tick_w > 0:
        prow.grid_columnconfigure(2, minsize=_tick_w + 3)
    # 주문버튼(arow) 바닥을 오더북 바닥에 맞춘다 — 오더북이 좌측열보다 크면 그 차이만큼
    # arow를 아래로 내린다(위 여백). 버튼·Rdce·Post·주문 Top이 함께 내려감. (사용자 요청)
    root.update_idletasks()
    _gap = hoga.winfo_reqheight() - left.winfo_reqheight()
    if _gap > 0:
        arow.pack_configure(pady=(_gap, 1))

    # --- 폼 필드 저장/복원 (종목·모드·체크박스 등, win_fields.json) ---
    _saved = win_state.saved_fields("order_hl")
    if _saved.get("under") in UNDERLYINGS:
        cb_under.set(_saved["under"])          # 종목(단, '적' 눌러야 활성 — 델파이 2단계)
    if _saved.get("side") in ("buy", "sell"):
        side_var.set(_saved["side"])           # trace → 버튼 색·캡션 갱신
    if _saved.get("mode") in ("hoga", "price"):
        mode_var.set(_saved["mode"])           # trace → 틱 스핀 표시 토글
    reduce_var.set(bool(_saved.get("reduce", False)))
    post_var.set(bool(_saved.get("post", False)))
    oneclick_var.set(bool(_saved.get("oneclick", True)))
    _mbs = _saved.get("merge_by_sym")  # 종목별 호가단위 — '적' 후 그 종목 것만 되살림
    if isinstance(_mbs, dict):
        merge_by_sym.update(
            {str(k): str(v) for k, v in _mbs.items() if isinstance(v, str) and v})
    try:
        tick_var.set(int(_saved.get("tick", 0)))
    except (ValueError, TypeError):
        pass

    def _persist_fields() -> None:
        try:
            win_state.save_fields("order_hl", {
                "under": cb_under.get(), "side": side_var.get(), "mode": mode_var.get(),
                "reduce": reduce_var.get(), "post": post_var.get(),
                "oneclick": oneclick_var.get(), "tick": tick_var.get(),
                # 종목별 호가단위 dict 저장 — 콤보가 '적' 전 비어도 빈값 덮어쓰기 없음.
                "merge_by_sym": dict(merge_by_sym)})  # 종목별 호가단위(틱)
            root.after(2000, _persist_fields)
        except tk.TclError:
            pass  # 창 닫힘

    root.after(2000, _persist_fields)

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
