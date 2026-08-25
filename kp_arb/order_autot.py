"""자동T 주문 화면 (바로쏴 — HL·주식 동시 taker) — 코어 클라이언트.

    python -m kp_arb.order_autot     (운영은 main.bat 메뉴에서)

원본: docs/STG_1 목업 + DESIGN-auto-t.md. 화면은 입력·표시만, 판단·주문은 코어.
**화면 스레드는 네트워크 금지**(CLAUDE.md) — 명령은 뒷단 전송 스레드, 표시는 뒷단 폴링 결과만.

v1 = **레이아웃·입력 중심**(발주 상태기계는 다음 단계). 실행 토글·표시 셀은 자리만 잡아 두고
코어 명령은 종목/호가단위 등 이미 있는 것만 연결한다(나머지는 상태기계 구현 시 배선).
"""
from __future__ import annotations

import re
from functools import partial
from typing import Any

UNDERLYINGS = ("하이닉스", "삼성", "현대차")
UNDER_MAP = {"하이닉스": "sk_hynix", "삼성": "samsung", "현대차": "hyundai"}

# HL 호가단위 머지 배수(시세 모니터와 같은 표) — 원시/2·5·10·100배
AGG_CHOICES: dict[str, tuple[int | None, int | None]] = {
    "원시": (None, None), "2배": (5, 2), "5배": (5, 5),
    "10배": (4, None), "100배": (3, None),
}

# 국내 신용거래 주문유형 코드 (DESIGN-auto-t §9) — 공통설정에서 정/역 진입·청산별 선택
ORDER_TYPES: dict[str, str] = {
    "00": "보통", "003": "유통/자기융자신규", "005": "유통대주신규",
    "007": "자기대주신규", "101": "유통융자상환", "103": "자기융자상환",
    "105": "유통대주상환", "107": "자기대주상환", "180": "예탁담보대출상환(신용)",
}


def is_int_text(text: str) -> bool:
    """정수 입력칸 허용 — 빈칸 또는 숫자만."""
    return text == "" or text.isdigit()


def is_decimal_text(text: str) -> bool:
    """소수 입력칸 허용 — 부호·소수점 포함 숫자 형태(입력 중간 상태 허용)."""
    return re.fullmatch(r"-?\d*\.?\d*", text) is not None


def is_time_text(text: str) -> bool:
    """시:분:초 입력 허용 — 숫자와 콜론만(입력 중간 상태 허용)."""
    return re.fullmatch(r"[\d:]*", text) is not None


def parse_qty(text: str) -> int:
    """수량 → int. 빈칸/오타는 0."""
    try:
        return int(text.strip())
    except ValueError:
        return 0


def parse_threshold(text: str) -> float | None:
    """기준값(%) → float(% 단위 그대로). 빈칸/오타는 None."""
    try:
        return float(text.strip())
    except ValueError:
        return None


