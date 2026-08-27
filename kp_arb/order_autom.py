"""자동M 주문 화면 (체결쏴 — HL·주식선물 선주문 maker→후주문 taker) — 코어 클라이언트.

    python -m kp_arb.order_autom     (운영은 main.bat 메뉴에서)

원본: docs/STG_2 목업(layout_1·체결쏴 설정·세트설정) + DESIGN-auto-m.md.
화면 뼈대는 자동T(order_autot)와 거의 같고, 아래 3가지만 다르다:
  1) 진입 기준 컬럼이 2개(SF·S) — 청산은 SF 1개.
  2) 상단 모니터가 3칸(진입 SF / 진입 S / 청산 SF).
  3) 설정창(공통=체결쏴 설정 / 세트)이 자동M 전용.
**화면 스레드는 네트워크 금지**(CLAUDE.md). v1 = 레이아웃·입력 중심(발주 상태기계는 다음 단계).
공용 순수 헬퍼·상수는 order_autot에서 재사용(단일 출처).
"""
from __future__ import annotations

from functools import partial
from typing import Any

from .order_autot import (
    AGG_CHOICES,
    UNDER_MAP,
    UNDERLYINGS,
    is_decimal_text,
    is_int_text,
    is_time_text,
    parse_qty,
    parse_threshold,
)

# 방향별 컬럼 라벨 (목업 STG_2) — (태그, 이름, 진입SF, 진입S, 청산SF)
# 정방향 진입 -HP/+SF·-HP/+S, 청산 +HP/-SF / 역방향은 부호 반대
_DIRECTIONS = (
    ("fwd", "정방향", "-HP/+SF", "-HP/+S", "+HP/-SF"),
    ("rev", "역방향", "+HP/-SF", "+HP/-S", "-HP/+SF"),
)
# 누적결과 3성분 라벨 (진입/청산별). 환 부호 = HP 부호 (목업 STG_2 대조).
_ACC_ROWS_FWD = (("진입", ("-HP", "+S", "-환")), ("청산", ("+HP", "-S", "+환")))
_ACC_ROWS_REV = (("진입", ("+HP", "-S", "+환")), ("청산", ("-HP", "+S", "-환")))

# 선주문 호가단위 설정 종목 순서 (목업 라벨 → underlying 코드)
_PRE_TICK_ROWS = (("하이닉스", "sk_hynix"), ("삼성전자", "samsung"), ("현대차", "hyundai"))
# 상대호가 콤보 — 선주문 진입범위 §6.3: 매수는 상대호가−1틱, 매도는 +1틱
_REL_CHOICES_BUY = [f"상대{n}호가 - 1틱" for n in range(1, 6)]
_REL_CHOICES_SELL = [f"상대{n}호가 + 1틱" for n in range(1, 6)]


