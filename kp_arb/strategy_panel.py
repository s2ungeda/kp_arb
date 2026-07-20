"""전략 화면 UI 시안 (DESIGN §6.2) — 배치·구성 확인용, 주문·코어 연결 전.

    python -m kp_arb.strategy_panel   (또는 strategy_panel.bat)

모니터와 같은 tkinter 컴팩트 창. 지금은 수치가 비어 있는 시안이다.
모드(수동/자동T/자동M)에 따라 위젯이 §6.2 규칙대로 바뀐다.
"""
from __future__ import annotations

from dataclasses import dataclass

MODES = ("수동", "자동T", "자동M")
UNDERLYINGS = ("하이닉스", "삼성", "현대차")
COUNTERPARTS = ("주식", "주식선물")


@dataclass(frozen=True)
class ModeUI:
    """모드별 위젯 상태 (DESIGN §6.2-1)."""

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
        win.title("옵션 — 재시도")
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
        tk.Button(win, text="저장", width=8, command=win.destroy).grid(
            row=3, column=0, columnspan=2, pady=(4, 6))

    tk.Button(row1, text="옵션", command=open_options).pack(side="right")

    # --- 2행: 수량·기준값 입력 ---
    row2 = tk.Frame(root)
    row2.pack(fill="x", padx=4, pady=2)

    def labeled_entry(parent: tk.Frame, label: str, width: int = 7) -> tk.Entry:
        tk.Label(parent, text=label).pack(side="left", padx=(0, 2))
        entry = tk.Entry(parent, width=width, justify="right")
        entry.pack(side="left", padx=(0, 8))
        return entry

    labeled_entry(row2, "총진입")
    labeled_entry(row2, "1회")
    ent_entry_th = labeled_entry(row2, "진입")
    ent_exit_th = labeled_entry(row2, "청산")
    tk.Label(row2, text="(%)").pack(side="left")

    # --- 3행: 현재진입수량 + 시작 + PAUSE ---
    row3 = tk.Frame(root)
    row3.pack(fill="x", padx=4, pady=2)
    lbl_position = tk.Label(row3, text="현재진입수량: - / -", anchor="w")
    lbl_position.pack(side="left")
    started = tk.BooleanVar(value=False)
    chk_start = tk.Checkbutton(row3, text="시작", variable=started)
    btn_pause = tk.Button(row3, text="PAUSE", width=8,
                          fg="white", bg="#8b0000",
                          command=lambda: set_status("PAUSE — (시안: 코어 미연결)"))
    btn_pause.pack(side="right")
    # chk_start 는 모드에 따라 pack/pack_forget (아래 apply_mode)

    # --- 4행: 진입/청산 패널 (estprice 기반, §6.2-1) ---
    row4 = tk.Frame(root)
    row4.pack(fill="x", padx=4, pady=2)
    panel_font = ("Malgun Gothic", 16, "bold")
    lbl_panel_entry = tk.Label(row4, text="진입\n-", bg="red", fg="white",
                               font=panel_font, width=10, height=2)
    lbl_panel_entry.pack(side="left", expand=True, fill="both", padx=(0, 2))
    lbl_panel_exit = tk.Label(row4, text="청산\n-", bg="blue", fg="white",
                              font=panel_font, width=10, height=2)
    lbl_panel_exit.pack(side="left", expand=True, fill="both", padx=(2, 0))

    # --- 5행: 수동 주문 버튼 ---
    row5 = tk.Frame(root)
    row5.pack(fill="x", padx=4, pady=2)
    btn_enter = tk.Button(row5, text="진입주문", width=12,
                          command=lambda: set_status("진입주문 — (시안: 코어 미연결)"))
    btn_exit = tk.Button(row5, text="청산주문", width=12,
                         command=lambda: set_status("청산주문 — (시안: 코어 미연결)"))

    # --- 상태줄 ---
    status = tk.Label(root, text="시안 — 코어 미연결", anchor="w", relief="groove")
    status.pack(fill="x", padx=4, pady=(2, 4))

    def set_status(text: str) -> None:
        status.config(text=text)

    def apply_mode(_event: object = None) -> None:
        ui = mode_ui_state(cb_mode.get())
        if ui.threshold_enabled:
            ent_entry_th.config(state="normal")
            ent_exit_th.config(state="normal")
        else:
            ent_entry_th.config(state="disabled")
            ent_exit_th.config(state="disabled")
        if ui.start_visible:
            chk_start.pack(side="left", padx=(12, 0))
        else:
            started.set(False)
            chk_start.pack_forget()
        if ui.order_buttons_visible:
            btn_enter.pack(side="left", expand=True, padx=(0, 2))
            btn_exit.pack(side="left", expand=True, padx=(2, 0))
        else:
            btn_enter.pack_forget()
            btn_exit.pack_forget()

    cb_mode.bind("<<ComboboxSelected>>", apply_mode)
    apply_mode()

    root.mainloop()


if __name__ == "__main__":
    main()