# 방향별 컬럼 라벨 (목업: 정방향 진입 -HP/+S / 청산 +HP/-S, 역방향은 반대)
_DIRECTIONS = (
    ("fwd", "정방향", "진입(융자)", "청산(상환)", "-HP/+S", "+HP/-S"),
    ("rev", "역방향", "진입(대주)", "청산(상환)", "+HP/-S", "-HP/+S"),
)
# 누적결과 3성분 라벨 (진입/청산별) — 정방향 기준. 역방향은 부호 반대 표기.
_ACC_ROWS_FWD = (("진입", ("-HP", "+S", "-환")), ("청산", ("+HP", "-S", "-환")))
_ACC_ROWS_REV = (("진입", ("+HP", "-S", "+환")), ("청산", ("-HP", "+S", "+환")))


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """자동T 화면 실행."""
    import queue
    import sys
    import threading
    import time
    import tkinter as tk
    from collections.abc import Callable
    from tkinter import ttk

    from . import ui_theme as T
    from . import win_state
    from .core_client import core_request, watch_parent_exit

    # UI만 확인하는 미리보기 — 코어 접속·부모감시 없이 레이아웃만 띄운다.
    preview = "--preview" in sys.argv
    if not preview:
        watch_parent_exit()  # 메인이 죽으면 이 창도 종료(고아 방지)
    root = tk.Tk()
    root.title("바로쏴 (자동T)")
    root.resizable(True, True)  # 창 크기 조절 가능 — 컨트롤은 좌상단 고정(안 늘어남)
    win_state.attach(root, "autoT")
    T.apply_base(root)
    root.option_add("*Font", T.FONT_BASE_LG)  # 큰 화면 — 일반주문창(order_hl)과 같은 11pt
    vcmd_int = (root.register(is_int_text), "%P")
    vcmd_dec = (root.register(is_decimal_text), "%P")
    vcmd_time = (root.register(is_time_text), "%P")

    # --- 명령 전송(뒷단) + 상태 폴링(뒷단) ---
    jobs: queue.Queue[tuple[dict[str, Any], str]] = queue.Queue()
    results: queue.Queue[tuple[str, dict[str, Any] | None]] = queue.Queue()
    state_box: dict[str, Any] = {"data": None}

    def sender() -> None:
        while True:
            payload, label = jobs.get()
            results.put((label, core_request("/command", payload, timeout=10.0)))

    def poller() -> None:
        while True:
            state_box["data"] = core_request("/state", timeout=2.0)
            time.sleep(1.0)

    if not preview:  # 미리보기는 네트워크 스레드 없이 레이아웃만
        threading.Thread(target=sender, daemon=True).start()
        threading.Thread(target=poller, daemon=True).start()

    def send(payload: dict[str, Any], label: str) -> None:
        jobs.put(({**payload, "screen": "autoT"}, label))

    # 화면 상태(로컬) — v1은 표시·입력만. 실행 토글·세트값은 여기 보관.
    common: dict[str, Any] = {
        "s_exclude": True, "windows": ["08:30:10", "08:46:20", "15:35:30", "15:46:55"],
        "buy_tick": 10, "buy_pct": 15.0, "sell_tick": 10, "sell_pct": 15.0,
        "otype": {"fwd_en": "003", "fwd_ex": "101", "rev_en": "007", "rev_ex": "105"},
    }
    sets: dict[tuple[str, int], dict[str, Any]] = {}  # (dir, i) → {target,per,delay,en,ex,...}
    for d, *_ in _DIRECTIONS:
        for i in range(3):
            sets[(d, i)] = {"target": 0, "per": 0, "delay": 0,
                            "en": None, "ex": None, "rt_manual": None, "clear_diff": False}

    # ===================== 상단 바 =====================
    top = tk.Frame(root)
    top.pack(fill="x", padx=2, pady=(2, 1))
    tk.Label(top, text="종목").pack(side="left")
    cb_under = ttk.Combobox(top, values=UNDERLYINGS, width=7, state="readonly")
    cb_under.set("하이닉스")
    cb_under.pack(side="left", padx=(2, 4))
    cb_agg = ttk.Combobox(top, values=list(AGG_CHOICES), width=5, state="readonly")
    cb_agg.set("원시")
    cb_agg.pack(side="left", padx=(0, 4))

    def apply_market() -> None:
        u = UNDER_MAP[cb_under.get()]
        send({"cmd": "select", "underlying": u}, "종목 선택")
        nsf, mant = AGG_CHOICES[cb_agg.get()]
        send({"cmd": "manual_hl_merge", "underlying": u,
              "n_sig_figs": nsf, "mantissa": mant}, "호가단위")

    tk.Button(top, text="적", width=3, command=apply_market).pack(side="left", padx=(0, 8))
    tk.Label(top, text="기준수량").pack(side="left")
    ent_refqty = tk.Entry(top, width=6, justify="right", validate="key",
                          validatecommand=vcmd_int)
    ent_refqty.insert(0, "0")
    ent_refqty.pack(side="left", padx=(2, 10))

    # 상단 모니터 수치 (정/역방향 진입·청산 est) — 기준수량>0일 때만 표시(값은 코어 연결 후)
    mon: dict[str, tk.Label] = {}
    for tag, name in (("fwd", "정방향"), ("rev", "역방향")):
        tk.Label(top, text=name).pack(side="left", padx=(4, 1))
        for side, color in (("en", T.C_BUY), ("ex", T.C_SELL)):
            lbl = tk.Label(top, text="-", bg=color, fg="white", width=6,
                           font=T.FONT_NUM_LG)
            lbl.pack(side="left", padx=1, ipady=1)
            mon[f"{tag}_{side}"] = lbl

    # ===================== 2번째 바 (공통설정 표시) =====================
    bar2 = tk.Frame(root)
    bar2.pack(fill="x", padx=2, pady=(0, 2))
    lbl_windows = tk.Label(bar2, text="", fg="gray25")
    lbl_windows.pack(side="left")
    lbl_sexcl = tk.Label(bar2, text="", fg="gray25")
    lbl_sexcl.pack(side="left", padx=(10, 0))

    def refresh_common_bar() -> None:
        w = common["windows"]
        lbl_windows.config(text=f"주문가능 {w[0]}~{w[1]} / {w[2]}~{w[3]}")
        lbl_sexcl.config(text="S 주문 제외" if common["s_exclude"] else "S 포함")

    # ===================== 방향 섹션 2개 =====================
    def build_section(dtag: str, name: str, en_col: str, ex_col: str,
                      acc_rows: tuple[Any, ...]) -> None:
        sec = tk.LabelFrame(root, text=name, fg="black")
        sec.pack(fill="x", padx=2, pady=(1, 2))
        grid = tk.Frame(sec)
        grid.grid(row=0, column=0, sticky="nw")  # 좌상단 고정 — 리사이즈해도 컨트롤 안 늘어남
        heads = ("목표수량", "1회주문", en_col, "실행", ex_col, "실행",
                 "설정", "RT선진입", "체결차", "초")
        for c, h in enumerate(heads):
            tk.Label(grid, text=h, fg="gray25").grid(
                row=0, column=c, padx=1, sticky="nsew")
        for i in range(3):
            w = sets[(dtag, i)]
            lbl_tg = tk.Label(grid, text="-", width=7, anchor="e", bg="#fffbcc",
                              relief="solid", bd=1)  # 목표수량(세트설정에서만)
            lbl_tg.grid(row=i + 1, column=0, padx=1, pady=1, sticky="nsew")
            lbl_per = tk.Label(grid, text="-", width=6, anchor="e", bg="#f0f0f0",
                               relief="solid", bd=1)  # 1회주문수량(세트설정)
            lbl_per.grid(row=i + 1, column=1, padx=1, pady=1, sticky="nsew")
            e_en = tk.Entry(grid, width=6, justify="right", validate="key",
                            validatecommand=vcmd_dec)  # 진입 기준값(인라인 수정)
            e_en.grid(row=i + 1, column=2, padx=1, pady=1, sticky="nsew")
            btn_en = tk.Button(grid, text="진입", width=4, padx=0, pady=0,
                               bd=1, highlightthickness=0)
            btn_en.grid(row=i + 1, column=3, padx=1, pady=1, sticky="nsew")
            e_ex = tk.Entry(grid, width=6, justify="right", validate="key",
                            validatecommand=vcmd_dec)  # 청산 기준값(인라인 수정)
            e_ex.grid(row=i + 1, column=4, padx=1, pady=1, sticky="nsew")
            btn_ex = tk.Button(grid, text="청산", width=4, padx=0, pady=0,
                               bd=1, highlightthickness=0)
            btn_ex.grid(row=i + 1, column=5, padx=1, pady=1, sticky="nsew")
            btn_set = tk.Button(grid, text="설정", width=4, padx=0, pady=0,
                                bd=1, highlightthickness=0,
                                command=partial(open_set_dialog, dtag, i))
            btn_set.grid(row=i + 1, column=6, padx=1, pady=1, sticky="nsew")
            lbl_rt = tk.Label(grid, text="-", width=7, anchor="e", bg="white",
                              relief="solid", bd=1)  # RT선진입(세트별)
            lbl_rt.grid(row=i + 1, column=7, padx=1, pady=1, sticky="nsew")
            lbl_diff = tk.Label(grid, text="-", width=6, anchor="e", bg="white",
                                relief="solid", bd=1)  # 체결차(세트별)
            lbl_diff.grid(row=i + 1, column=8, padx=1, pady=1, sticky="nsew")
            lbl_sec = tk.Label(grid, text="-", width=4, anchor="e", bg="#f0f0f0",
                               relief="solid", bd=1)  # 전환딜레이 초(세트설정)
            lbl_sec.grid(row=i + 1, column=9, padx=1, pady=1, sticky="nsew")
            w.update({"tg": lbl_tg, "per_lbl": lbl_per, "e_en": e_en, "e_ex": e_ex,
                      "btn_en": btn_en, "btn_ex": btn_ex, "rt": lbl_rt,
                      "diff": lbl_diff, "sec": lbl_sec,
                      "run_en": False, "run_ex": False})
            btn_en.config(command=partial(toggle_run, dtag, i, "en"))
            btn_ex.config(command=partial(toggle_run, dtag, i, "ex"))

        # 누적결과(진입/청산별) — 오른쪽. clear 버튼 + -HP/+S/-환(또는 부호반대) + Sprd
        acc = tk.Frame(sec)
        acc.grid(row=0, column=1, sticky="nw", padx=(6, 0))
        for gi, (glabel, comps) in enumerate(acc_rows):
            box = tk.LabelFrame(acc, text=glabel)
            box.grid(row=0, column=gi, padx=2)
            tk.Button(box, text="clear", width=4, padx=0, font=T.FONT_SMALL,
                      command=partial(clear_acc, dtag, glabel)).grid(
                row=0, column=0, columnspan=2)
            labels: dict[str, tk.Label] = {}
            for ri, comp in enumerate((*comps, "Sprd")):
                tk.Label(box, text=comp, fg="gray30", font=T.FONT_SMALL).grid(
                    row=ri + 1, column=0, sticky="e")
                v = tk.Label(box, text="-", width=6, anchor="e", bg="white",
                             relief="solid", bd=1)
                v.grid(row=ri + 1, column=1, padx=1, pady=1, sticky="nsew")
                labels[comp] = v
            sets[(dtag, 0)].setdefault("_acc", {})[glabel] = labels  # 방향당 1묶음(세트0에 보관)

    # --- 콜백들(v1: 로컬 동작) ---
    def toggle_run(dtag: str, i: int, side: str) -> None:
        w = sets[(dtag, i)]
        key = f"run_{side}"
        w[key] = not w[key]
        btn = w["btn_en" if side == "en" else "btn_ex"]
        on = w[key]
        btn.config(text=("정지" if on else ("진입" if side == "en" else "청산")),
                   bg="#e6b800" if on else "SystemButtonFace")
        # TODO(상태기계): send({"cmd":"autot_run", ...}) — 지금은 화면 토글만

    def clear_acc(dtag: str, group: str) -> None:
        accs = sets[(dtag, 0)].get("_acc", {}).get(group, {})
        for lbl in accs.values():
            lbl.config(text="-")
        # TODO(상태기계): send 누적 초기화 명령

    def open_set_dialog(dtag: str, i: int) -> None:
        w = sets[(dtag, i)]
        win = tk.Toplevel(root)
        win.title(f"{'정방향' if dtag == 'fwd' else '역방향'} {i + 1}세트 설정")
        win.resizable(False, False)
        win.transient(root)
        rows = [("목표수량", "target", vcmd_int), ("1회주문수량", "per", vcmd_int),
                ("전환딜레이(초)", "delay", vcmd_int),
                ("진입", "en", vcmd_dec), ("청산", "ex", vcmd_dec)]
        ents: dict[str, tk.Entry] = {}
        for r, (label, key, vc) in enumerate(rows):
            tk.Label(win, text=label, anchor="w").grid(
                row=r, column=0, sticky="w", padx=6, pady=3)
            e = tk.Entry(win, width=10, justify="right", validate="key",
                         validatecommand=vc)
            val = w.get(key)
            e.insert(0, "" if val in (None, 0) else str(val))
            e.grid(row=r, column=1, padx=6, pady=3)
            ents[key] = e
        rt_var = tk.BooleanVar(value=w.get("rt_manual") is not None)
        rt_ent = tk.Entry(win, width=10, justify="right", validate="key",
                          validatecommand=vcmd_int)
        tk.Checkbutton(win, text="RT 진입수량 수동 입력", variable=rt_var).grid(
            row=len(rows), column=0, sticky="w", padx=6)
        rt_ent.grid(row=len(rows), column=1, padx=6, pady=2)
        diff_var = tk.BooleanVar(value=False)
        tk.Checkbutton(win, text="체결차 Clear", variable=diff_var).grid(
            row=len(rows) + 1, column=0, sticky="w", padx=6, pady=(0, 4))

        def save() -> None:
            for key in ("target", "per", "delay"):
                w[key] = parse_qty(ents[key].get())
            w["en"] = parse_threshold(ents["en"].get())
            w["ex"] = parse_threshold(ents["ex"].get())
            w["rt_manual"] = parse_qty(rt_ent.get()) if rt_var.get() else None
            w["clear_diff"] = diff_var.get()
            apply_set_display(dtag, i)
            win.destroy()
            # TODO(상태기계): send 세트 설정 명령

        btns = tk.Frame(win)
        btns.grid(row=len(rows) + 2, column=0, columnspan=2, pady=(4, 6))
        tk.Button(btns, text="확인", width=8, command=save).pack(side="left", padx=4)
        tk.Button(btns, text="취소", width=8, command=win.destroy).pack(side="left", padx=4)
        _center(win)

    def apply_set_display(dtag: str, i: int) -> None:
        w = sets[(dtag, i)]
        w["tg"].config(text=str(w["target"]) if w["target"] else "-")
        w["per_lbl"].config(text=str(w["per"]) if w["per"] else "-")
        w["sec"].config(text=str(w["delay"]) if w["delay"] else "-")
        for key, ent in (("en", "e_en"), ("ex", "e_ex")):
            e = w[ent]
            e.delete(0, "end")
            if w[key] is not None:
                e.insert(0, f"{w[key]:g}")

    def open_common_dialog() -> None:
        win = tk.Toplevel(root)
        win.title("바로쏴 설정")
        win.resizable(False, False)
        win.transient(root)
        tk.Label(win, text="주문가능시간").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        wframe = tk.Frame(win)
        wframe.grid(row=0, column=1, columnspan=3, sticky="w", pady=3)
        w_ents: list[tk.Entry] = []
        for idx in range(4):
            if idx == 2:
                tk.Label(wframe, text="  /  ").pack(side="left")
            elif idx in (1, 3):
                tk.Label(wframe, text="~").pack(side="left")
            e = tk.Entry(wframe, width=9, justify="center", validate="key",
                         validatecommand=vcmd_time)
            e.insert(0, common["windows"][idx])
            e.pack(side="left", padx=1)
            w_ents.append(e)
        sx_var = tk.BooleanVar(value=common["s_exclude"])
        tk.Checkbutton(win, text="S 제외여부", variable=sx_var).grid(
            row=1, column=0, sticky="w", padx=6, pady=(4, 2))
        # 여유 표: 매수/매도 × LS(tick)/HP(%)
        tbl = tk.Frame(win)
        tbl.grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        tk.Label(tbl, text="").grid(row=0, column=0)
        tk.Label(tbl, text="LS(tick)").grid(row=0, column=1)
        tk.Label(tbl, text="HP(%)").grid(row=0, column=2)
        margin: dict[str, tk.Entry] = {}
        for r, (rk, rlabel) in enumerate((("buy", "매수"), ("sell", "매도")), start=1):
            tk.Label(tbl, text=rlabel).grid(row=r, column=0, padx=(0, 4))
            e_t = tk.Entry(tbl, width=6, justify="right", validate="key",
                           validatecommand=vcmd_int)
            e_t.insert(0, str(common[f"{rk}_tick"]))
            e_t.grid(row=r, column=1, padx=1, pady=1)
            e_p = tk.Entry(tbl, width=6, justify="right", validate="key",
                           validatecommand=vcmd_dec)
            e_p.insert(0, f"{common[f'{rk}_pct']:g}")
            e_p.grid(row=r, column=2, padx=1, pady=1)
            margin[f"{rk}_tick"] = e_t
            margin[f"{rk}_pct"] = e_p
        # 주문유형: 정/역방향 진입·청산
        otype_frame = tk.Frame(win)
        otype_frame.grid(row=2, column=2, columnspan=2, sticky="w", padx=6)
        type_names = list(ORDER_TYPES.values())
        name_to_code = {v: k for k, v in ORDER_TYPES.items()}
        otype_cbs: dict[str, ttk.Combobox] = {}
        for r, (ok, olabel) in enumerate((("fwd_en", "정방향 진입"), ("fwd_ex", "정방향 청산"),
                                          ("rev_en", "역방향 진입"), ("rev_ex", "역방향 청산"))):
            tk.Label(otype_frame, text=olabel).grid(row=r, column=0, sticky="w")
            cb = ttk.Combobox(otype_frame, values=type_names, width=16, state="readonly")
            cb.set(ORDER_TYPES.get(common["otype"][ok], "보통"))
            cb.grid(row=r, column=1, padx=2, pady=1)
            otype_cbs[ok] = cb

        def save() -> None:
            common["windows"] = [e.get().strip() for e in w_ents]
            common["s_exclude"] = sx_var.get()
            for key, e in margin.items():
                common[key] = (parse_qty(e.get()) if key.endswith("tick")
                               else (parse_threshold(e.get()) or 0.0))
            for ok, cb in otype_cbs.items():
                common["otype"][ok] = name_to_code.get(cb.get(), "00")
            refresh_common_bar()
            win.destroy()
            # TODO(상태기계): send 공통설정 명령

        btns = tk.Frame(win)
        btns.grid(row=3, column=0, columnspan=4, pady=(6, 6))
        tk.Button(btns, text="확인", width=8, command=save).pack(side="left", padx=4)
        tk.Button(btns, text="취소", width=8, command=win.destroy).pack(side="left", padx=4)
        _center(win)

    def _center(win: tk.Toplevel) -> None:
        win.update_idletasks()
        x = root.winfo_x() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_y() + (root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        win.focus_set()

    tk.Button(bar2, text="설정", command=open_common_dialog).pack(side="right")

    for dtag, name, en_lbl, ex_lbl, en_col, ex_col in _DIRECTIONS:
        acc_rows = _ACC_ROWS_FWD if dtag == "fwd" else _ACC_ROWS_REV
        build_section(dtag, f"{name}  ({en_lbl} / {ex_lbl})", en_col, ex_col, acc_rows)

    status = tk.Label(root, anchor="w", relief="groove",
                      text="UI 미리보기 — 코어 미연결" if preview else "코어 확인 중 ...")
    status.pack(fill="x", padx=4, pady=(2, 4))

    refresh_common_bar()

    # --- 표시 갱신 루프(뒷단 폴링 결과만 읽음) ---
    def drain() -> None:
        try:
            while True:
                label, result = results.get_nowait()
                if result is None:
                    status.config(text=f"{label} 실패 — 코어 미접속")
                elif not result.get("ok"):
                    status.config(text=f"{label} 거부 — {'; '.join(result.get('errors', []))}")
                else:
                    status.config(text=f"{label}됨")
        except queue.Empty:
            pass
        _reschedule(drain, 200)

    preview_mon = {"fwd_en": "-0.82", "fwd_ex": "-0.52", "rev_en": "0.12", "rev_ex": "0.23"}

    def refresh() -> None:
        try:
            if preview:  # 정적 — 목업 대조용 샘플 수치, 갱신 루프 없음
                for key, lbl in mon.items():
                    lbl.config(text=preview_mon.get(key, "-"))
                return
            connected = isinstance(state_box["data"], dict)
            show = parse_qty(ent_refqty.get()) > 0  # 기준수량>0일 때만 모니터 수치
            for lbl in mon.values():
                lbl.config(text="0.00" if (connected and show) else "-")
            if not connected:
                status.config(text="코어 미접속 — 메인에서 코어 시작")
        except tk.TclError:
            return
        _reschedule(refresh, 1000)

    def _reschedule(fn: Callable[[], None], ms: int) -> None:
        try:
            root.after(ms, fn)
        except tk.TclError:
            pass

    drain()
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
