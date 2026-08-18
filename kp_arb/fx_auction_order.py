"""원달러선물 동시호가 대응주문 창 — 코어 클라이언트 (DESIGN-fx-auction.md §5).

주식선물(삼성·하이닉스) 신규주문을 코어가 감시해 원달러선물로 대응주문을 낸다.
이 창은 **설정·실행/정지·상태 표시**만 한다(감시·발주는 코어). 화면 스레드 네트워크 금지 —
전송·폴링은 뒷단 스레드 + 큐, 화면은 저장된 결과만 after()로 읽는다.

목업: docs/동시호가주문_run.png(실행중) · docs/동시호가_stop.png(정지).
"""
from __future__ import annotations

import queue
from typing import Any, cast

from . import ui_theme as T
from . import win_state
from .core_client import core_request, watch_parent_exit


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """원달러선물 동시호가 주문 창 실행."""
    import threading
    import time
    import tkinter as tk
    from tkinter import ttk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료 (고아 방지)
    root = tk.Tk()
    root.title("원달러선물 동시호가 주문")
    root.resizable(True, True)  # 크기 조절 — 발주내역 리스트만 확장(폼·상태바 고정)
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

    # ===== 상단 grid: col0 라벨 / col1 입력(좁게) / col2 [개시 체크 위 + 실행 버튼] =====
    form = tk.Frame(root)
    form.pack(fill="x", padx=8, pady=(8, 2))

    def _lab(text: str, r: int) -> None:
        tk.Label(form, text=text).grid(row=r, column=0, sticky="w", padx=(0, 6), pady=1)

    # 주문시간: 장전 시작~종료 / 마감 시작~종료 (4칸) — col1~2 걸침
    _lab("주문시간", 0)
    trow = tk.Frame(form)
    trow.grid(row=0, column=1, columnspan=2, sticky="w", pady=1)

    def _time_entry(default: str) -> tk.Entry:
        e = tk.Entry(trow, width=6, justify="center", font=T.FONT_NUM)
        e.insert(0, default)
        return e

    e_pre_s, e_pre_e = _time_entry("08:30"), _time_entry("08:46")
    e_cls_s, e_cls_e = _time_entry("15:35"), _time_entry("15:46")
    e_pre_s.pack(side="left")
    tk.Label(trow, text="~").pack(side="left", padx=2)
    e_pre_e.pack(side="left")
    tk.Label(trow, text="/").pack(side="left", padx=6)
    e_cls_s.pack(side="left")
    tk.Label(trow, text="~").pack(side="left", padx=2)
    e_cls_e.pack(side="left")

    # 종목: 원달러선물 종목코드(근·차근) 콤보 (col1) — 오른쪽 끝이 기준선
    _lab("종목", 1)
    cb_code = ttk.Combobox(form, values=[], width=12, state="readonly")
    cb_code.grid(row=1, column=1, sticky="w", pady=1)

    # 현재가 + 틱 (col1, 좁게 — 틱 오른쪽이 콤보 오른쪽에 가깝게)
    _lab("현재가", 2)
    prow = tk.Frame(form)
    prow.grid(row=2, column=1, sticky="we", pady=1)  # we: 틱을 col1 우측에 붙임
    e_price = tk.Entry(prow, width=7, justify="right", font=T.FONT_NUM)
    e_price.pack(side="left")
    tk.Label(prow, text="틱").pack(side="right")  # 우측 정렬(적용 버튼과 같은 세로선)
    e_tick = tk.Entry(prow, width=3, justify="right", font=T.FONT_NUM)
    e_tick.insert(0, "10")
    e_tick.pack(side="right", padx=(6, 2))

    # 헤지비율 (col1, 좁게) + 적용 버튼(실행 중 설정 변경 재적용) — %·적용 우측 정렬
    _lab("헤지비율", 3)
    rrow = tk.Frame(form)
    rrow.grid(row=3, column=1, sticky="we", pady=1)
    e_ratio = tk.Entry(rrow, width=7, justify="right", font=T.FONT_NUM)
    e_ratio.insert(0, "50")
    e_ratio.pack(side="left")
    btn_apply = tk.Button(rrow, text="적용", font=T.FONT_SMALL,
                          command=lambda: do_apply())  # 실행 중에만 활성(_apply_running)
    btn_apply.pack(side="right")
    tk.Label(rrow, text="%").pack(side="right", padx=(2, 2))

    # 오른쪽 공간(col2): 대응주문 개시 체크(종목 줄, 버튼 위) + 실행 버튼(현재가~헤지비율 걸침)
    form.columnconfigure(2, weight=1)  # 남는 우측 폭을 col2가 흡수 → 체크·버튼 우측 정렬
    arm_var = tk.BooleanVar(value=False)
    tk.Checkbutton(form, text="대응주문 개시", variable=arm_var).grid(
        row=1, column=2, sticky="se", padx=(14, 8))
    # height=1 + sticky nse: 걸친 두 행(현재가·헤지비율) 자연높이로 채우고 우측 정렬(가로 안 늘림).
    btn_run = tk.Button(form, text="실행", width=12, height=1, font=T.FONT_STRONG)
    btn_run.grid(row=2, column=2, rowspan=2, padx=(14, 8), sticky="nse")

    # 배치 주의: pack은 **나중에 pack된 위젯이 먼저 잘린다**. 안내(top)→상태바(bottom 먼저
    # 예약)→발주내역 리스트(맨 마지막 pack) 순으로 해야, 창을 줄일 때 리스트만 줄어든다.
    tk.Label(root, text="* 삼전닉스 주식선물 신규 주문일때만 대응주문",
             fg=T.C_MUTED).pack(anchor="w", padx=8, pady=(2, 0))
    status = tk.Label(root, text="-", anchor="w", relief="groove", width=1, fg=T.C_MUTED)
    status.pack(side="bottom", fill="x", padx=6, pady=(2, 6))  # 하단 고정(먼저 예약)
    _HCOLS: tuple[tuple[str, str, int, str], ...] = (
        ("time", "시각", 64, "center"), ("code", "종목", 78, "w"),
        ("side", "매매", 40, "center"), ("qty", "수량", 44, "e"),
        ("price", "가격", 64, "e"), ("st", "상태", 48, "center"))
    htf = tk.Frame(root)  # 맨 마지막 pack → 창 줄이면 이 리스트만 줄어듦
    htf.pack(fill="both", expand=True, padx=6, pady=(2, 0))
    hlog = ttk.Treeview(htf, columns=[c for c, *_ in _HCOLS], show="headings",
                        height=6, selectmode="none")
    hvsb = ttk.Scrollbar(htf, orient="vertical", command=hlog.yview)
    hlog.configure(yscrollcommand=hvsb.set)
    hvsb.pack(side="right", fill="y")
    hlog.pack(side="left", fill="both", expand=True)
    for c, title, w, a in _HCOLS:
        hlog.heading(c, text=title)
        hlog.column(c, width=w, anchor=cast(Any, a))
    hlog.tag_configure("buy", foreground=T.C_BUY)    # 대응 매수(주식선물 매도 시)
    hlog.tag_configure("sell", foreground=T.C_SELL)  # 대응 매도(주식선물 매수 시)

    def set_status(text: str, err: bool = False) -> None:
        status.config(text=text[:90], fg=T.C_ERR if err else T.C_MUTED)

    # ===== 동작 =====
    def _fx_state() -> dict[str, Any]:
        return (state_box["data"] or {}).get("fx_auction") or {}

    def _send_settings(label: str) -> None:
        fx_code = cb_code.get().strip()
        if not fx_code:
            set_status("원달러선물 종목코드 없음 — 코어 연결 확인", err=True)
            return
        try:
            payload = {
                "cmd": "fx_auction_start",
                "windows": [[e_pre_s.get().strip(), e_pre_e.get().strip()],
                            [e_cls_s.get().strip(), e_cls_e.get().strip()]],
                "fx_code": fx_code,
                "price": float(e_price.get().strip()),
                "tick": int(e_tick.get().strip()),
                "hedge_ratio": float(e_ratio.get().strip()),  # % (코어가 /100)
            }
        except ValueError:
            set_status("현재가·틱·헤지비율은 숫자로 입력하세요", err=True)
            return
        send(payload, label)

    def do_run() -> None:
        if not arm_var.get():  # 게이트 — 개시 체크돼야 실행
            set_status("대응주문 개시를 체크하세요", err=True)
            return
        _send_settings("실행")

    def do_apply() -> None:  # 실행 중 설정 변경 재적용(코어가 설정 교체) — 실행 중에만 활성
        _send_settings("적용")

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
        btn_apply.config(state="normal" if running else "disabled")  # 적용은 실행 중에만

    arm_var.trace_add(
        "write", lambda *_: _apply_running(bool(_fx_state().get("running"))))

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

    def refresh() -> None:
        fx = _fx_state()
        codes = [str(c) for c in (fx.get("codes") or [])]
        if codes != state_box.get("_codes"):  # 코드 목록 바뀜 → 콤보 갱신(선택 유지)
            state_box["_codes"] = codes
            cur = cb_code.get()
            cb_code.config(values=codes)
            want = state_box.pop("_want_code", None)  # 저장값 복원(코드 로드 후 1회)
            if want in codes:
                cb_code.set(want)
            elif cur in codes:
                cb_code.set(cur)
            elif codes:
                cb_code.current(0)
        # 대응 발주 내역(최신 우선) — 바뀔 때만 다시 그림
        hedges = fx.get("hedges") or []
        hsig = tuple((h.get("order_id"), h.get("status")) for h in hedges)
        if hsig != state_box.get("_hsig"):
            state_box["_hsig"] = hsig
            hlog.delete(*hlog.get_children())
            for h in hedges:
                sd = str(h.get("side"))
                hlog.insert("", "end", tags=("buy" if sd == "buy" else "sell",),
                            values=(h.get("time", ""), h.get("code", ""),
                                    "매수" if sd == "buy" else "매도",
                                    h.get("qty", ""), h.get("price", ""),
                                    h.get("status", "")))
        _apply_running(bool(fx.get("running")))
        _reschedule(refresh, 500)

    # 저장된 입력 복원 (종목코드는 목록 로드 후 refresh에서 복원)
    _saved = win_state.saved_fields("fx_auction_order")
    for e, key in ((e_pre_s, "pre_s"), (e_pre_e, "pre_e"), (e_cls_s, "cls_s"),
                   (e_cls_e, "cls_e"), (e_price, "price"), (e_tick, "tick"),
                   (e_ratio, "ratio")):
        if _saved.get(key):
            e.delete(0, "end")
            e.insert(0, str(_saved[key]))
    if _saved.get("code"):
        state_box["_want_code"] = str(_saved["code"])

    def _persist() -> None:
        win_state.save_fields("fx_auction_order", {
            "pre_s": e_pre_s.get(), "pre_e": e_pre_e.get(),
            "cls_s": e_cls_s.get(), "cls_e": e_cls_e.get(),
            "price": e_price.get(), "tick": e_tick.get(), "ratio": e_ratio.get(),
            "code": cb_code.get()})
        _reschedule(_persist, 2000)

    root.update_idletasks()
    # 최소 높이 = (트리 제외한 폼·안내·상태바) + 트리 한 줄 정도 → 트리를 거의 다 줄일 수 있음.
    _min_h = root.winfo_reqheight() - hlog.winfo_reqheight() + 26
    root.minsize(root.winfo_reqwidth(), max(120, _min_h))
    _apply_running(False)
    drain_results()
    refresh()
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
