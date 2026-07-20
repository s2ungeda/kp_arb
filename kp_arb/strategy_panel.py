"""전략 화면 UI 시안 (DESIGN §6.2) — 배치·구성 확인용, 주문·코어 연결 전.

    python -m kp_arb.strategy_panel   (또는 strategy_panel.bat)

모니터와 같은 tkinter 컴팩트 창. 지금은 수치가 비어 있는 시안이다.
- 입력 세트(총진입/1회/진입/청산) ×3 — 세트별 시작 체크박스·진입/청산 주문 버튼.
- 모니터링: 전용 수량(estprice 계산용) + 진입/청산 패널 + 콤보 2종목 현재가 + 주문가능시간.
- 모드(수동/자동T/자동M)에 따라 위젯이 §6.2 규칙대로 바뀐다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

MODES = ("수동", "자동T", "자동M")
UNDERLYINGS = ("하이닉스", "삼성", "현대차")
COUNTERPARTS = ("주식", "주식선물")
SET_COUNT = 3  # 입력 세트 수 (추후 확장 가능 — DESIGN §6.2-1)


@dataclass(frozen=True)
class ModeUI:
    """모드별 위젯 상태 (DESIGN §6.2-1) — 세트마다 동일하게 적용."""

    threshold_enabled: bool       # 진입/청산 기준값 에디트 (자동만 입력)
    start_visible: bool           # 시작 체크박스 (자동만 표시)
    order_buttons_visible: bool   # 진입/청산 주문 버튼 (수동만 표시)


def mode_ui_state(mode: str) -> ModeUI:
    manual = mode == "수동"
    return ModeUI(
        threshold_enabled=not manual,
        start_visible=not manual,
        order_buttons_visible=manual,
    )


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """시안 창 실행 — 코어 미연결, 배치 확인용."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("kp-arb 전략 (시안)")
    root.resizable(False, False)
    font = ("Malgun Gothic", 9)
    root.option_add("*Font", font)

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
    tk.Checkbutton(row1, text="LS", variable=ls_on).pack(side="left")
    tk.Checkbutton(row1, text="HL", variable=hl_on).pack(side="left")

    def open_options() -> None:
        win = tk.Toplevel(root)
        win.title("옵션")
        win.resizable(False, False)
        win.transient(root)
        for i, (label, default) in enumerate(
            [("재시도 횟수", "3"), ("재시도 간격(초)", "1.0"), ("대기 타이머(초)", "5.0")]
        ):
            tk.Label(win, text=label, anchor="w").grid(
                row=i, column=0, sticky="w", padx=6, pady=3)
            e = tk.Entry(win, width=8, justify="right")
            e.insert(0, default)
            e.grid(row=i, column=1, padx=6, pady=3)
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
        tk.Button(win, text="저장", width=8, command=win.destroy).grid(
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
    panel_font = ("Malgun Gothic", 11, "bold")
    lbl_panel_entry = tk.Label(row3, text="진입  -", bg="red", fg="white",
                               font=panel_font, width=13)
    lbl_panel_entry.pack(side="left", ipady=3, padx=(0, 2))
    lbl_panel_exit = tk.Label(row3, text="청산  -", bg="blue", fg="white",
                              font=panel_font, width=13)
    lbl_panel_exit.pack(side="left", ipady=3)

    # --- 입력 세트 ×3: 총진입/1회/진입/청산 + 시작 + 주문 버튼 ---
    sets = tk.Frame(root)
    sets.pack(fill="x", padx=4, pady=2)
    for col, header in enumerate(("", "총진입", "1회", "진입", "청산")):
        tk.Label(sets, text=header, fg="gray25").grid(row=0, column=col, pady=(0, 1))

    def set_status(text: str) -> None:
        status.config(text=text)

    def order_command(n: int, action: str) -> Callable[[], None]:
        return lambda: set_status(f"세트{n} {action} — (시안: 코어 미연결)")

    set_rows: list[tuple[list[tk.Entry], tk.Checkbutton, tk.Button, tk.Button]] = []
    for i in range(SET_COUNT):
        r = i + 1
        tk.Label(sets, text=f"세트{r}").grid(row=r, column=0, padx=(0, 4))
        entries: list[tk.Entry] = []
        for c in range(4):
            e = tk.Entry(sets, width=7, justify="right")
            e.grid(row=r, column=1 + c, padx=2, pady=1)
            entries.append(e)
        chk_start = tk.Checkbutton(sets, text="시작", variable=tk.BooleanVar(sets))
        chk_start.grid(row=r, column=5, padx=(10, 2))
        btn_in = tk.Button(sets, text="진입주문", width=8,
                           command=order_command(r, "진입주문"))
        btn_in.grid(row=r, column=6, padx=2)
        btn_out = tk.Button(sets, text="청산주문", width=8,
                            command=order_command(r, "청산주문"))
        btn_out.grid(row=r, column=7, padx=(2, 0))
        set_rows.append((entries, chk_start, btn_in, btn_out))

    # --- 하단: 현재진입수량 + PAUSE ---
    row_bottom = tk.Frame(root)
    row_bottom.pack(fill="x", padx=4, pady=2)
    lbl_position = tk.Label(row_bottom, text="현재진입수량: - / -", anchor="w")
    lbl_position.pack(side="left")
    tk.Button(row_bottom, text="PAUSE", width=8, fg="white", bg="#8b0000",
              command=lambda: set_status("PAUSE — (시안: 코어 미연결)")
              ).pack(side="right")

    # --- 상태줄 ---
    status = tk.Label(root, text="시안 — 코어 미연결", anchor="w", relief="groove")
    status.pack(fill="x", padx=4, pady=(2, 4))

    def apply_mode(_event: object = None) -> None:
        ui = mode_ui_state(cb_mode.get())
        for entries, chk_start, btn_in, btn_out in set_rows:
            if ui.threshold_enabled:
                entries[2].config(state="normal")   # 진입 기준값
                entries[3].config(state="normal")   # 청산 기준값
            else:
                entries[2].config(state="disabled")
                entries[3].config(state="disabled")
            if ui.start_visible:
                chk_start.grid()
            else:
                chk_start.deselect()
                chk_start.grid_remove()
            if ui.order_buttons_visible:
                btn_in.grid()
                btn_out.grid()
            else:
                btn_in.grid_remove()
                btn_out.grid_remove()

    cb_mode.bind("<<ComboboxSelected>>", apply_mode)
    apply_mode()

    root.mainloop()


if __name__ == "__main__":
    main()
