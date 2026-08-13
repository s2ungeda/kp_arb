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


def _venue(instrument: str) -> str:
    """거래소 구분 — HL perp만 HL, 나머지(국내 주식/선물)는 LS."""
    return "HL" if instrument == "hl_perp" else "LS"


def _sym(underlying: object, instrument: str) -> str:
    """종목 표시 — 거래소는 별도 컬럼이라 여기선 종목명(+선물 태그)만."""
    return f"{underlying} 선물" if instrument == "kr_stock_future" else f"{underlying}"


# 주문상태 한글 표시 — '구분'의 '주문'과 헷갈리지 않게 상태는 한글로(accepted=접수 등).
_ST_KR = {"new": "신규", "accepted": "접수", "partial": "부분", "filled": "체결",
          "cancelled": "취소", "rejected": "거부"}


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """주문 리스트 창 실행."""
    import threading
    import time
    import tkinter as tk
    from tkinter import ttk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료 (고아 방지)
    root = tk.Tk()
    root.title("주문 리스트 (미체결·취소·정정)")
    root.resizable(True, True)  # 창 크기 조절 허용 — 목록이 세로로 늘어남(win_state가 크기 저장)
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

    # ===== UI (접수·체결 한 표 + 유형 필터) =====
    filt = tk.Frame(root)
    filt.pack(fill="x", padx=6, pady=(6, 0))
    tk.Label(filt, text="표시").pack(side="left")
    # 표시 필터 — 마지막 값 복원, 토글 때마다 저장(win_fields.json). 없으면 전부 켬.
    _saved = win_state.saved_fields("order_list")
    show_orders = tk.BooleanVar(value=bool(_saved.get("show_orders", True)))   # 미체결 주문
    show_fills = tk.BooleanVar(value=bool(_saved.get("show_fills", True)))      # 체결
    show_cancels = tk.BooleanVar(value=bool(_saved.get("show_cancels", True)))  # 취소

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

    # 구분='주문'(미체결)/'체결' — 상태(accepted 등)와 헷갈리지 않게 '주문'으로. 상태는 한글.
    tree = ttk.Treeview(
        root, columns=("kind", "ex", "sym", "side", "qty", "rem", "price", "st", "time"),
        show="headings", height=16, selectmode="browse")
    for c, t, w in (("kind", "구분", 44), ("ex", "거래소", 42), ("sym", "종목", 92),
                    ("side", "매매", 44), ("qty", "수량", 58), ("rem", "잔량", 58),
                    ("price", "가격", 84), ("st", "상태", 52), ("time", "시각", 72)):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="e")
    tree.column("kind", anchor="center")
    tree.column("ex", anchor="center")
    tree.column("side", anchor="center")
    tree.column("time", anchor="center")
    # 전부 검은색(사용자 확정) — 매수/매도는 '매매' 칸 텍스트로만 구분(색 없음).
    tree.pack(fill="both", expand=True, padx=6, pady=(2, 2))  # 창 크기 따라 세로로 확장

    status = tk.Label(root, text="-", anchor="w", relief="groove", width=1)

    def set_status(text: str, err: bool = False) -> None:
        status.config(text=text[:90], fg="#8b0000" if err else "black")

    def _selected_oid() -> str | None:
        sel = tree.selection()
        if not sel:
            return None
        iid = sel[0]
        return None if iid.startswith("__") else iid  # 체결·취소 행은 취소/정정 대상 아님

    def do_cancel() -> None:
        oid = _selected_oid()
        if oid is None:
            set_status("취소할 미체결을 선택하세요", err=True)
            return
        send({"cmd": "manual_cancel", "order_id": oid}, "취소")

    def do_amend() -> None:
        sel = tree.selection()
        oid = _selected_oid()
        if oid is None:
            set_status("정정할 미체결 주문을 선택하세요", err=True)
            return
        # HL은 정정 미지원(크로싱 시 원주문 소실 위험) — LS만 정정. 거래소 열로 판별.
        if str(tree.item(sel[0], "values")[1]) == "HL":
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

    # 정정가 입력 + 정정/취소 버튼 (LS만 정정 — HL은 미지원)
    ctrl = tk.Frame(root)
    ctrl.pack(fill="x", padx=6, pady=(0, 2))
    tk.Label(ctrl, text="정정가").pack(side="left")
    e_price = tk.Entry(ctrl, width=10, justify="right")
    e_price.pack(side="left", padx=(2, 8))
    tk.Button(ctrl, text="선택 정정", command=do_amend).pack(side="left")
    tk.Button(ctrl, text="선택 취소", command=do_cancel).pack(side="left", padx=4)
    tk.Label(ctrl, text="(LS 만 정정 가능)", fg="gray40").pack(side="left", padx=(6, 0))

    status.pack(fill="x", padx=6, pady=(2, 6))

    # 미체결 선택 시, 정정가 칸이 **비어 있을 때만** 현재가를 채운다(편의). 이미 입력한
    # 값이 있으면 덮지 않는다 — 가격 먼저 치고 주문을 골라도 입력이 안 날아가게.
    def on_select(_e: Any) -> None:
        if e_price.get().strip():
            return
        sel = tree.selection()
        if not sel or sel[0].startswith("__"):
            return
        vals = tree.item(sel[0], "values")  # (구분,거래소,종목,매매,수량,잔량,가격,상태,시각)
        if len(vals) >= 7 and vals[6] not in ("", "-"):
            e_price.delete(0, "end")
            e_price.insert(0, str(vals[6]).replace(",", ""))

    tree.bind("<<TreeviewSelect>>", on_select)

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
            _render()
        except Exception:  # noqa: BLE001 - 갱신 오류로 창이 죽지 않게
            pass
        _reschedule(refresh, 400)

    def _rows() -> list[tuple[str, str, tuple[Any, ...]]]:
        # (iid, tag, 값) — 필터(주문/체결)로 골라 한 표에. 주문(미체결) 먼저, 그다음 체결.
        # 상태·시각은 별 칸. 주문=상태+잔량(시각 없음) / 체결=시각(상태 없음).
        data = state_box["data"] or {}
        out: list[tuple[str, str, tuple[Any, ...]]] = []
        if show_orders.get():
            for o in data.get("open_orders") or []:
                buy = o.get("side") == "buy"
                inst = str(o.get("instrument"))
                stk = _ST_KR.get(str(o.get("status")), o.get("status"))  # 상태만(잔량 별 칸)
                out.append((str(o.get("order_id")), "buy" if buy else "sell",
                            ("주문", _venue(inst), _sym(o.get("underlying"), inst),
                             "매수" if buy else "매도",
                             _fmt_qty(o.get("qty")), _fmt_qty(o.get("remaining")),
                             _fmt_px(o.get("price")), stk, o.get("time", ""))))  # 접수시각
        if show_fills.get():
            for i, f in enumerate(data.get("fills") or []):
                buy = f.get("side") == "buy"
                inst = str(f.get("instrument"))
                out.append((f"__fill{i}", "buy" if buy else "sell",
                            ("체결", _venue(inst), _sym(f.get("underlying"), inst),
                             "매수" if buy else "매도",
                             _fmt_qty(f.get("qty")), "",  # 체결량=수량, 잔량 없음
                             _fmt_px(f.get("price")), "", f.get("time"))))  # 체결시각
        if show_cancels.get():
            for i, c in enumerate(data.get("cancels") or []):
                buy = c.get("side") == "buy"
                inst = str(c.get("instrument"))
                out.append((f"__cancel{i}", "buy" if buy else "sell",
                            ("취소", _venue(inst), _sym(c.get("underlying"), inst),
                             "매수" if buy else "매도",
                             _fmt_qty(c.get("qty")), "",
                             _fmt_px(c.get("price")), "취소", c.get("time"))))  # 취소시각
        return out

    def _render() -> None:
        rows = _rows()
        sig = tuple((iid, *vals) for iid, _, vals in rows)
        if sig == state_box.get("_sig"):
            return  # 변화 없으면 다시 안 그림(선택 유지)
        state_box["_sig"] = sig
        keep = tree.selection()
        tree.delete(*tree.get_children())
        for iid, tag, vals in rows:
            tree.insert("", "end", iid=iid, values=vals, tags=(tag,))
        for iid in keep:
            if tree.exists(iid):
                tree.selection_set(iid)

    def _rerender() -> None:
        state_box["_sig"] = None  # 필터 바뀜 → 강제 재그림
        _render()

    # 최소 크기 — 가로는 컬럼 전체 폭, 세로는 하단(버튼·상태바) 안 잘릴 만큼.
    root.update_idletasks()
    root.minsize(root.winfo_reqwidth(), 220)

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
