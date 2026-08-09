"""HL 일반 주문창 (수동) — 코어 클라이언트 (DESIGN-manual-order.md §6.3).

Hyperliquid perp 전용 수동 주문창(LS는 별도 화면 `order_ls`). 델파이 원본 레이아웃 —
좌(입력+잔고) / 우(호가창) 2분할. 지정가만. 화면은 명령·표시만, 판단·주문은 코어.
**화면 스레드 네트워크 금지** — 전송·폴링은 뒷단 스레드 + 큐, 화면은 결과만 after()로 읽는다.
코어 명령(manual_order/amend/cancel/hl_merge/refresh)·스냅샷(/manual_state)은 LS 창과 공용.
"""
from __future__ import annotations

import math
import queue
from typing import Any

from . import win_state
from .core_client import core_request, watch_parent_exit
from .order_panel import UNDER_MAP, is_decimal_text

INSTRUMENT = "hl_perp"  # 이 창은 HL perp 전용
UNDERLYINGS = ("삼성", "하이닉스", "현대차")
# HL 호가단위(aggregation) 단계 — (기준틱 배수, n_sig_figs, mantissa). HL은 유효숫자
# (nSigFigs) 기준으로 뭉치므로, 종목별 실제 틱 = 기준틱×배수로 표시한다(원시/2배 라벨 대신).
_MERGE_LEVELS: list[tuple[int, int | None, int | None]] = [
    (1, None, None), (2, 5, 2), (5, 5, 5), (10, 4, None), (100, 3, None), (1000, 2, None)]


