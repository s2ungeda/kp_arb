"""원달러선물 동시호가 대응주문 창 — 코어 클라이언트 (DESIGN-fx-auction.md §5).

주식선물(삼성·하이닉스) 신규주문을 코어가 감시해 원달러선물로 대응주문을 낸다.
이 창은 **설정·실행/정지·상태 표시**만 한다(감시·발주는 코어). 화면 스레드 네트워크 금지 —
전송·폴링은 뒷단 스레드 + 큐, 화면은 저장된 결과만 after()로 읽는다.

목업: docs/동시호가주문_run.png(실행중) · docs/동시호가_stop.png(정지).
"""
from __future__ import annotations

import queue
from typing import Any

from . import ui_theme as T
from . import win_state
from .core_client import core_request, watch_parent_exit

_MONTH_LABELS = ("최근월물", "차근월물")  # 콤보 표시 — 코어 fx_auction.codes[0/1]에 매핑


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """원달러선물 동시호가 주문 창 실행."""
    import threading
    import time
    import tkinter as tk
    from tkinter import ttk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료 (고아 방지)
    root = tk.Tk()
    root.title("원달러선물 동시호가 주문")
    root.resizable(False, False)
    win_state.attach(root, "fx_auction_order")
    T.apply_base(root)

    # --- 명령 전송 + 상태 폴링 (뒷단 스레드) ---
    jobs: queue.Queue[tuple[dict[str, Any], str]] = queue.Queue()
    results: queue.Queue[tuple[str, dict[str, Any] | None]] = queue.Queue()
    state_box: dict[str, Any] = {"data": None}

    def sender() -> None:
        while True:
            payload, label = jobs.get()
            results.put((label, core_request("/command", payload, timeout=10.0)))

    def poller() -> None:
        while True:
            state_box["data"] = core_request("/manual_state", timeout=2.0)
            time.sleep(0.5)

    threading.Thread(target=sender, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()

    def send(payload: dict[str, Any], label: str) -> None:
        jobs.put((payload, label))

    # ===== 입력 폼 =====
    form = tk.Frame(root)
    form.pack(fill="x", padx=8, pady=(8, 2))

    # 주문시간: 장전 시작~종료 / 마감 시작~종료 (4칸)
    tk.Label(form, text="주문시간").grid(row=0, column=0, sticky="w", pady=2)
    trow = tk.Frame(form)
    trow.grid(row=0, column=1, columnspan=3, sticky="w")

    def _time_entry(parent: tk.Frame, default: str) -> tk.Entry:
        e = tk.Entry(parent, width=6, justify="center", font=T.FONT_NUM)
        e.insert(0, default)
        return e

    e_pre_s = _time_entry(trow, "08:30")
    e_pre_e = _time_entry(trow, "08:46")
    e_cls_s = _time_entry(trow, "15:35")
    e_cls_e = _time_entry(trow, "15:46")
    e_pre_s.pack(side="left")
    tk.Label(trow, text="~").pack(side="left", padx=2)
    e_pre_e.pack(side="left")
    tk.Label(trow, text="/").pack(side="left", padx=6)
    e_cls_s.pack(side="left")
    tk.Label(trow, text="~").pack(side="left", padx=2)
    e_cls_e.pack(side="left")

    # 종목 콤보 + 대응주문 개시 체크
    tk.Label(form, text="종목").grid(row=1, column=0, sticky="w", pady=2)
    cb_month = ttk.Combobox(form, values=list(_MONTH_LABELS), width=8, state="readonly")
    cb_month.current(0)
    cb_month.grid(row=1, column=1, sticky="w")
    arm_var = tk.BooleanVar(value=False)  # 대응주문 개시 — 실행 게이트
    tk.Checkbutton(form, text="대응주문 개시", variable=arm_var).grid(
        row=1, column=2, columnspan=2, sticky="e")

    # 현재가 + 틱
    tk.Label(form, text="현재가").grid(row=2, column=0, sticky="w", pady=2)
    e_price = tk.Entry(form, width=9, justify="right", font=T.FONT_NUM)
    e_price.grid(row=2, column=1, sticky="w")
    e_tick = tk.Entry(form, width=4, justify="right", font=T.FONT_NUM)
    e_tick.grid(row=2, column=2, sticky="w", padx=(4, 0))
    tk.Label(form, text="틱").grid(row=2, column=3, sticky="w")

    # 헤지비율
    tk.Label(form, text="헤지비율").grid(row=3, column=0, sticky="w", pady=2)
    e_ratio = tk.Entry(form, width=9, justify="right", font=T.FONT_NUM)
    e_ratio.grid(row=3, column=1, sticky="w")
    tk.Label(form, text="%").grid(row=3, column=2, sticky="w")

    # 실행/자동주문실행중 버튼 — 현재가·헤지비율 행 오른쪽에 크게(목업)
    btn_run = tk.Button(form, text="실행", width=12, height=3, font=T.FONT_STRONG)
    btn_run.grid(row=2, column=4, rowspan=2, padx=(10, 0), sticky="nsew")

    # 안내 + 상태/시계
    tk.Label(root, text="* 삼전닉스 주식선물 신규 주문일때만 대응주문",
             fg=T.C_MUTED).pack(anchor="w", padx=8, pady=(2, 0))
    status = tk.Label(root, text="-", anchor="w", relief="groove", width=1, fg=T.C_MUTED)
    status.pack(side="bottom", fill="x", padx=6, pady=(2, 6))

    def set_status(text: str, err: bool = False) -> None:
        status.config(text=text[:90], fg=T.C_ERR if err else T.C_MUTED)

    # ===== 동작 =====
    def _fx_state() -> dict[str, Any]:
        data = state_box["data"] or {}
        return data.get("fx_auction") or {}

    def do_run() -> None:
        if not arm_var.get():  # 게이트 — 개시 체크돼야 실행
            set_status("대응주문 개시를 체크하세요", err=True)
            return
        codes = _fx_state().get("codes") or []
        idx = cb_month.current()
        if idx < 0 or idx >= len(codes):
            set_status("원달러선물 종목코드 없음 — 코어 연결 확인", err=True)
            return
        try:
            payload = {
                "cmd": "fx_auction_start",
                "windows": [[e_pre_s.get().strip(), e_pre_e.get().strip()],
                            [e_cls_s.get().strip(), e_cls_e.get().strip()]],
                "fx_code": str(codes[idx]),
                "price": float(e_price.get().strip()),
                "tick": int(e_tick.get().strip()),
                "hedge_ratio": float(e_ratio.get().strip()),  # % (코어가 /100)
            }
        except ValueError:
            set_status("현재가·틱·헤지비율은 숫자로 입력하세요", err=True)
            return
        send(payload, "실행")

    def do_stop() -> None:
        send({"cmd": "fx_auction_stop"}, "정지")
        arm_var.set(False)  # 정지 시 개시 해제(목업)

    def _apply_running(running: bool) -> None:
        # 실행중: '자동주문실행중'(노랑) → 클릭 시 정지 / 정지: '실행'(개시 체크 시만 활성)
        if running:
            btn_run.config(text="자동주문실행중", bg=T.C_HILITE_BG,
                           activebackground=T.C_HILITE_BG, command=do_stop, state="normal")
        else:
            btn_run.config(text="실행", bg=root.cget("bg"), command=do_run,
                           state=("normal" if arm_var.get() else "disabled"))

    arm_var.trace_add("write", lambda *_: _apply_running(_fx_state().get("running", False)))

    # ===== 갱신 루프 (네트워크 없음 — 폴링 결과만 읽음) =====
    def _reschedule(fn: Any, ms: int) -> None:
        try:
            root.after(ms, fn)
        except tk.TclError:
            pass

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
                    set_status(f"{label} 접수됨")
        except queue.Empty:
            pass
        _reschedule(drain_results, 200)

    def _clock() -> None:
        status_r.config(text=time.strftime("%H:%M:%S"))
        _reschedule(_clock, 1000)

    status_r = tk.Label(root, text="", anchor="e", fg=T.C_MUTED)
    status_r.pack(side="bottom", anchor="e", padx=8)

    def refresh() -> None:
        # 실행 상태(코어 authoritative)에 맞춰 버튼 캡션·색 갱신.
        _apply_running(bool(_fx_state().get("running")))
        _reschedule(refresh, 500)

    # 저장된 입력 복원
    _saved = win_state.saved_fields("fx_auction_order")
    for e, key in ((e_pre_s, "pre_s"), (e_pre_e, "pre_e"), (e_cls_s, "cls_s"),
                   (e_cls_e, "cls_e"), (e_price, "price"), (e_tick, "tick"),
                   (e_ratio, "ratio")):
        if key in _saved:
            e.delete(0, "end")
            e.insert(0, str(_saved[key]))
    if _saved.get("month") in (0, 1):
        cb_month.current(int(_saved["month"]))

    def _persist() -> None:
        win_state.save_fields("fx_auction_order", {
            "pre_s": e_pre_s.get(), "pre_e": e_pre_e.get(),
            "cls_s": e_cls_s.get(), "cls_e": e_cls_e.get(),
            "price": e_price.get(), "tick": e_tick.get(), "ratio": e_ratio.get(),
            "month": cb_month.current()})
        _reschedule(_persist, 2000)

    _apply_running(False)
    drain_results()
    refresh()
    _clock()
    _persist()
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