def check_risk(dtag: str, en_sf: float | None, en_s: float | None,
               ex_sf: float | None, risk_en: float, risk_ex: float,
               risk_gap: float) -> list[str]:
    """자동M 리스크방지 입력 검증 (DESIGN-auto-m §10). 위반 메시지 목록(빈 목록=통과).

    None(미입력) 값은 건너뛴다. gap 검증은 진입SF·청산SF 둘 다 있을 때만.
    정방향: 진입 > 기준, 청산 < 기준, 진입SF−청산 > gap.
    역방향: 진입 < 기준, 청산 > 기준, 청산−진입SF > gap.
    """
    errs: list[str] = []
    if dtag == "fwd":
        for label, v in (("진입SF", en_sf), ("진입S", en_s)):
            if v is not None and v <= risk_en:
                errs.append(f"정방향 {label}는 {risk_en:g} 초과여야 합니다")
        if ex_sf is not None and ex_sf >= risk_ex:
            errs.append(f"정방향 청산은 {risk_ex:g} 미만이어야 합니다")
        if en_sf is not None and ex_sf is not None and en_sf - ex_sf <= risk_gap:
            errs.append(f"정방향 진입SF−청산은 {risk_gap:g} 초과여야 합니다")
    else:
        for label, v in (("진입SF", en_sf), ("진입S", en_s)):
            if v is not None and v >= risk_en:
                errs.append(f"역방향 {label}는 {risk_en:g} 미만이어야 합니다")
        if ex_sf is not None and ex_sf <= risk_ex:
            errs.append(f"역방향 청산은 {risk_ex:g} 초과여야 합니다")
        if en_sf is not None and ex_sf is not None and ex_sf - en_sf <= risk_gap:
            errs.append(f"역방향 청산−진입SF는 {risk_gap:g} 초과여야 합니다")
    return errs


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """자동M 화면 실행."""
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

    preview = "--preview" in sys.argv  # UI만 확인 — 코어 접속·부모감시 없이 레이아웃만
    if not preview:
        watch_parent_exit()
    root = tk.Tk()
    root.title("체결쏴 (자동M)")
    root.resizable(True, True)
    win_state.attach(root, "autoM")
    T.apply_base(root)
    root.option_add("*Font", T.FONT_BASE_LG)  # 큰 화면 — 자동T와 같은 11pt
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

    if not preview:
        threading.Thread(target=sender, daemon=True).start()
        threading.Thread(target=poller, daemon=True).start()

    def send(payload: dict[str, Any], label: str) -> None:
        jobs.put(({**payload, "screen": "autoM"}, label))

    # 화면 상태(로컬) — v1은 표시·입력만. 공통설정(체결쏴 설정) 기본값은 목업 기준.
    common: dict[str, Any] = {
        "windows": ["08:30:10", "08:46:20", "15:35:30", "15:46:55"],
        "pre_tick": {"sk_hynix": 3000, "samsung": 500, "hyundai": 1000},
        "pre_delay": 1000, "pre_range": 0.4, "rel_buy": 1, "rel_sell": 1,
        "risk": {"fwd_en": 0.0, "fwd_ex": 0.5, "fwd_gap": 0.1,
                 "rev_en": 0.5, "rev_ex": 0.0, "rev_gap": 0.1},
    }
    # 세트: 진입 SF·S 2개 + 청산 SF 1개 (자동T는 진입/청산 1개씩)
    sets: dict[tuple[str, int], dict[str, Any]] = {}
    for d, *_ in _DIRECTIONS:
        for i in range(3):
            sets[(d, i)] = {"target": 0, "per": 0, "delay": 0, "en_sf": None,
                            "en_s": None, "ex_sf": None, "rt_manual": None,
                            "clear_diff": False}

    # ===================== 상단 바 =====================
    top = tk.Frame(root)
    top.pack(fill="x", padx=4, pady=(2, 2))
    tk.Label(top, text="종목").pack(side="left")
    cb_under = ttk.Combobox(top, values=UNDERLYINGS, width=7, state="readonly")
    cb_under.set("하이닉스")
    cb_under.pack(side="left", padx=(2, 4))
    # TODO(운영 배선): 호가단위를 코어 merge_ticks(실제 틱 숫자)로 채운다(지금은 라벨).
    cb_agg = ttk.Combobox(top, values=list(AGG_CHOICES), width=5, state="readonly")
    cb_agg.set("원시")
    cb_agg.pack(side="left", padx=(0, 4))

    def apply_market() -> None:
        u = UNDER_MAP[cb_under.get()]
        send({"cmd": "select", "underlying": u}, "종목 선택")
        nsf, mant = AGG_CHOICES[cb_agg.get()]
        send({"cmd": "manual_hl_merge", "underlying": u,
              "n_sig_figs": nsf, "mantissa": mant}, "호가단위")

    ttk.Style().configure("Ap.TButton", padding=(6, 2))  # 콤보 높이(≈26)에 맞춤
    ttk.Button(top, text="적", width=3, style="Ap.TButton",
               command=apply_market).pack(side="left", padx=(0, 4))
    ent_refqty = tk.Entry(top, width=6, justify="right", validate="key",
                          validatecommand=vcmd_int, font=T.FONT_NUM_LG)
    ent_refqty.insert(0, "0")
    ent_refqty.pack(side="left", padx=(0, 6))

    # 오른쪽 끝 = 설정, 그 왼쪽 = 주문가능시간 표시 (모니터 수치는 방향 제목 옆으로 이동)
    tk.Button(top, text="설정", command=lambda: open_common_dialog()).pack(side="right")
    mon: dict[str, tk.Label] = {}  # 방향 제목 옆 모니터 라벨 — build_section에서 채움
    lbl_windows = tk.Label(top, text="", fg="gray25")
    lbl_windows.pack(side="right", padx=(0, 8))

    def refresh_windows_bar() -> None:
        w = common["windows"]
        lbl_windows.config(text=f"주문가능  {w[0]}~{w[1]}  /  {w[2]}~{w[3]}")

    # ===================== 방향 섹션 2개 =====================
    def build_section(grid: Any, rbase: int, dtag: str, name: str, en_sf: str,
                      en_s: str, ex_sf: str, acc_rows: tuple[Any, ...]) -> None:
        # 두 방향을 공유 그리드에 rbase 오프셋으로 → 컬럼 공유 = 완벽 정렬.
        heads = ("목표수량", "1회주문", en_sf, en_s, "실행", ex_sf, "실행",
                 "설정", "RT선진입", "체결차", "초")
        nset = len(heads)  # 11 (자동T 10 + 진입 S 한 칸)

        tk.Label(grid, text=name, font=T.FONT_NUM_LG).grid(
            row=rbase, column=0, columnspan=2, sticky="w", pady=(0, 2))
        # 모니터 수치를 제목 옆, 각 기준값 컬럼(진입SF=2·진입S=3·청산SF=5) 위치에 맞춰 배치
        for mcol, skey, color in ((2, "en_sf", T.C_BUY), (3, "en_s", T.C_BUY),
                                  (5, "ex_sf", T.C_SELL)):
            mlbl = tk.Label(grid, text="-", bg=color, fg="white", anchor="center",
                            font=T.FONT_NUM_LG)
            mlbl.grid(row=rbase, column=mcol, padx=1, pady=(0, 2), sticky="nsew")
            mon[f"{dtag}_{skey}"] = mlbl
        ttk.Separator(grid, orient="vertical").grid(
            row=rbase, column=nset, rowspan=5, sticky="ns", padx=3)
        acc_cols: dict[str, tuple[int, int, tuple[str, ...]]] = {}
        cum_labels: dict[str, tk.Label] = {}
        for gi, (glabel, comps) in enumerate(acc_rows):
            lcol, vcol = nset + 1 + gi * 2, nset + 2 + gi * 2
            acc_cols[glabel] = (lcol, vcol, comps)
            tk.Button(grid, text=glabel, padx=0, pady=0, bd=1, highlightthickness=0,
                      font=T.FONT_LABEL, command=partial(clear_acc, dtag, glabel)).grid(
                row=rbase, column=lcol, padx=(2, 0), pady=1, sticky="nsew")
            lbl_cum = tk.Label(grid, text="-", width=7, anchor="e", bg="white",
                               relief="solid", bd=1, font=T.FONT_BASE_LG)
            lbl_cum.grid(row=rbase, column=vcol, padx=1, pady=1, sticky="nsew")
            cum_labels[glabel] = lbl_cum

        for c, h in enumerate(heads):  # 컬럼 헤더 (9pt) — 매매결과 Sprd와 정렬
            tk.Label(grid, text=h, fg="gray25", font=T.FONT_LABEL).grid(
                row=rbase + 1, column=c, padx=1, sticky="nsew")

        for i in range(3):  # 세트 3줄
            r = rbase + i + 2
            w = sets[(dtag, i)]
            lbl_tg = tk.Label(grid, text="-", width=6, anchor="e", bg="#fffbcc",
                              relief="solid", bd=1, font=T.FONT_BASE_LG)  # 목표수량
            lbl_tg.grid(row=r, column=0, padx=1, pady=1, sticky="nsew")
            lbl_per = tk.Label(grid, text="-", width=5, anchor="e", bg="#f0f0f0",
                               relief="solid", bd=1, font=T.FONT_BASE_LG)  # 1회주문
            lbl_per.grid(row=r, column=1, padx=1, pady=1, sticky="nsew")
            e_en_sf = tk.Entry(grid, width=5, justify="right", validate="key",
                               validatecommand=vcmd_dec, font=T.FONT_NUM_LG)  # 진입 SF
            e_en_sf.grid(row=r, column=2, padx=1, pady=1, sticky="nsew")
            e_en_s = tk.Entry(grid, width=5, justify="right", validate="key",
                              validatecommand=vcmd_dec, font=T.FONT_NUM_LG)  # 진입 S
            e_en_s.grid(row=r, column=3, padx=1, pady=1, sticky="nsew")
            btn_en = tk.Button(grid, text="진입", width=3, padx=0, pady=0,
                               bd=1, highlightthickness=0)
            btn_en.grid(row=r, column=4, padx=1, pady=1, sticky="nsew")
            e_ex_sf = tk.Entry(grid, width=5, justify="right", validate="key",
                               validatecommand=vcmd_dec, font=T.FONT_NUM_LG)  # 청산 SF
            e_ex_sf.grid(row=r, column=5, padx=1, pady=1, sticky="nsew")
            btn_ex = tk.Button(grid, text="청산", width=3, padx=0, pady=0,
                               bd=1, highlightthickness=0)
            btn_ex.grid(row=r, column=6, padx=1, pady=1, sticky="nsew")
            btn_set = tk.Button(grid, text="설정", width=3, padx=0, pady=0,
                                bd=1, highlightthickness=0,
                                command=partial(open_set_dialog, dtag, i))
            btn_set.grid(row=r, column=7, padx=1, pady=1, sticky="nsew")
            lbl_rt = tk.Label(grid, text="-", width=7, anchor="e", bg="white",
                              relief="solid", bd=1, font=T.FONT_BASE_LG)  # RT선진입
            lbl_rt.grid(row=r, column=8, padx=1, pady=1, sticky="nsew")
            lbl_diff = tk.Label(grid, text="-", width=6, anchor="e", bg="white",
                                relief="solid", bd=1, font=T.FONT_BASE_LG)  # 체결차
            lbl_diff.grid(row=r, column=9, padx=1, pady=1, sticky="nsew")
            lbl_sec = tk.Label(grid, text="-", width=3, anchor="e", bg="#f0f0f0",
                               relief="solid", bd=1, font=T.FONT_BASE_LG)  # 전환딜레이 초
            lbl_sec.grid(row=r, column=10, padx=1, pady=1, sticky="nsew")
            w.update({"tg": lbl_tg, "per_lbl": lbl_per, "e_en_sf": e_en_sf,
                      "e_en_s": e_en_s, "e_ex_sf": e_ex_sf, "btn_en": btn_en,
                      "btn_ex": btn_ex, "rt": lbl_rt, "diff": lbl_diff,
                      "sec": lbl_sec, "run_en": False, "run_ex": False})
            btn_en.config(command=partial(toggle_run, dtag, i, "en"))
            btn_ex.config(command=partial(toggle_run, dtag, i, "ex"))

        # 매매결과 값 — Sprd=컬럼헤더 줄, -HP/+S/-환=세트1~3 줄. 탑·끝 라인 정렬.
        for glabel, (lcol, vcol, comps) in acc_cols.items():
            labels: dict[str, tk.Label] = {"누적": cum_labels[glabel]}
            for ri, comp in enumerate(("Sprd", *comps)):
                tk.Label(grid, text=comp, fg="gray30", font=T.FONT_LABEL).grid(
                    row=rbase + ri + 1, column=lcol, padx=(2, 0), sticky="e")
                v = tk.Label(grid, text="-", width=7, anchor="e", relief="solid",
                             bd=1, font=T.FONT_BASE_LG,
                             bg="#fffbcc" if comp == "Sprd" else "white")
                v.grid(row=rbase + ri + 1, column=vcol, padx=1, pady=1, sticky="nsew")
                labels[comp] = v
            sets[(dtag, 0)].setdefault("_acc", {})[glabel] = labels

    # --- 콜백들(v1: 로컬 동작) ---
    def toggle_run(dtag: str, i: int, side: str) -> None:
        w = sets[(dtag, i)]
        key = f"run_{side}"
        turning_on = not w[key]
        if turning_on:  # 실행 시작 전 필수 입력 + 리스크방지 검증(인라인 현재값 확정)
            en_sf = parse_threshold(w["e_en_sf"].get())
            en_s = parse_threshold(w["e_en_s"].get())
            ex_sf = parse_threshold(w["e_ex_sf"].get())
            errs: list[str] = []
            if side == "en":  # 진입 실행 — 목표·1회주문·진입SF·진입S 필수
                if w["target"] <= 0:
                    errs.append("목표수량을 입력하세요")
                if w["per"] <= 0:
                    errs.append("1회주문수량을 입력하세요")
                if en_sf is None:
                    errs.append("진입SF를 입력하세요")
                if en_s is None:
                    errs.append("진입S를 입력하세요")
            else:  # 청산 실행 — 1회주문·청산 필수
                if w["per"] <= 0:
                    errs.append("1회주문수량을 입력하세요")
                if ex_sf is None:
                    errs.append("청산을 입력하세요")
            errs += check_risk(dtag, en_sf, en_s, ex_sf, *_risk_of(dtag))
            if errs:  # 필수 미입력·위반 — 경고, 실행 시작 안 함(버튼 상태 유지)
                warn_center("\n".join(errs))
                return
            w["en_sf"], w["en_s"], w["ex_sf"] = en_sf, en_s, ex_sf
        w[key] = on = turning_on
        btn = w["btn_en" if side == "en" else "btn_ex"]
        if on:  # 진입중=빨강, 청산중=파랑, 흰 글씨 볼드
            btn.config(bg=T.C_BUY if side == "en" else T.C_SELL, fg="white",
                       font=T.FONT_NUM_LG)
        else:
            btn.config(bg="SystemButtonFace", fg="black", font=T.FONT_BASE_LG)
        # 실행 중엔 해당 기준값 칸 잠금(진입=SF·S 두 칸 / 청산=SF 한 칸) — DESIGN-auto-m
        st = "disabled" if on else "normal"
        for ent in (("e_en_sf", "e_en_s") if side == "en" else ("e_ex_sf",)):
            w[ent].config(state=st)
        # TODO(상태기계): send({"cmd":"autom_run", ...})

    def clear_acc(dtag: str, group: str) -> None:
        accs = sets[(dtag, 0)].get("_acc", {}).get(group, {})
        for lbl in accs.values():
            lbl.config(text="-")
        # TODO(상태기계): send 누적 초기화 명령

    def _risk_of(dtag: str) -> tuple[float, float, float]:
        r = common["risk"]
        return r[f"{dtag}_en"], r[f"{dtag}_ex"], r[f"{dtag}_gap"]

    def warn_center(msg: str) -> None:
        # 리스크방지 경고 — 메인 창 중앙에 모달로(닫을 때까지 대기).
        win = tk.Toplevel(root)
        win.title("리스크방지")
        win.resizable(False, False)
        win.transient(root)
        tk.Label(win, text=msg, justify="left", padx=16, pady=12).pack()
        tk.Button(win, text="확인", width=10, command=win.destroy).pack(pady=(0, 10))
        _center(win)
        win.wait_window()

    def open_set_dialog(dtag: str, i: int) -> None:
        w = sets[(dtag, i)]
        win = tk.Toplevel(root)
        win.title(f"{'정방향' if dtag == 'fwd' else '역방향'} {i + 1}세트 설정")
        win.resizable(False, False)
        win.transient(root)
        # 진입은 SF·S 두 칸, 청산은 SF 한 칸 (자동T 대비 한 줄 늘어남)
        rows = [("목표수량", "target", vcmd_int), ("1회주문수량", "per", vcmd_int),
                ("전환딜레이(초)", "delay", vcmd_int), ("진입SF", "en_sf", vcmd_dec),
                ("진입S", "en_s", vcmd_dec), ("청산", "ex_sf", vcmd_dec)]
        ents: dict[str, tk.Entry] = {}
        inline_map = {"en_sf": "e_en_sf", "en_s": "e_en_s", "ex_sf": "e_ex_sf"}
        for r, (label, key, vc) in enumerate(rows):
            tk.Label(win, text=label, anchor="w").grid(
                row=r, column=0, sticky="w", padx=6, pady=3)
            e = tk.Entry(win, width=10, justify="right", validate="key",
                         validatecommand=vc)
            if key in inline_map:  # 진입SF·진입S·청산 = 화면 인라인 현재값
                e.insert(0, w[inline_map[key]].get())
            else:  # 목표수량·1회주문·전환딜레이 = 세트 상태값
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
            en_sf = parse_threshold(ents["en_sf"].get())
            en_s = parse_threshold(ents["en_s"].get())
            ex_sf = parse_threshold(ents["ex_sf"].get())
            target = parse_qty(ents["target"].get())
            per = parse_qty(ents["per"].get())
            errs: list[str] = []  # 필수 입력 검사(목표·1회주문·진입SF·진입S·청산)
            if target <= 0:
                errs.append("목표수량을 입력하세요")
            if per <= 0:
                errs.append("1회주문수량을 입력하세요")
            if en_sf is None:
                errs.append("진입SF를 입력하세요")
            if en_s is None:
                errs.append("진입S를 입력하세요")
            if ex_sf is None:
                errs.append("청산을 입력하세요")
            errs += check_risk(dtag, en_sf, en_s, ex_sf, *_risk_of(dtag))
            if errs:  # 필수 미입력·위반 — 경고만, 저장·닫기 안 함
                warn_center("\n".join(errs))
                return
            w["target"], w["per"] = target, per
            w["delay"] = parse_qty(ents["delay"].get())
            w["en_sf"], w["en_s"], w["ex_sf"] = en_sf, en_s, ex_sf
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
        # 실행 중이면 잠긴 칸도 잠시 열어 값 반영 후 다시 잠금(세트설정은 실행 중에도 가능).
        for key, entkey, running in (("en_sf", "e_en_sf", w["run_en"]),
                                     ("en_s", "e_en_s", w["run_en"]),
                                     ("ex_sf", "e_ex_sf", w["run_ex"])):
            e = w[entkey]
            e.config(state="normal")
            e.delete(0, "end")
            if w[key] is not None:
                e.insert(0, f"{w[key]:g}")
            if running:
                e.config(state="disabled")

    def open_common_dialog() -> None:
        win = tk.Toplevel(root)
        win.title("체결쏴 설정")
        win.resizable(False, False)
        win.transient(root)
        # 주문가능시간 2구간(초 단위)
        tk.Label(win, text="주문가능시간").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        wframe = tk.Frame(win)
        wframe.grid(row=0, column=1, columnspan=3, sticky="w", pady=4)
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

        # 선주문 호가단위 (종목별) — 왼쪽 블록
        pt = tk.LabelFrame(win, text="선주문 호가단위")
        pt.grid(row=1, column=0, columnspan=2, sticky="nw", padx=6, pady=4)
        pt_ents: dict[str, tk.Entry] = {}
        for r, (plabel, pcode) in enumerate(_PRE_TICK_ROWS):
            tk.Label(pt, text=plabel, anchor="w").grid(
                row=r, column=0, sticky="w", padx=4, pady=2)
            e = tk.Entry(pt, width=9, justify="right", validate="key",
                         validatecommand=vcmd_int)
            e.insert(0, str(common["pre_tick"][pcode]))
            e.grid(row=r, column=1, padx=4, pady=2)
            pt_ents[pcode] = e

        # 선주문 딜레이·범위·상대호가 — 오른쪽 블록
        pr = tk.Frame(win)
        pr.grid(row=1, column=2, columnspan=2, sticky="nw", padx=6, pady=4)
        tk.Label(pr, text="선주문 딜레이(ms)").grid(row=0, column=0, sticky="e", pady=2)
        e_delay = tk.Entry(pr, width=7, justify="right", validate="key",
                           validatecommand=vcmd_int)
        e_delay.insert(0, str(common["pre_delay"]))
        e_delay.grid(row=0, column=1, padx=4, pady=2)
        tk.Label(pr, text="선주문 범위(%)").grid(row=1, column=0, sticky="e", pady=2)
        e_range = tk.Entry(pr, width=7, justify="right", validate="key",
                           validatecommand=vcmd_dec)
        e_range.insert(0, f"{common['pre_range']:g}")
        e_range.grid(row=1, column=1, padx=4, pady=2)
        rel_cbs: dict[str, ttk.Combobox] = {}
        for r, (rk, rlabel) in enumerate((("rel_buy", "매수"), ("rel_sell", "매도")), start=2):
            choices = _REL_CHOICES_BUY if rk == "rel_buy" else _REL_CHOICES_SELL
            tk.Label(pr, text=rlabel).grid(row=r, column=0, sticky="e", pady=2)
            cb = ttk.Combobox(pr, values=choices, width=15, state="readonly")
            cb.set(choices[common[rk] - 1])
            cb.grid(row=r, column=1, padx=4, pady=2)
            rel_cbs[rk] = cb

        # 리스크방지 — 정/역방향 각 3칸
        risk_ents: dict[str, tk.Entry] = {}

        def _risk_block(col: int, title: str, pfx: str,
                        specs: tuple[tuple[str, str, str], ...]) -> None:
            fr = tk.LabelFrame(win, text=title)
            fr.grid(row=2, column=col, columnspan=2, sticky="nw", padx=6, pady=4)
            for r, (rlabel, op, rk) in enumerate(specs):
                tk.Label(fr, text=f"{rlabel} {op}").grid(
                    row=r, column=0, sticky="e", padx=4, pady=2)
                e = tk.Entry(fr, width=7, justify="right", validate="key",
                             validatecommand=vcmd_dec)
                e.insert(0, f"{common['risk'][pfx + rk]:g}")
                e.grid(row=r, column=1, padx=4, pady=2)
                risk_ents[pfx + rk] = e

        _risk_block(0, "정방향 리스크방지", "fwd_",
                    (("진입", ">", "en"), ("청산", "<", "ex"), ("진입-청산", ">", "gap")))
        _risk_block(2, "역방향 리스크방지", "rev_",
                    (("진입", "<", "en"), ("청산", ">", "ex"), ("청산-진입", ">", "gap")))

        def save() -> None:
            common["windows"] = [e.get().strip() for e in w_ents]
            for pcode, e in pt_ents.items():
                common["pre_tick"][pcode] = parse_qty(e.get())
            common["pre_delay"] = parse_qty(e_delay.get())
            common["pre_range"] = parse_threshold(e_range.get()) or 0.0
            for rk, cb in rel_cbs.items():
                choices = _REL_CHOICES_BUY if rk == "rel_buy" else _REL_CHOICES_SELL
                common[rk] = choices.index(cb.get()) + 1
            for rk, e in risk_ents.items():
                common["risk"][rk] = parse_threshold(e.get()) or 0.0
            refresh_windows_bar()
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

    board = tk.Frame(root)  # 두 방향 공유 그리드
    board.pack(fill="x", padx=4, pady=(1, 2), anchor="w")
    for di, (dtag, name, en_sf, en_s, ex_sf) in enumerate(_DIRECTIONS):
        rbase = di * 6  # 정방향 0~4, (5=구분선), 역방향 6~10
        if di > 0:
            ttk.Separator(board, orient="horizontal").grid(
                row=rbase - 1, column=0, columnspan=16, sticky="ew", pady=3)
        acc_rows = _ACC_ROWS_FWD if dtag == "fwd" else _ACC_ROWS_REV
        build_section(board, rbase, dtag, name, en_sf, en_s, ex_sf, acc_rows)

    if preview:  # 최대 폭 샘플로 칸 폭 테스트 (세트설정 목업: 진입SF 0.5·진입S 0.5·청산 -0.1)
        acc_sample = {"누적": "99,999", "Sprd": "-0.825", "-HP": "9,999",
                      "+HP": "9,999", "+S": "99,999", "-S": "99,999",
                      "-환": "1,418.5", "+환": "1,418.5"}
        for (dtag, _i), w in sets.items():
            w["target"], w["per"], w["delay"] = 10000, 100, 30  # 상태도 채워 설정창과 일관
            w["tg"].config(text="10,000")
            w["per_lbl"].config(text="100")
            w["rt"].config(text="9,999")
            w["diff"].config(text="-999")
            w["sec"].config(text="30")
            # 방향별 리스크방지에 맞는 샘플(정=양수 진입 / 역=음수 진입) — 탭 이동 거짓경고 방지
            en, ex = ("0.50", "-0.10") if dtag == "fwd" else ("-1.50", "0.90")
            w["e_en_sf"].insert(0, en)
            w["e_en_s"].insert(0, en)
            w["e_ex_sf"].insert(0, ex)
        for d in ("fwd", "rev"):
            for labels in sets[(d, 0)].get("_acc", {}).values():
                for comp, lbl in labels.items():
                    lbl.config(text=acc_sample.get(comp, "-"))

    status = tk.Label(root, anchor="w", relief="groove",
                      text="UI 미리보기 — 코어 미연결" if preview else "코어 확인 중 ...")
    status.pack(fill="x", padx=4, pady=(2, 4))
    refresh_windows_bar()  # 상단 주문가능시간 표시 초기화

    # --- 화면 저장/복원 (win_state.autoM — 2초 자동저장, order_hl과 동일 방식) ---
    def _collect_fields() -> dict[str, Any]:
        sets_data: dict[str, Any] = {}
        for (dtag, i), w in sets.items():
            sets_data[f"{dtag}{i}"] = {
                "target": w["target"], "per": w["per"], "delay": w["delay"],
                "rt_manual": w["rt_manual"], "clear_diff": w["clear_diff"],
                "en_sf": w["e_en_sf"].get(), "en_s": w["e_en_s"].get(),
                "ex_sf": w["e_ex_sf"].get()}
        return {
            "under": cb_under.get(), "agg": cb_agg.get(), "refqty": ent_refqty.get(),
            "common": {"windows": list(common["windows"]),
                       "pre_tick": dict(common["pre_tick"]),
                       "pre_delay": common["pre_delay"], "pre_range": common["pre_range"],
                       "rel_buy": common["rel_buy"], "rel_sell": common["rel_sell"],
                       "risk": dict(common["risk"])},
            "sets": sets_data}

    def _restore_saved() -> None:
        saved = win_state.saved_fields("autoM")
        if not saved:
            return
        if saved.get("under") in UNDERLYINGS:
            cb_under.set(saved["under"])
        if saved.get("agg") in AGG_CHOICES:
            cb_agg.set(saved["agg"])
        if isinstance(saved.get("refqty"), str):
            ent_refqty.delete(0, "end")
            ent_refqty.insert(0, saved["refqty"])
        sc = saved.get("common")
        if isinstance(sc, dict):
            if isinstance(sc.get("windows"), list) and len(sc["windows"]) == 4:
                common["windows"] = [str(x) for x in sc["windows"]]
            if isinstance(sc.get("pre_tick"), dict):
                for k in common["pre_tick"]:
                    if isinstance(sc["pre_tick"].get(k), int):
                        common["pre_tick"][k] = sc["pre_tick"][k]
            for k in ("pre_delay", "rel_buy", "rel_sell"):
                if isinstance(sc.get(k), int):
                    common[k] = sc[k]
            if isinstance(sc.get("pre_range"), int | float):
                common["pre_range"] = float(sc["pre_range"])
            if isinstance(sc.get("risk"), dict):
                for k in common["risk"]:
                    if isinstance(sc["risk"].get(k), int | float):
                        common["risk"][k] = float(sc["risk"][k])
        ss = saved.get("sets")
        if isinstance(ss, dict):
            for (dtag, i), w in sets.items():
                d = ss.get(f"{dtag}{i}")
                if not isinstance(d, dict):
                    continue
                w["target"] = d["target"] if isinstance(d.get("target"), int) else 0
                w["per"] = d["per"] if isinstance(d.get("per"), int) else 0
                w["delay"] = d["delay"] if isinstance(d.get("delay"), int) else 0
                w["rt_manual"] = (d["rt_manual"] if isinstance(d.get("rt_manual"), int)
                                  else None)
                w["clear_diff"] = bool(d.get("clear_diff", False))
                for fld in ("en_sf", "en_s", "ex_sf"):
                    txt = d.get(fld)
                    w[fld] = parse_threshold(txt) if isinstance(txt, str) else None
                apply_set_display(dtag, i)
        refresh_windows_bar()

    if not preview:  # 미리보기는 저장/복원 제외(샘플과 실제 저장 분리)
        _restore_saved()

        def _persist_fields() -> None:
            try:
                win_state.save_fields("autoM", _collect_fields())
                root.after(2000, _persist_fields)
            except tk.TclError:
                pass  # 창 닫힘

        root.after(2000, _persist_fields)

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

    # 목업 대조용 모니터 샘플 (정 -0.82/-0.82/-0.52, 역 0.12/0.12/0.23)
    preview_mon = {"fwd_en_sf": "-0.82", "fwd_en_s": "-0.82", "fwd_ex_sf": "-0.52",
                   "rev_en_sf": "0.12", "rev_en_s": "0.12", "rev_ex_sf": "0.23"}

    def refresh() -> None:
        try:
            if preview:
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
