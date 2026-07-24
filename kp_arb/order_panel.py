"""주문 화면 (자동T=HL-주식 / 자동M=HL-주식선물) — 코어 클라이언트 (DESIGN §6.2 개정).

    python -m kp_arb.order_panel autoT|autoM   (운영은 main.bat 메뉴에서)

화면은 입력·버튼을 코어 명령(POST /command)으로 보내고 결과를 표시만 한다.
판단(검증·한도·주문)은 전부 코어.

**화면 스레드는 네트워크를 하지 않는다**(CLAUDE.md — 어기면 창이 얼어 버벅임):
- 명령은 작업 큐에 넣고 뒷단 전송 스레드가 보낸다. 응답은 결과 큐로 돌아와
  화면 루프(after)가 상태줄 갱신·버튼 되돌림 등을 처리한다.
- 수치 갱신도 뒷단 폴링 스레드가 /state를 1초마다 받아두면 화면은 읽기만 한다.
"""
from __future__ import annotations

import queue
import sys
from collections.abc import Callable
from typing import Any

from .core_client import core_request
from .strategy_core import OPERATING_WINDOWS, ScreenKind

UNDERLYINGS = ("하이닉스", "삼성", "현대차")
UNDER_MAP = {"하이닉스": "sk_hynix", "삼성": "samsung", "현대차": "hyundai"}
TITLES = {
    ScreenKind.AUTO_T: "자동T — HL-주식 (동시 taker)",
    ScreenKind.AUTO_M: "자동M — HL-주식선물 (LS maker→HL taker)",
}
BLOCK_LABELS = (("entry", "entry", "red"),
                ("exit", "exit", "blue"))


def parse_qty(text: str) -> int:
    """수량 에디트 값 → int. 빈칸/오타는 0 (코어 검증에서 걸러짐)."""
    try:
        return int(text.strip())
    except ValueError:
        return 0


def parse_threshold(text: str) -> float | None:
    """기준값 에디트 값 → float(% 단위 그대로). 빈칸/오타는 None."""
    try:
        return float(text.strip())
    except ValueError:
        return None


def threshold_to_fraction(text: str) -> float | None:
    """기준값(%) 입력 → 코어 소수값. 괴리보드 표시와 같은 단위 — 0.075 = 0.075%."""
    value = parse_threshold(text)
    return None if value is None else value / 100.0


def fraction_to_pct_text(value: float) -> str:
    """코어 소수값 → 화면 %(입력칸 채움용). 0.00075 → '0.075'."""
    return f"{value * 100:g}"


def is_int_text(text: str) -> bool:
    """정수 입력칸 허용 문자 검사 — 빈칸 또는 숫자만 (키 입력마다 호출)."""
    return text == "" or text.isdigit()


def is_decimal_text(text: str) -> bool:
    """소수 입력칸 허용 검사 — 부호/소수점 포함 숫자 형태만 (입력 중간 상태 허용)."""
    import re

    return re.fullmatch(r"-?\d*\.?\d*", text) is not None


