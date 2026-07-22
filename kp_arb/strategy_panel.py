"""전략 화면 — 코어(core_server) 클라이언트 (DESIGN §6.2, §12 "코어 하나 + 여러 화면").

    1) core_server.bat  (코어 먼저)
    2) strategy_panel.bat

화면은 입력·버튼을 코어 명령(POST /command)으로 보내고 결과를 상태줄에 표시한다.
판단(검증·한도·환산)은 전부 코어. 코어가 없으면 "코어 미접속"으로 표시만 한다.
발주·실시세 반영(LiveSystem 결합)은 3단계 후반.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CORE_URL = "http://127.0.0.1:8787"

MODES = ("수동", "자동T", "자동M")
UNDERLYINGS = ("하이닉스", "삼성", "현대차")
COUNTERPARTS = ("주식", "주식선물")
SET_COUNT = 3  # 입력 세트 수 (DESIGN §6.2-1 — 코어 strategy_core.SET_COUNT와 일치)

# 화면 표기 → 코어 enum 값 (domain.enums)
UNDER_MAP = {"하이닉스": "sk_hynix", "삼성": "samsung", "현대차": "hyundai"}
COUNTER_MAP = {"주식": "kr_stock", "주식선물": "kr_stock_future"}


@dataclass(frozen=True)
class ModeUI:
    """모드별 위젯 상태 (DESIGN §6.2-1) — 세트마다 동일하게 적용."""

    threshold_enabled: bool       # 진입/청산 기준값 에디트 (자동만 입력)
    start_visible: bool           # 시작·PAUSE 체크박스 (자동만 표시)
    order_buttons_visible: bool   # 진입/청산 주문 버튼 (수동만 표시)


def mode_ui_state(mode: str) -> ModeUI:
    manual = mode == "수동"
    return ModeUI(
        threshold_enabled=not manual,
        start_visible=not manual,
        order_buttons_visible=manual,
    )


def parse_qty(text: str) -> int:
    """수량 에디트 값 → int. 빈칸/오타는 0 (코어 검증에서 걸러짐)."""
    try:
        return int(text.strip())
    except ValueError:
        return 0


def parse_threshold(text: str) -> float | None:
    """기준값 에디트 값 → float(%). 빈칸/오타는 None (자동 검증에서 걸러짐)."""
    try:
        return float(text.strip())
    except ValueError:
        return None


def core_request(path: str, payload: dict[str, Any] | None = None,
                 timeout: float = 1.0) -> dict[str, Any] | None:
    """코어 API 호출. 실패(미접속 등)는 None — 화면은 표시만 하고 판단하지 않는다."""
    url = f"{CORE_URL}{path}"
    try:
        if payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """화면 실행 — 코어에 명령을 보내는 클라이언트."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("kp-arb 전략")
    root.resizable(False, False)
    font = ("Malgun Gothic", 9)
    root.option_add("*Font", font)

    def set_status(text: str) -> None:
        status.config(text=text)

    def send(payload: dict[str, Any], label: str) -> dict[str, Any] | None:
        """명령 전송 + 상태줄 갱신. 코어 미접속/거부도 그대로 보여준다."""
        result = core_request("/command", payload)
        if result is None:
            set_status(f"{label} 실패 — 코어 미접속 (core_server.bat 먼저 실행)")
            return None
        if not result.get("ok"):
            set_status(f"{label} 거부 — {'; '.join(result.get('errors', []))}")
            return result
        set_status(f"{label} 완료")
        return result

    # --- 1행: 종목/상대/모드 콤보 + LS/HL 체크 + 옵션 ---
    row1 = tk.Frame(root)
    row1.pack(fill="x", padx=4, pady=2)
    cb_under = ttk.Combobox(row1, values=UNDERLYINGS, width=8, state="readonly")
    cb_under.set("하이닉스")
    cb_under.pack(side="left", padx=(0, 4))
    cb_counter = ttk.Combobox(row1, values=COUNTERPARTS, width=8, state="readonly")
    cb_counter.set("주식선물")
    cb_counter.pack(side="left", padx=(0, 4))
    cb_mode = ttk.Combobox(row1, values=MODES, width=6, state="readonly")
    cb_mode.set("수동")
    cb_mode.pack(side="left", padx=(0, 12))
    ls_on = tk.BooleanVar(value=True)
    hl_on = tk.BooleanVar(value=True)

    def send_venues() -> None:
        send({"cmd": "venues", "ls": ls_on.get(), "hl": hl_on.get()}, "거래소 선택")

    tk.Checkbutton(row1, text="LS", variable=ls_on, command=send_venues).pack(side="left")
    tk.Checkbutton(row1, text="HL", variable=hl_on, command=send_venues).pack(side="left")

    def send_select(_event: object = None) -> None:
        send({"cmd": "select", "underlying": UNDER_MAP[cb_under.get()],
              "counterpart": COUNTER_MAP[cb_counter.get()]}, "종목 선택")

    cb_under.bind("<<ComboboxSelected>>", send_select)
    cb_counter.bind("<<ComboboxSelected>>", send_select)

    def open_options() -> None:
        win = tk.Toplevel(root)
        win.title("옵션")
        win.resizable(False, False)
        win.transient(root)
        entries: dict[str, tk.Entry] = {}
        for i, (key, label, default) in enumerate([
            ("max_retries", "재시도 횟수", "3"),
            ("retry_interval_s", "재시도 간격(초)", "1.0"),
            ("wait_timer_s", "대기 타이머(초)", "5.0"),
        ]):
            tk.Label(win, text=label, anchor="w").grid(
                row=i, column=0, sticky="w", padx=6, pady=3)
            e = tk.Entry(win, width=8, justify="right")
            e.insert(0, default)
            e.grid(row=i, column=1, padx=6, pady=3)
            entries[key] = e
        tk.Label(win, text="주문 가능 시간", anchor="w").grid(
            row=3, column=0, sticky="w", padx=6, pady=3)
        time_frame = tk.Frame(win)
        time_frame.grid(row=3, column=1, padx=6, pady=3)
        ent_from = tk.Entry(time_frame, width=6, justify="center")
        ent_from.insert(0, "09:00")
        ent_from.pack(side="left")
        tk.Label(time_frame, text="~").pack(side="left", padx=2)
        ent_to = tk.Entry(time_frame, width=6, justify="center")
        ent_to.insert(0, "15:30")
        ent_to.pack(side="left")
        credit = tk.BooleanVar(value=False)
        tk.Checkbutton(win, text="주식 신용거래 사용", variable=credit).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=6, pady=3)

        def save_options() -> None:
            send({"cmd": "options",
                  "max_retries": parse_qty(entries["max_retries"].get()),
                  "retry_interval_s": parse_threshold(
                      entries["retry_interval_s"].get()) or 0.0,
                  "wait_timer_s": parse_threshold(entries["wait_timer_s"].get()) or 0.0,
                  "order_window": [ent_from.get().strip(), ent_to.get().strip()],
                  "stock_credit": credit.get()}, "옵션 저장")
            win.destroy()

        tk.Button(win, text="저장", width=8, command=save_options).grid(
            row=5, column=0, columnspan=2, pady=(4, 6))
        # 부모창 가운데 배치 + 모달(닫기 전까지 부모 조작 불가)
        win.update_idletasks()
        x = root.winfo_x() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_y() + (root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.focus_set()

    tk.Button(row1, text="옵션", command=open_options).pack(side="right")

    # --- 2행: 콤보 선택 2종목 현재가 + 주문 가능 시간 ---
    row2 = tk.Frame(root)
    row2.pack(fill="x", padx=4, pady=2)
    lbl_prices = tk.Label(row2, text="현재가: 국내 -  |  HL -", anchor="w")
    lbl_prices.pack(side="left")
    lbl_order_time = tk.Label(row2, text="주문가능: -", anchor="e", fg="gray25")
    lbl_order_time.pack(side="right")

    # --- 3행: 모니터링 — 전용 수량(estprice 계산) + 진입/청산 패널 (컴팩트) ---
    row3 = tk.Frame(root)
    row3.pack(fill="x", padx=4, pady=2)
    tk.Label(row3, text="수량").pack(side="left", padx=(0, 2))
    ent_mon_qty = tk.Entry(row3, width=6, justify="right")
    ent_mon_qty.pack(side="left", padx=(0, 8))
    ent_mon_qty.bind(
        "<Return>",
        lambda _e: send({"cmd": "monitor_qty",
                         "qty": parse_qty(ent_mon_qty.get())}, "모니터링 수량"))
    panel_font = ("Malgun Gothic", 11, "bold")
    lbl_panel_entry = tk.Label(row3, text="진입  -", bg="red", fg="white",
                               font=panel_font, width=13)
    lbl_panel_entry.pack(side="left", ipady=3, padx=(0, 2))
    lbl_panel_exit = tk.Label(row3, text="청산  -", bg="blue", fg="white",
                              font=panel_font, width=13)
    lbl_panel_exit.pack(side="left", ipady=3)

    # --- 입력 세트 ×3: 총진입/1회/진입/청산 + 시작/PAUSE + 주문 버튼 ---
    sets = tk.Frame(root)
    sets.pack(fill="x", padx=4, pady=2)
    for col, header in enumerate(("", "총진입", "1회", "진입", "청산")):
        tk.Label(sets, text=header, fg="gray25").grid(row=0, column=col, pady=(0, 1))

    @dataclass
    class SetRow:
        entries: list[tk.Entry]
        start_var: tk.BooleanVar
        chk_start: tk.Checkbutton
        pause_var: tk.BooleanVar
        chk_pause: tk.Checkbutton
        btn_in: tk.Button
        btn_out: tk.Button

    def send_inputs(index: int) -> None:
        e = set_rows[index].entries
        send({"cmd": "set_inputs", "set": index,
              "total": parse_qty(e[0].get()), "per": parse_qty(e[1].get()),
              "entry": parse_threshold(e[2].get()),
              "exit": parse_threshold(e[3].get())}, f"세트{index + 1} 입력")

    def toggle_start(index: int) -> Callable[[], None]:
        def _toggle() -> None:
            send_inputs(index)
            row = set_rows[index]
            result = send({"cmd": "start", "set": index, "value": row.start_var.get()},
                          f"세트{index + 1} 시작")
            if result is None or not result.get("ok"):
                row.start_var.set(False)  # 코어가 거부 — 화면 상태 되돌림
        return _toggle

    def toggle_pause(index: int) -> Callable[[], None]:
        def _toggle() -> None:
            row = set_rows[index]
            send({"cmd": "pause", "set": index, "value": row.pause_var.get()},
                 f"세트{index + 1} PAUSE")
        return _toggle

    def manual_order(index: int, action: str) -> Callable[[], None]:
        def _order() -> None:
            send_inputs(index)
            result = send({"cmd": "manual_order", "set": index, "action": action},
                          f"세트{index + 1} {action}주문")
            if result is not None and result.get("ok"):
                legs = result.get("plan", {}).get("legs", [])
                summary = " + ".join(
                    f"{leg['venue']} {leg['side']} {leg['qty']}" for leg in legs)
                set_status(f"세트{index + 1} {action} 계획: {summary} (발주는 3단계)")
        return _order

    set_rows: list[SetRow] = []
    for i in range(SET_COUNT):
        r = i + 1
        tk.Label(sets, text=f"세트{r}").grid(row=r, column=0, padx=(0, 4))
        entries: list[tk.Entry] = []
        for c in range(4):
            e = tk.Entry(sets, width=7, justify="right")
            e.grid(row=r, column=1 + c, padx=2, pady=1)
            entries.append(e)
        start_var = tk.BooleanVar(value=False)
        chk_start = tk.Checkbutton(sets, text="시작", variable=start_var,
                                   command=toggle_start(i))
        chk_start.grid(row=r, column=5, padx=(10, 2))
        pause_var = tk.BooleanVar(value=False)
        chk_pause = tk.Checkbutton(sets, text="PAUSE", fg="#8b0000",
                                   variable=pause_var, command=toggle_pause(i))
        chk_pause.grid(row=r, column=6, padx=(0, 2))
        btn_in = tk.Button(sets, text="진입주문", width=6, command=manual_order(i, "진입"))
        btn_in.grid(row=r, column=7, padx=1)
        btn_out = tk.Button(sets, text="청산주문", width=6, command=manual_order(i, "청산"))
        btn_out.grid(row=r, column=8, padx=(1, 0))
        set_rows.append(SetRow(entries, start_var, chk_start, pause_var, chk_pause,
                               btn_in, btn_out))

    # --- 하단: 현재진입수량 ---
    row_bottom = tk.Frame(root)
    row_bottom.pack(fill="x", padx=4, pady=2)
    lbl_position = tk.Label(row_bottom, text="현재진입수량: - / -", anchor="w")
    lbl_position.pack(side="left")

    # --- 상태줄 ---
    status = tk.Label(root, text="…", anchor="w", relief="groove")
    status.pack(fill="x", padx=4, pady=(2, 4))

    def apply_mode(_event: object = None) -> None:
        send({"cmd": "set_mode", "mode": cb_mode.get()}, "모드 전환")
        ui = mode_ui_state(cb_mode.get())
        for row in set_rows:
            if ui.threshold_enabled:
                row.entries[2].config(state="normal")   # 진입 기준값
                row.entries[3].config(state="normal")   # 청산 기준값
            else:
                row.entries[2].config(state="disabled")
                row.entries[3].config(state="disabled")
            if ui.start_visible:  # 시작·PAUSE는 자동 모드 전용 (수동엔 불필요)
                row.chk_start.grid()
                row.chk_pause.grid()
            else:
                row.start_var.set(False)
                row.chk_start.grid_remove()
                row.pause_var.set(False)
                row.chk_pause.grid_remove()
            if ui.order_buttons_visible:
                row.btn_in.grid()
                row.btn_out.grid()
            else:
                row.btn_in.grid_remove()
                row.btn_out.grid_remove()

    cb_mode.bind("<<ComboboxSelected>>", apply_mode)
    apply_mode()
    if core_request("/state") is None:
        set_status("코어 미접속 — core_server.bat 먼저 실행 (화면만 확인 가능)")
    else:
        set_status("코어 연결됨")

    # 콘솔로 Ctrl-C 신호가 흘러들어도(직접 누르지 않아도 생김) 화면을 죽이지 않는다.
    while True:
        try:
            root.mainloop()
            break  # 창이 닫혀 정상 종료
        except KeyboardInterrupt:
            try:
                root.winfo_exists()
            except tk.TclError:
                break  # 창도 이미 닫힘 — 종료


if __name__ == "__main__":
    main()