def _merge_ticks(price: float) -> list[tuple[str, int | None, int | None]]:
    """활성 종목 가격 기준 호가단위 옵션 — [(틱표시, nSigFigs, mantissa), ...].
    기준틱 = 10^(floor(log10 가격) − 4)(유효숫자 5자리), 그 배수로 단계 구성."""
    if price <= 0:
        return []
    base = 10.0 ** (math.floor(math.log10(abs(price))) - 4)
    return [(_fmt_px(base * mult), nsf, mant) for mult, nsf, mant in _MERGE_LEVELS]


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
    """호가 다시그리기 판단용 — (구분, 가격, 잔량)."""
    return tuple((tag, price, qty) for tag, price, qty in rows)


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
            state_box["data"] = core_request("/manual_state", timeout=2.0)
            time.sleep(0.5)

    threading.Thread(target=poller, daemon=True).start()

    # ===== 좌(입력+잔고) / 우(호가창) 2분할 =====
    top = tk.Frame(root)
    top.pack(side="top", fill="both", padx=4, pady=4)
    left = tk.Frame(top)
    left.pack(side="left", anchor="n")
    right = tk.Frame(top)
    right.pack(side="left", anchor="n", padx=(3, 0))

    def _row() -> tk.Frame:
        f = tk.Frame(left)
        f.pack(fill="x", pady=1)
        return f

    # 상단 2줄 — grid로 열 정렬: 종목/적/호가단위 // 레버리지/Unified (HL 라벨 제거, 좌측 당김)
    head = tk.Frame(left)
    head.pack(fill="x", pady=1)
    cb_under = ttk.Combobox(head, values=UNDERLYINGS, width=7, state="readonly")
    cb_under.set("삼성")
    cb_under.grid(row=0, column=0, sticky="w")
    btn_apply = ttk.Button(head, text="적", width=3)
    btn_apply.grid(row=0, column=1, padx=(3, 0), sticky="ns")  # 밀착
    cb_merge = ttk.Combobox(head, values=[], width=6, state="readonly")  # 호가단위(틱)
    cb_merge.grid(row=0, column=2, sticky="w", padx=(3, 0))
    # 콤보 표시값(틱) → (nSigFigs, mantissa) 역매핑 — '적'에서 종목 가격으로 채운다.
    merge_map: dict[str, tuple[int | None, int | None]] = {}
    # 레버리지 버튼(종목~적 아래) + Unified(호가단위 아래) — 폰트로 ~40% 축소, 표시 전용(배선 D).
    _small = ("Malgun Gothic", 7)
    btn_lev = tk.Button(head, text="Cross  5x", font=_small, padx=1, pady=0, bd=1)
    btn_lev.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 0))  # 1px 아래로
    lbl_mmode = tk.Label(head, text="Unified", fg="gray30", font=_small)
    lbl_mmode.grid(row=1, column=2, sticky="w", padx=(3, 0), pady=(1, 0))  # 1px 아래로

    # 매수/매도
    r = _row()
    side_var = tk.StringVar(value="buy")
    tk.Radiobutton(r, text="매수", variable=side_var, value="buy",
                   fg="#c00000").pack(side="left")
    tk.Radiobutton(r, text="매도", variable=side_var, value="sell",
                   fg="#0000c0").pack(side="left", padx=(4, 0))

    # 수량
    r = _row()
    tk.Label(r, text="수량", width=3, anchor="w").pack(side="left")
    e_qty = tk.Entry(r, width=11, justify="right", validate="key",
                     validatecommand=vcmd_dec)  # HL 수량은 소수 허용
    e_qty.pack(side="left", padx=(2, 0))  # 라벨과 2px 간격
    # 단가 + 틱
    r = _row()
    tk.Label(r, text="단가", width=3, anchor="w").pack(side="left")
    e_price = tk.Entry(r, width=11, justify="right", validate="key",
                       validatecommand=vcmd_dec)
    e_price.pack(side="left", padx=(2, 0))
    tick_var = tk.IntVar(value=0)
    sp_tick = tk.Spinbox(r, from_=-20, to=20, width=3, textvariable=tick_var,
                         justify="right")  # 호가 모드에서만 보임 (_toggle_tick)
    sp_tick.pack(side="left", padx=(4, 0))

    # 호가/가격 모드 (default 가격)
    r = _row()
    mode_var = tk.StringVar(value="price")  # "hoga" | "price"
    tk.Radiobutton(r, text="호가", variable=mode_var, value="hoga").pack(side="left")
    tk.Radiobutton(r, text="가격", variable=mode_var, value="price").pack(
        side="left", padx=(4, 0))

    def _toggle_tick(*_: Any) -> None:
        # 가격 모드: 틱 스핀 숨김(클릭·직접입력이라 오프셋 불필요) / 호가 모드: 보임.
        if mode_var.get() == "hoga":
            sp_tick.pack(side="left", padx=(4, 0))
        else:
            sp_tick.pack_forget()

    mode_var.trace_add("write", _toggle_tick)
    _toggle_tick()  # 초기 = 가격 → 틱 숨김

    # 체크박스(세로) + 큰 주문 버튼(옆) — 화면안. 배경 매수 빨강·매도 파랑, 흰 굵은 글씨(§1).
    r = _row()
    checks = tk.Frame(r)
    checks.pack(side="left", anchor="n")
    reduce_var = tk.BooleanVar(value=False)
    post_var = tk.BooleanVar(value=False)
    oneclick_var = tk.BooleanVar(value=True)  # 안전 잠금 — 체크돼야만 발송(기본 체크)
    tk.Checkbutton(checks, text="Reduce", variable=reduce_var).pack(anchor="w")
    tk.Checkbutton(checks, text="Post", variable=post_var).pack(anchor="w")
    tk.Checkbutton(checks, text="원클릭", variable=oneclick_var).pack(anchor="w")
    btn_order = tk.Button(r, text="삼성\n매수 주문", width=8,
                          fg="white", bg="#c00000", activeforeground="white",
                          activebackground="#a00000", font=("Malgun Gothic", 13, "bold"))
    # 폭 축소(사용자 요청) — expand 제거해 늘어나지 않게, 높이는 내부 여백으로.
    btn_order.pack(side="left", padx=(8, 0), ipady=7)

    # 우: 호가창(헤더 없음 — 색으로 매도/매수 구분). 5호가(§1-4) = 10행(매도5+매수5).
    hoga = ttk.Treeview(right, columns=("price", "qty"), show="",
                        height=10, selectmode="browse")
    hoga.column("price", width=78, anchor="e")   # 폭 축소(사용자 요청)
    hoga.column("qty", width=82, anchor="e")
    hoga.tag_configure("ask", background="#e8eeff", foreground="#0000c0")
    hoga.tag_configure("bid", background="#ffeef0", foreground="#c00000")
    hoga.tag_configure("cur", background="#fff6b0")
    hoga.pack()

    # ===== 하단: 2열 잔고표 (§1, §1-1) — 왼쪽 포지션 / 오른쪽 마진·펀딩·오라클 =====
    bal = tk.Frame(root, relief="groove", bd=1)
    bal.pack(side="top", fill="x", padx=4, pady=(0, 2))
    bal_val: dict[str, tk.Label] = {}
    _BAL_LEFT = ("수량", "진입금액", "진입가", "PNL", "Liq_Prc")
    _BAL_RIGHT = ("Margin", "Funding", "Oracle", "FundRate", "CountDown")
    # 값 칼럼 폭을 좁혀 창 너비를 줄인다(잔고표가 폼 너비를 잡고 있어서 — 사용자 요청).
    for i, (lname, rname) in enumerate(zip(_BAL_LEFT, _BAL_RIGHT, strict=True)):
        tk.Label(bal, text=lname, width=7, anchor="w").grid(
            row=i, column=0, sticky="w", padx=(4, 2))
        lv = tk.Label(bal, text="-", width=9, anchor="e")
        lv.grid(row=i, column=1, sticky="e", padx=(0, 6))
        bal_val[lname] = lv
        tk.Label(bal, text=rname, width=9, anchor="w").grid(
            row=i, column=2, sticky="w", padx=(4, 2))
        rv = tk.Label(bal, text="-", width=9, anchor="e")
        rv.grid(row=i, column=3, sticky="e", padx=(0, 4))
        bal_val[rname] = rv
    # 상태바 — width=1(요청폭 최소) + fill=x: 긴 로그가 창 넓이를 밀지 않고 잘린다.
    status = tk.Label(root, text="-", anchor="w", relief="groove", width=1)
    status.pack(side="top", fill="x", padx=4, pady=(0, 4))

    def set_status(text: str, err: bool = False) -> None:
        status.config(text=text[:90], fg="#8b0000" if err else "black")  # 길면 잘라 표시

    # ===== 동작 =====
    def sym_key() -> str:
        return f"{UNDER_MAP[cb_under.get()]}|{INSTRUMENT}"

    # '적'으로 활성화한 종목만 하단 표시·주문 (콤보만 바꾼다고 안 바뀜 — 델파이 SetSymbol)
    active: dict[str, Any] = {"key": None, "underlying": None, "name": None}

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
        _populate_merge()  # 종목 가격 기준 호가단위(틱) 콤보 채우기
        set_status(f"{cb_under.get()} 적용 — 조회 중")

    def _populate_merge() -> None:
        # 활성 종목 가격으로 호가단위(틱) 콤보를 채운다 — '원시/2배' 대신 실제 틱 값.
        ref = _ref_price(active_symbol())
        merge_map.clear()
        vals: list[str] = []
        for s, nsf, mant in (_merge_ticks(float(ref)) if ref else []):
            vals.append(s)
            merge_map[s] = (nsf, mant)
        cb_merge["values"] = vals
        if vals:
            cb_merge.set(vals[0])  # 최소 틱(원시)

    def do_order() -> None:
        if not oneclick_var.get():  # 안전 잠금 — 원클릭 체크돼야만 발송(사용자 확정)
            set_status("원클릭 미체크 — 주문 안 나감", err=True)
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

    def on_hoga_click(_e: Any) -> None:
        # 가격모드에서만 — 클릭한 오더북 가격을 단가에 그대로(틱 없음).
        if mode_var.get() != "price":
            return
        sel = hoga.selection()
        if not sel:
            return
        vals = hoga.item(sel[0], "values")
        if len(vals) < 1 or vals[0] in ("", "-"):
            return
        e_price.delete(0, "end")
        e_price.insert(0, str(vals[0]).replace(",", ""))

    hoga.bind("<<TreeviewSelect>>", on_hoga_click)

    def _set_hoga_price(sym: dict[str, Any], dec: int) -> None:
        # 호가모드: 매수=매수호가+틱 / 매도=매도호가+틱 (자동, 시세 따라 갱신).
        buy = side_var.get() == "buy"
        levels = sym.get("bids") if buy else sym.get("asks")
        if not levels:
            return
        tick = 10.0 ** (-dec)  # 그 종목 소수 자리수의 최소 증분
        price = float(levels[0][0]) + tick_var.get() * tick
        e_price.delete(0, "end")
        e_price.insert(0, _fmt_px(price, dec).replace(",", ""))

    def on_merge(_e: Any) -> None:
        nsf, mant = merge_map.get(cb_merge.get(), (None, None))
        under = active["underlying"] or UNDER_MAP[cb_under.get()]
        send({"cmd": "manual_hl_merge", "underlying": under,
              "n_sig_figs": nsf, "mantissa": mant}, "머지")

    cb_merge.bind("<<ComboboxSelected>>", on_merge)
    btn_order.config(command=do_order)
    btn_apply.config(command=do_apply)
    # 레버리지 버튼 — 설정 팝업·updateLeverage 는 D단계에서 배선(지금은 자리·표시만)
    btn_lev.config(command=lambda: set_status("레버리지 설정은 준비 중(D단계)"))

    def refresh_side() -> None:
        buy = side_var.get() == "buy"
        name = active["name"] or cb_under.get()
        # 2줄 캡션(종목명/방향 주문) + 배경 매수 빨강·매도 파랑, 흰 글씨(§1)
        btn_order.config(text=f"{name}\n{'매수' if buy else '매도'} 주문",
                         bg="#c00000" if buy else "#0000c0",
                         activebackground="#a00000" if buy else "#000090")

    side_var.trace_add("write", lambda *_: refresh_side())
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
                    warns = "; ".join(result.get("warnings", []))
                    tail = (f" (#{oid})" if oid else "") + (f" ⚠ {warns}" if warns else "")
                    # 경고 있으면 빨간 글씨로 주의 환기(발주는 됨 — WS 불량 경고, §2 차단 아님)
                    if detail:  # 주문 성공 : 매수 167.5 10 (#주문번호) ⚠ 시세 지연...
                        set_status(f"주문 성공 : {detail}" + tail, err=bool(warns))
                    else:
                        set_status(f"{label} 접수됨" + tail, err=bool(warns))
        except queue.Empty:
            pass
        _reschedule(drain_results, 200)

    def _ref_price(sym: dict[str, Any]) -> Any:
        if sym.get("last") is not None:
            return sym.get("last")
        asks = sym.get("asks") or []
        bids = sym.get("bids") or []
        return (asks[0][0] if asks else None) or (bids[0][0] if bids else None)

    def refresh() -> None:
        try:
            sym = active_symbol()
            dec = _hl_decimals(_ref_price(sym))
            # 왼쪽(포지션) — 5개 다 스냅샷에 있음
            bal_val["수량"].config(text=_fmt_qty(sym.get("position")))
            bal_val["진입금액"].config(text=_fmt(sym.get("eval")))
            bal_val["진입가"].config(text=_fmt_px(sym.get("avg_price"), dec))
            bal_val["PNL"].config(text=_fmt(sym.get("pnl"), 2))
            bal_val["Liq_Prc"].config(text=_fmt_px(sym.get("liq"), dec))
            # 오른쪽 — 오라클·펀딩률(WS), 마진·누적펀딩(clearinghouse, B2), 카운트다운(화면 계산)
            bal_val["Margin"].config(text=_fmt(sym.get("margin"), 2))
            bal_val["Funding"].config(text=_fmt(sym.get("cum_funding"), 4))
            bal_val["Oracle"].config(text=_fmt_px(sym.get("oracle"), dec))
            rate = sym.get("funding_rate")
            bal_val["FundRate"].config(
                text=f"{rate * 100:.4f}%" if rate is not None else "-")
            secs = 3600 - (time.localtime().tm_min * 60 + time.localtime().tm_sec)
            bal_val["CountDown"].config(text=f"{secs // 60:02d}:{secs % 60:02d}")
            if mode_var.get() == "hoga":
                _set_hoga_price(sym, dec)
            # 내 미체결이 있는 호가에 "(건수)" 표시 — 활성 종목 미체결을 가격별 집계
            my_ords: dict[str, int] = {}
            for o in (state_box["data"] or {}).get("open_orders") or []:
                if (o.get("underlying") == active["underlying"]
                        and o.get("instrument") == INSTRUMENT):
                    ps = _fmt_px(o.get("price"), dec)
                    my_ords[ps] = my_ords.get(ps, 0) + 1
            _fill_hoga(sym, dec, my_ords)
        except Exception:  # noqa: BLE001 - 갱신 오류로 창이 죽지 않게 (버벅임 방지)
            pass
        _reschedule(refresh, 500)  # 호가창 갱신 주기 500ms (§1-4)

    def _fill_hoga(sym: dict[str, Any], dec: int, my_ords: dict[str, int]) -> None:
        asks = list(sym.get("asks") or [])[:5]  # 5호가 (§1-4)
        bids = list(sym.get("bids") or [])[:5]
        last = sym.get("last")
        last_s = _fmt_px(last, dec) if last is not None else None

        def _qcell(price_s: str, qty: Any) -> str:  # "(건수) 잔량" — 내 미체결 있으면
            qs = _fmt_qty(qty)
            n = my_ords.get(price_s)
            return f"({n}) {qs}" if n else qs

        # 매도(파랑) 위 → 매수(빨강) 아래. 현재가와 같은 호가만 노랑 바탕(별도 현재가 행 없음).
        draw: list[tuple[str, Any, Any]] = []
        for p, q in reversed(asks):
            ps = _fmt_px(p, dec)
            draw.append(("cur" if ps == last_s else "ask", ps, _qcell(ps, q)))
        for p, q in bids:
            ps = _fmt_px(p, dec)
            draw.append(("cur" if ps == last_s else "bid", ps, _qcell(ps, q)))
        if _hoga_signature(draw) == state_box.get("_hsig"):
            return
        state_box["_hsig"] = _hoga_signature(draw)
        hoga.delete(*hoga.get_children())
        for tag, ps, qs in draw:
            hoga.insert("", "end", values=(ps, qs), tags=(tag,))

    # 오더북(10행) 바닥을 좌측 입력열 바닥(=주문버튼)과 맞춘다 — 좌측열 실제 높이를 10등분해
    # 행높이로. 픽셀 추측 없이 정렬되고, 폰트·DPI가 달라도 따라간다. (사용자 요청)
    root.update_idletasks()
    _left_h = left.winfo_reqheight()
    if _left_h > 0:
        ttk.Style().configure("Treeview", rowheight=max(20, _left_h // 10))

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