def operating_text(kind: ScreenKind) -> str:
    """운영시간 표시 문자열 — 코어 규칙(OPERATING_WINDOWS)과 같은 원본."""
    return " / ".join(f"{s}~{e}" for s, e in OPERATING_WINDOWS[kind])


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """화면 실행. 인자: autoT(기본) | autoM."""
    import threading
    import time
    import tkinter as tk
    from tkinter import ttk

    kind = ScreenKind(sys.argv[1]) if len(sys.argv) > 1 else ScreenKind.AUTO_T
    screen_key = kind.value

    root = tk.Tk()
    root.title(f"kp-arb {TITLES[kind]}")
    root.resizable(False, False)
    root.option_add("*Font", ("Malgun Gothic", 9))
    # 입력 제한 — 키 입력마다 검사해 글자·특수기호를 막는다
    vcmd_int = (root.register(is_int_text), "%P")
    vcmd_dec = (root.register(is_decimal_text), "%P")

    # --- 명령 전송: 작업 큐 → 전송 스레드 → 결과 큐 → 화면 루프 ---
    Job = tuple[dict[str, Any], str, Callable[[dict[str, Any] | None], None] | None]
    jobs: queue.Queue[Job] = queue.Queue()
    results: queue.Queue[tuple[str, dict[str, Any] | None,
                               Callable[[dict[str, Any] | None], None] | None]] = queue.Queue()

    def sender() -> None:
        while True:
            payload, label, callback = jobs.get()
            results.put((label, core_request("/command", payload), callback))

    threading.Thread(target=sender, daemon=True).start()

    def send(payload: dict[str, Any], label: str,
             callback: Callable[[dict[str, Any] | None], None] | None = None) -> None:
        """명령을 큐에만 넣는다 — 화면 스레드는 기다리지 않음."""
        jobs.put(({**payload, "screen": screen_key}, label, callback))

    def set_status(text: str) -> None:
        status.config(text=text)

    def drain_results() -> None:
        try:
            while True:
                label, result, callback = results.get_nowait()
                if result is None:
                    set_status(f"{label} 실패 — 코어 미접속 (메인 화면에서 코어 시작)")
                elif not result.get("ok"):
                    set_status(f"{label} 거부 — {'; '.join(result.get('errors', []))}")
                else:
                    warnings = result.get("warnings") or []
                    set_status(f"{label} 저장됨"  # '완료'는 목표수량 달성과 헷갈려 변경
                               + (f" (경고: {'; '.join(warnings)})" if warnings else ""))
                if callback is not None:
                    callback(result)
        except queue.Empty:
            pass
        try:
            root.after(200, drain_results)
        except tk.TclError:
            pass  # 창 닫힘

    unit = "주" if kind is ScreenKind.AUTO_T else "계약"

    # --- 1행: 종목 + 공통설정 ---
    row1 = tk.Frame(root)
    row1.pack(fill="x", padx=4, pady=2)
    tk.Label(row1, text="종목").pack(side="left")
    cb_under = ttk.Combobox(row1, values=UNDERLYINGS, width=8, state="readonly")
    cb_under.set("하이닉스")
    cb_under.pack(side="left", padx=(2, 10))
    cb_under.bind("<<ComboboxSelected>>", lambda _e: send(
        {"cmd": "select", "underlying": UNDER_MAP[cb_under.get()]}, "종목 선택"))
    lbl_hours = tk.Label(row1, text=f"운영 {operating_text(kind)}", fg="gray25")
    lbl_hours.pack(side="left", padx=(4, 0))

    def open_settings() -> None:
        win = tk.Toplevel(root)
        win.title(f"공통설정 — {kind.value}")
        win.resizable(False, False)
        win.transient(root)
        # 뒷단 폴링이 받아둔 최신 상태에서 채움 (네트워크 호출 없음)
        data = state_box["data"] or {}
        raw = ((data.get("screens") or {}).get(screen_key) or {}).get("settings")
        saved = raw if isinstance(raw, dict) else {}
        fields = [  # (키, 라벨, 기본값, 종류 int/dec/text)
            ("kr_margin_ticks", "국내 주문가 여유(틱)",
             saved.get("kr_margin_ticks", 10), "int"),
            ("hl_margin_pct", "하리 주문가 여유(예 0.01=1%)",
             saved.get("hl_margin_pct", 0.01), "dec"),
            ("max_position", "종목보유최대수량", saved.get("max_position", 0), "int"),
            ("daily_limit_100m", "일거래한도(억, 0=미사용)",
             saved.get("daily_limit_100m", 0.0), "dec"),
        ]
        if kind is ScreenKind.AUTO_M:
            fields += [
                ("delay_ms", "딜레이(ms)", saved.get("delay_ms", 500), "int"),
                ("pre_order_range_ticks", "선주문진입범위(틱)",
                 saved.get("pre_order_range_ticks", 0), "int"),
            ]
        fields.append(("operating_hours", "운영시간 (빈칸=기본값)",
                       saved.get("operating_hours") or "", "text"))
        entries: dict[str, tk.Entry] = {}
        kinds: dict[str, str] = {}
        for i, (key, label, default, field_kind) in enumerate(fields):
            tk.Label(win, text=label, anchor="w").grid(
                row=i, column=0, sticky="w", padx=6, pady=3)
            if field_kind == "text":
                e = tk.Entry(win, width=24)
            else:
                e = tk.Entry(win, width=10, justify="right", validate="key",
                             validatecommand=(vcmd_int if field_kind == "int"
                                              else vcmd_dec))
            e.insert(0, str(default))
            e.grid(row=i, column=1, padx=6, pady=3)
            entries[key] = e
            kinds[key] = field_kind
        tk.Label(win, text=f"운영시간 형식: 08:00-08:50,09:00-15:30 (기본 {operating_text(kind)})",
                 fg="gray40").grid(row=len(fields), column=0, columnspan=2,
                                   sticky="w", padx=6)

        def save_settings() -> None:
            payload: dict[str, Any] = {"cmd": "settings"}
            for key, entry in entries.items():
                if kinds[key] == "text":
                    payload[key] = entry.get().strip()
                else:
                    value = parse_threshold(entry.get())
                    payload[key] = value if value is not None else 0
            send(payload, "설정 저장")
            win.destroy()

        buttons = tk.Frame(win)
        buttons.grid(row=len(fields) + 1, column=0, columnspan=2, pady=(4, 6))
        tk.Button(buttons, text="저장", width=8, command=save_settings).pack(
            side="left", padx=4)
        tk.Button(buttons, text="취소", width=8, command=win.destroy).pack(
            side="left", padx=4)
        win.update_idletasks()
        x = root.winfo_x() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_y() + (root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.focus_set()

    tk.Button(row1, text="공통설정", command=open_settings).pack(side="right")

    # --- 실시간 표시(7-3a): 진입/청산 신호(est, %) + 현재가·환율 한 줄 ---
    live_row = tk.Frame(root)
    live_row.pack(fill="x", padx=4, pady=2)
    signal_font = ("Malgun Gothic", 10, "bold")
    lbl_sig_entry = tk.Label(live_row, text="진입 -", bg="red", fg="white",
                             font=signal_font, width=11)
    lbl_sig_entry.pack(side="left", ipady=1, padx=(0, 2))
    lbl_sig_exit = tk.Label(live_row, text="청산 -", bg="blue", fg="white",
                            font=signal_font, width=11)
    lbl_sig_exit.pack(side="left", ipady=1)
    lbl_prices = tk.Label(live_row, text="- | - | -", anchor="w", fg="gray25")
    lbl_prices.pack(side="left", padx=(8, 0))

    # --- entry/exit 블록: 세트 3줄 ---
    # 기준값·목표진입량은 세트설정창에서만 수정(자동 반영 오주문 방지 — 사용자 확정).
    # 1회주문수량은 블록별로 헤더에 두고 '적용' 버튼으로 수시 반영.
    kr_tag = "S" if kind is ScreenKind.AUTO_T else "SF"
    grid = tk.Frame(root)
    grid.pack(fill="x", padx=4, pady=2)
    headers = ("", "기준값%", "목표량", "설정", "실행", "LS", "진입수량", "환율",
               "avg HL", f"avg {kr_tag}")
    row_no = 0
    set_widgets: dict[tuple[str, int], dict[str, Any]] = {}
    per_entries: dict[str, tk.Entry] = {}
    lbl_position: tk.Label | None = None

    def send_per_qty(block: str) -> Callable[[], None]:
        def _apply() -> None:
            send({"cmd": "per_qty", "block": block,
                  "qty": parse_qty(per_entries[block].get())},
                 f"{block} 1회주문수량")
        return _apply

    def toggle_run(block: str, index: int) -> None:
        w = set_widgets[(block, index)]
        turning_on = not w["running"]

        def on_result(result: dict[str, Any] | None) -> None:
            if result is not None and result.get("ok"):
                w["running"] = turning_on
                w["button"].config(text="중지" if turning_on else "실행",
                                   bg="#1a7a1a" if turning_on else "SystemButtonFace",
                                   fg="white" if turning_on else "black")
        send({"cmd": "run", "block": block, "set": index, "value": turning_on},
             f"{block} {index + 1}세트 실행", on_result)

    def run_command(block: str, index: int) -> Callable[[], None]:
        return lambda: toggle_run(block, index)

    def open_set_settings(block: str, index: int) -> None:
        """세트설정창 — 기준값·목표진입량 입력·저장 시 반영 (실행 중에도 명시적으로)."""
        w = set_widgets[(block, index)]
        win = tk.Toplevel(root)
        win.title(f"{block} {index + 1}세트 설정")
        win.resizable(False, False)
        win.transient(root)
        tk.Label(win, text="기준값(%)", anchor="w").grid(
            row=0, column=0, sticky="w", padx=6, pady=3)
        e_th = tk.Entry(win, width=10, justify="right", validate="key",
                        validatecommand=vcmd_dec)
        e_th.insert(0, w["threshold_val"])
        e_th.grid(row=0, column=1, padx=6, pady=3)
        tk.Label(win, text="목표진입량", anchor="w").grid(
            row=1, column=0, sticky="w", padx=6, pady=3)
        e_tg = tk.Entry(win, width=10, justify="right", validate="key",
                        validatecommand=vcmd_int)
        e_tg.insert(0, w["target_val"])
        e_tg.grid(row=1, column=1, padx=6, pady=3)

        def save() -> None:
            send({"cmd": "set_threshold", "block": block, "set": index,
                  "value": threshold_to_fraction(e_th.get())}, "기준값")
            send({"cmd": "set_target", "block": block, "set": index,
                  "value": parse_qty(e_tg.get())}, "목표진입량")
            win.destroy()

        buttons = tk.Frame(win)
        buttons.grid(row=2, column=0, columnspan=2, pady=(4, 6))
        tk.Button(buttons, text="저장", width=8, command=save).pack(side="left", padx=4)
        tk.Button(buttons, text="취소", width=8, command=win.destroy).pack(
            side="left", padx=4)
        win.update_idletasks()
        x = root.winfo_x() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_y() + (root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.focus_set()

    def set_settings_command(block: str, index: int) -> Callable[[], None]:
        return lambda: open_set_settings(block, index)

    def fired_reset(block: str, index: int) -> Callable[[object], None]:
        def _reset(_event: object) -> None:
            from tkinter import messagebox

            if messagebox.askokcancel(
                    "진입수량 초기화", f"{block} {index + 1}세트 진입수량을 0으로?"):
                send({"cmd": "reset_fired", "block": block, "set": index},
                     f"{block} {index + 1}세트 진입수량 초기화")
        return _reset

    def ls_order_command(block: str, index: int, var: Any) -> Callable[[], None]:
        return lambda: send({"cmd": "ls_order", "block": block, "set": index,
                             "value": var.get()}, "LS주문 체크")

    for block, block_label, color in BLOCK_LABELS:
        head = tk.Frame(grid)
        head.grid(row=row_no, column=0, columnspan=len(headers), sticky="we",
                  pady=(6 if row_no else 0, 1))
        tk.Label(head, text=block_label, fg=color,
                 font=("Malgun Gothic", 9, "bold")).pack(side="left")
        # 블록별 1회주문수량 + 적용 (수시 변경)
        tk.Label(head, text=f"  1회({unit})").pack(side="left")
        ent_per = tk.Entry(head, width=5, justify="right", validate="key",
                           validatecommand=vcmd_int)
        ent_per.pack(side="left", padx=(2, 2))
        per_entries[block] = ent_per
        tk.Button(head, text="적용", command=send_per_qty(block)).pack(side="left")
        if block == "entry":
            lbl_position = tk.Label(head, text="현재진입수량 -")
            lbl_position.pack(side="right")
        row_no += 1
        for col, header in enumerate(headers):
            tk.Label(grid, text=header, fg="gray25").grid(row=row_no, column=col)
        row_no += 1
        for i, name in enumerate(("1st", "2nd", "3rd")):
            tk.Label(grid, text=name).grid(row=row_no, column=0, padx=(0, 4))
            # 기준값·목표진입량은 읽기 전용 표시 — 설정창에서만 수정
            lbl_th = tk.Label(grid, text="-", width=8, anchor="e",
                              bg="#f0f0f0", relief="solid", bd=1)
            lbl_th.grid(row=row_no, column=1, padx=2, pady=1)
            lbl_tg = tk.Label(grid, text="-", width=8, anchor="e",
                              bg="#f0f0f0", relief="solid", bd=1)
            lbl_tg.grid(row=row_no, column=2, padx=2, pady=1)
            tk.Button(grid, text="설정", width=4,
                      command=set_settings_command(block, i)).grid(
                row=row_no, column=3, padx=2)
            btn = tk.Button(grid, text="실행", width=6, command=run_command(block, i))
            btn.grid(row=row_no, column=4, padx=2)
            ls_var = tk.BooleanVar(value=True)
            tk.Checkbutton(grid, variable=ls_var,
                           command=ls_order_command(block, i, ls_var)).grid(
                row=row_no, column=5)
            displays = []
            for col in range(6, 10):
                lbl = tk.Label(grid, text="-", width=8, anchor="e",
                               bg="white", relief="solid", bd=1)
                lbl.grid(row=row_no, column=col, padx=1, pady=1)
                displays.append(lbl)
            # 진입수량 칸 더블클릭 = 초기화 (리허설 재시작용, 확인창)
            displays[0].bind("<Double-Button-1>", fired_reset(block, i))
            set_widgets[(block, i)] = {
                "th_label": lbl_th, "tg_label": lbl_tg,
                "threshold_val": "", "target_val": "",
                "button": btn, "running": False, "ls_var": ls_var,
                "displays": displays,
            }
            row_no += 1

    status = tk.Label(root, text="코어 확인 중 ...", anchor="w", relief="groove")
    status.pack(fill="x", padx=4, pady=(2, 4))

    # --- 코어 상태 폴링(뒷단 스레드) → 화면은 결과만 표시 ---
    state_box: dict[str, Any] = {"data": None, "filled": False}

    def poll_state() -> None:
        while True:
            state_box["data"] = core_request("/state")
            time.sleep(1.0)

    threading.Thread(target=poll_state, daemon=True).start()

    def my_screen(data: Any) -> dict[str, Any]:
        raw = ((data or {}).get("screens") or {}).get(screen_key)
        return raw if isinstance(raw, dict) else {}

    def fill_initial() -> None:
        """첫 폴링 결과가 도착하면 입력칸 채움 — 화면 스레드는 기다리지 않는다."""
        data = state_box["data"]
        if data is None:
            try:
                root.after(300, fill_initial)  # 아직 응답 없음 — 다음에 다시
            except tk.TclError:
                pass
            return
        state_box["filled"] = True
        screen = my_screen(data)
        rev = {v: k for k, v in UNDER_MAP.items()}
        cb_under.set(rev.get(str(screen.get("underlying")), "하이닉스"))
        for block, key in (("entry", "entry_per_qty"), ("exit", "exit_per_qty")):
            per = screen.get(key)
            if isinstance(per, int) and per > 0:
                per_entries[block].insert(0, str(per))
        for block, sets_name in (("entry", "entry_sets"), ("exit", "exit_sets")):
            raw_sets = screen.get(sets_name)
            for i, raw_set in enumerate(raw_sets if isinstance(raw_sets, list) else []):
                if not isinstance(raw_set, dict) or i >= 3:
                    continue
                w = set_widgets[(block, i)]
                w["ls_var"].set(bool(raw_set.get("ls_order", True)))
        set_status("코어 연결됨 — 마지막 입력값 복원")

    def _pct_text(value: object) -> str:
        return f"{value * 100:.3f}" if isinstance(value, int | float) else "-"

    def _px_text(value: object, decimals: int = 0) -> str:
        return f"{value:,.{decimals}f}" if isinstance(value, int | float) else "-"

    def refresh() -> None:
        try:  # 네트워크 없음 — 뒷단 스레드 결과만 표시
            data = state_box["data"]
            screen = my_screen(data)
            for block, sets_name in (("entry", "entry_sets"), ("exit", "exit_sets")):
                raw_sets = screen.get(sets_name)
                if not isinstance(raw_sets, list):
                    continue
                for i, raw_set in enumerate(raw_sets[:3]):
                    if not isinstance(raw_set, dict):
                        continue
                    w = set_widgets[(block, i)]
                    # 기준값·목표진입량 읽기 전용 표시 (설정창에서만 수정)
                    th = raw_set.get("threshold")
                    w["threshold_val"] = (fraction_to_pct_text(float(th))
                                          if isinstance(th, int | float) else "")
                    w["th_label"].config(text=w["threshold_val"] or "-")
                    tg = raw_set.get("target_qty") or 0
                    w["target_val"] = str(tg) if tg else ""
                    w["tg_label"].config(text=str(tg) if tg else "-")
                    w["displays"][0].config(text=str(raw_set.get("fired_qty", 0)))
                    done = tg > 0 and raw_set.get("fired_qty", 0) >= tg
                    w["displays"][0].config(bg="#d0f0d0" if done else "white")
            # 실시간 수치 (코어 live 스냅샷 — 판정 루프와 같은 계산)
            live = data.get("live") if isinstance(data, dict) else None
            if isinstance(live, dict):
                info_raw = (live.get("screens") or {}).get(screen_key)
                info = info_raw if isinstance(info_raw, dict) else {}
                lbl_sig_entry.config(text=f"진입 {_pct_text(info.get('entry'))}")
                lbl_sig_exit.config(text=f"청산 {_pct_text(info.get('exit'))}")
                lbl_prices.config(  # 국내 | HL | 환율 (제목 없이 값만)
                    text=f"{_px_text(info.get('kr_last'))} | "
                         f"{_px_text(info.get('hl_last'), 4)} | "
                         f"{_px_text(info.get('fx'), 2)}"
                         + ("" if live.get("connected") else "  (시세 미접속)"))
                settings_raw = screen.get("settings")
                settings = settings_raw if isinstance(settings_raw, dict) else {}
                if lbl_position is not None:
                    max_pos = settings.get("max_position")
                    lbl_position.config(
                        text=f"현재진입수량 {info.get('position', 0)}"
                             + (f" / {max_pos}" if max_pos else ""))
                hours = str(settings.get("operating_hours") or "").strip()
                lbl_hours.config(text=f"운영 {hours or operating_text(kind)}")
        finally:
            try:
                root.after(1000, refresh)
            except tk.TclError:
                pass  # 창 닫힘

    fill_initial()
    refresh()
    drain_results()
    # 콘솔로 Ctrl-C 신호가 흘러들어도 화면을 죽이지 않는다
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
