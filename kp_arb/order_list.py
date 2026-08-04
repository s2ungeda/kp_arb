"""주문 리스트/미체결 관리 화면 — 코어 클라이언트 (DESIGN-manual-order.md §6.3).

전체 미체결 주문(HL·LS 공통)을 표로 보고 **취소·정정**한다. 코어 명령
manual_cancel/manual_amend 사용(주문창과 별도 화면). 화면 스레드는 네트워크 금지 —
전송·폴링은 뒷단 스레드 + 큐, 화면은 저장된 결과만 after()로 읽는다.
"""
from __future__ import annotations

import queue
from typing import Any

from . import win_state
from .core_client import core_request, watch_parent_exit
from .order_hl import _fmt_px, _fmt_qty


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """주문 리스트 창 실행."""
    import threading
    import time
    import tkinter as tk
    from tkinter import ttk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료 (고아 방지)
    root = tk.Tk()
    root.title("kp-arb 주문 리스트 (미체결·취소·정정)")
    root.resizable(False, False)
    win_state.attach(root, "order_list")
    root.option_add("*Font", ("Malgun Gothic", 9))

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
        while True:
            state_box["data"] = core_request("/manual_state", timeout=2.0)
            time.sleep(0.5)

    threading.Thread(target=poller, daemon=True).start()

    # ===== UI =====
    orders = ttk.Treeview(
        root, columns=("oid", "sym", "side", "qty", "rem", "price", "st"),
        show="headings", height=12, selectmode="browse")
    for c, t, w in (("oid", "주문번호", 100), ("sym", "종목", 140), ("side", "구분", 44),
                    ("qty", "수량", 60), ("rem", "잔량", 60), ("price", "가격", 84),
                    ("st", "상태", 60)):
        orders.heading(c, text=t)
        orders.column(c, width=w, anchor="e")
    orders.column("side", anchor="center")
    orders.pack(fill="x", padx=6, pady=(6, 2))

    status = tk.Label(root, text="-", anchor="w", relief="groove", width=1)

    def set_status(text: str, err: bool = False) -> None:
        status.config(text=text[:90], fg="#8b0000" if err else "black")

    def _selected_oid() -> str | None:
        sel = orders.selection()
        return str(orders.item(sel[0], "values")[0]) if sel else None

    def do_cancel() -> None:
        oid = _selected_oid()
        if oid is None:
            set_status("취소할 미체결을 선택하세요", err=True)
            return
        send({"cmd": "manual_cancel", "order_id": oid}, "취소")

    def do_amend() -> None:
        oid = _selected_oid()
        if oid is None:
            set_status("정정할 미체결을 선택하세요", err=True)
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

    # 정정가 입력 + 정정/취소 버튼
    ctrl = tk.Frame(root)
    ctrl.pack(fill="x", padx=6, pady=(0, 2))
    tk.Label(ctrl, text="정정가").pack(side="left")
    e_price = tk.Entry(ctrl, width=10, justify="right")
    e_price.pack(side="left", padx=(2, 8))
    tk.Button(ctrl, text="선택 정정", command=do_amend).pack(side="left")
    tk.Button(ctrl, text="선택 취소", command=do_cancel).pack(side="left", padx=4)

    status.pack(fill="x", padx=6, pady=(2, 6))

    # 클릭한 미체결 가격을 정정가 칸에 채워두면 정정이 편하다.
    def on_select(_e: Any) -> None:
        sel = orders.selection()
        if not sel:
            return
        vals = orders.item(sel[0], "values")
        if len(vals) >= 6 and vals[5] not in ("", "-"):
            e_price.delete(0, "end")
            e_price.insert(0, str(vals[5]).replace(",", ""))

    orders.bind("<<TreeviewSelect>>", on_select)

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

    def refresh() -> None:
        try:
            data = state_box["data"] or {}
            _fill(data.get("open_orders") or [])
        except Exception:  # noqa: BLE001 - 갱신 오류로 창이 죽지 않게
            pass
        _reschedule(refresh, 400)

    def _fill(open_orders: list[dict[str, Any]]) -> None:
        sig = tuple((o.get("order_id"), o.get("remaining"), o.get("status"))
                    for o in open_orders)
        if sig == state_box.get("_sig"):
            return
        state_box["_sig"] = sig
        keep = orders.selection()
        orders.delete(*orders.get_children())
        for o in open_orders:
            sym = f"{o.get('underlying')} {o.get('instrument')}"
            side = "매수" if o.get("side") == "buy" else "매도"
            orders.insert("", "end", iid=str(o.get("order_id")),
                          values=(o.get("order_id"), sym, side,
                                  _fmt_qty(o.get("qty")), _fmt_qty(o.get("remaining")),
                                  _fmt_px(o.get("price")), o.get("status")))
        for iid in keep:
            if orders.exists(iid):
                orders.selection_set(iid)

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
