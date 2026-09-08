"""자동M 주문 화면 (체결쏴 — HL·주식선물 선주문 maker→후주문 taker) — 코어 클라이언트.

    python -m kp_arb.order_autom     (운영은 main.bat 메뉴에서)

원본: docs/STG_2 목업(layout_1·체결쏴 설정·세트설정) + DESIGN-auto-m-exec.md §11(전략·화면 스펙).
화면 뼈대는 자동T(order_autot)와 거의 같고, 아래 3가지만 다르다:
  1) 진입 기준 컬럼이 2개(SF·S) — 청산은 SF 1개.
  2) 상단 모니터가 3칸(진입 SF / 진입 S / 청산 SF).
  3) 설정창(공통=체결쏴 설정 / 세트)이 자동M 전용.
**화면 스레드는 네트워크 금지**(CLAUDE.md). v1 = 레이아웃·입력 중심(발주 상태변화는 다음 단계).
공용 순수 헬퍼·상수는 order_autot에서 재사용(단일 출처).
"""
from __future__ import annotations

import json
from functools import partial
from typing import Any

from .order_autot import (
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

# 선물 월물 콤보(상단) — 표시 → 코어 settings.future_month 값 (DESIGN §5.11)
MONTH_MAP = {"최근": "near", "차근": "next"}  # 표시는 '최근/차근'(사용자 2026-09-03)


def pct_to_frac(value: float | None) -> float | None:
    """화면 %(0.5) → 코어 소수(0.005). 빈값은 None 그대로."""
    return None if value is None else value / 100.0


def set_payload(index: int, w: dict[str, Any], underlying: str) -> dict[str, Any]:
    """세트 화면 상태 → 코어 autom_set 명령(정방향, 종목별 책). 기준값은 %→소수."""
    return {
        "cmd": "autom_set", "underlying": underlying, "set": index,
        "target_qty": int(w.get("target") or 0), "per_qty": int(w.get("per") or 0),
        "switch_delay_s": int(w.get("delay") or 0),
        "en_sf": pct_to_frac(w.get("en_sf")), "en_s": pct_to_frac(w.get("en_s")),
        "ex_sf": pct_to_frac(w.get("ex_sf")),
        "rt_manual": w.get("rt_manual"), "clear_diff": bool(w.get("clear_diff")),
    }


def settings_payload(common: dict[str, Any]) -> dict[str, Any]:
    """체결쏴 설정 화면 상태 → 코어 autom_settings 명령. 범위·리스크는 %→소수."""
    win = common["windows"]
    return {
        "cmd": "autom_settings",
        "windows": [[win[0], win[1]], [win[2], win[3]]],
        "pre_tick": dict(common["pre_tick"]),
        "pre_delay_ms": int(common["pre_delay"]), "resume_delay_s": int(common["resume_delay"]),
        "pre_range": float(common["pre_range"]) / 100.0,
        "rel_buy": int(common["rel_buy"]), "rel_sell": int(common["rel_sell"]),
        "hl_margin_buy": float(common.get("hl_margin_buy", 1.0)) / 100.0,
        "hl_margin_sell": float(common.get("hl_margin_sell", 1.0)) / 100.0,
        "risk_fwd_en": float(common["risk"]["fwd_en"]) / 100.0,
        "risk_fwd_ex": float(common["risk"]["fwd_ex"]) / 100.0,
        "risk_fwd_gap": float(common["risk"]["fwd_gap"]) / 100.0,
    }


def sum_acc(rows: list[dict[str, Any]], leg: str) -> dict[str, float | None]:
    """세트별 누적(autom_live)을 방향 하나로 합산 — 수량은 **짝이 맞은(적은 쪽)** 체결량 합
    (사용자 확정 2026-09-08: LS·HL 누적 체결량이 다르면 적은 쪽 기준, SF 1 = HL 10), 환·Sprd는
    그 HL 수량 가중."""
    hl = sf = 0.0
    fx_w = sprd_w = 0.0
    sprd_q = 0.0
    for row in rows:
        acc = row.get(leg) or {}
        raw_hl = float(acc.get("hl_qty") or 0)
        raw_sf = float(acc.get("sf_qty") or 0)
        q = float(acc.get("matched_hl", min(raw_hl, raw_sf * 10)) or 0)
        hl += q
        sf += float(acc.get("matched_sf", q / 10) or 0)
        if q > 0 and acc.get("fx_avg") is not None:
            fx_w += float(acc["fx_avg"]) * q
        if q > 0 and acc.get("sprd") is not None:
            sprd_w += float(acc["sprd"]) * q
            sprd_q += q
    return {"hl_qty": hl, "sf_qty": sf,
            "fx_avg": fx_w / hl if hl > 0 else None,
            "sprd": sprd_w / sprd_q if sprd_q > 0 else None}

# 다리 진행 상태(exec §2)의 짧은 표시 — 상태줄 상세용(버튼 캡션은 항상 '진입'/'청산')
# 상태줄 다리 상태 표기 — pre_resting은 '접수'(옛 '걸림', 사용자 2026-09-08)
_STATUS_TEXT = {"armed": "감시", "pre_resting": "접수", "pre_partial": "부분",
                "post_pending": "HL", "settle_delay": "쉼", "halted": "중지"}

# 선주문 주문단위 설정 종목 순서 (목업 라벨 → underlying 코드)
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
    from .core_client import box_is_live, core_request, run_state_feed, watch_parent_exit
    from .ui_close import attach_auto_close
    from .ui_dialog import center_on_parent

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
        # 실시간(DESIGN §12.1 state 채널): 공유메모리 0.1초 읽기, 없거나 낡으면 HTTP 폴백.
        run_state_feed(state_box, log_tag="자동M", channel="state",
                       fallback_path="/state", poll_s=1.0)

    if not preview:
        threading.Thread(target=sender, daemon=True).start()
        threading.Thread(target=poller, daemon=True).start()

    def send(payload: dict[str, Any], label: str) -> None:
        jobs.put(({**payload, "screen": "autoM"}, label))

    # 화면 상태(로컬) — v1은 표시·입력만. 공통설정(체결쏴 설정) 기본값은 목업 기준.
    common: dict[str, Any] = {
        "windows": ["08:30:10", "08:46:20", "15:35:30", "15:46:55"],
        "pre_tick": {"sk_hynix": 3000, "samsung": 500, "hyundai": 1000},
        "pre_delay": 1000, "resume_delay": 10, "pre_range": 0.4,
        "rel_buy": 1, "rel_sell": 1,
        "hl_margin_buy": 1.0, "hl_margin_sell": 1.0,  # 후주문 HP 여유(%) — 지정가 taker

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
    # 종목 콤보 — 현대차 제외(사용자 2026-09-04, 시세 화면과 동일). 코어 취급 종목은 그대로.
    cb_under = ttk.Combobox(top, values=[u for u in UNDERLYINGS if u != "현대차"],
                            width=7, state="readonly")
    cb_under.set("하이닉스")
    cb_under.pack(side="left", padx=(2, 4))
    cb_under.bind("<<ComboboxSelected>>", lambda e: on_under_change(e))  # 종목별 책 전환
    # HL 호가단위(틱) — 일반주문창처럼 코어가 계산한 실제 틱 숫자(autom_live.hl_merge_ticks)로
    # 채운다. 코어 가격 수신 전엔 "-" 하나.
    cb_agg = ttk.Combobox(top, values=["-"], width=6, state="readonly")
    cb_agg.set("-")
    cb_agg.pack(side="left", padx=(0, 4))
    agg_map: dict[str, tuple[int | None, int | None]] = {}  # 틱 라벨 → (nSigFigs, mantissa)
    agg_shown = {"under": ""}  # 어느 종목 기준으로 콤보를 채웠나
    # 선물 월물(근/차근) — 화면(종목) 단위, 모든 세트 공통 (DESIGN §5.11, 사용자 확정 2026-09-03).
    # 목업 layout_1.png에는 없는 항목 — 호가단위 콤보 오른쪽. '적'으로 코어에 보낸다.
    cb_month = ttk.Combobox(top, values=list(MONTH_MAP), width=4, state="readonly")
    cb_month.set("최근")
    cb_month.pack(side="left", padx=(0, 4))

    def cur_under() -> str:
        """이 창이 보여주는 종목(코어 책 키). 자동M 상태는 코어가 종목별로 따로 든다(2026-09-08)."""
        return UNDER_MAP[cb_under.get()]

    def apply_market() -> None:
        u = cur_under()
        if cb_agg.get() in agg_map:  # 틱 목록이 아직 없으면(가격 미수신) 머지는 건너뜀
            nsf, mant = agg_map[cb_agg.get()]
            send({"cmd": "manual_hl_merge", "underlying": u,
                  "n_sig_figs": nsf, "mantissa": mant}, "호가단위")
        send({"cmd": "autom_month", "underlying": u, "month": MONTH_MAP[cb_month.get()]},
             "선물 월물")
        send_ref_qty(force=True)
        state_box["_applied"] = True  # 모니터 수치는 '적'을 누른 뒤부터 표시(사용자 2026-09-04)

    def on_under_change(_e: object = None) -> None:
        """종목 콤보 — 그 종목의 책(세트·RT·체결차·기준수량·월물)을 코어에서 다시 보여준다.
        실행 중이어도 바꿀 수 있다(다른 창에서 다른 종목을 돌리기 위함, 사용자 확정 2026-09-08)."""
        state_box["_loaded_under"] = None  # 다음 갱신 때 그 종목 책으로 입력값 다시 채움
        agg_shown["under"] = ""
        agg_map.clear()
        cb_agg.config(values=["-"])
        cb_agg.set("-")
        ref_sent["qty"] = -1

    # 마지막으로 보낸 기준수량(같으면 다시 안 보냄) · 입력칸 평소 배경(깜빡임 뒤 복귀)
    ref_sent: dict[str, Any] = {"qty": -1, "bg": "white"}

    def send_ref_qty(force: bool = False) -> None:
        """기준수량만 코어로 — Enter·포커스 이동 시 바로 반영(사용자 확정 2026-09-07).

        모니터 est 계산용 수량이라 세트 실행 중에도 바꿀 수 있고, '적'과 달리
        종목·호가단위·월물은 건드리지 않는다.
        """
        qty = parse_qty(ent_refqty.get())
        if not force and qty == ref_sent["qty"]:
            return
        ref_sent["qty"] = qty
        send({"cmd": "autom_ref_qty", "underlying": cur_under(), "qty": qty}, "기준수량")
        flash_ref_qty()

    def revert_ref_qty() -> None:
        """Enter 없이 포커스가 나가면 입력을 **마지막으로 보낸 값**으로 되돌린다(사용자 2026-09-07).

        보낸 적이 없으면(창 연 직후, '적' 전) 입력을 그대로 둔다.
        """
        last = ref_sent["qty"]
        if last < 0 or ent_refqty.get() == str(last):
            return
        ent_refqty.delete(0, "end")
        ent_refqty.insert(0, str(last))

    def flash_ref_qty() -> None:
        """보냈다는 표시 — 입력칸 배경을 잠깐 바꿨다 되돌린다(버튼 눌림처럼, 사용자 2026-09-07)."""
        ent_refqty.config(bg="#bfe0ff")
        ent_refqty.after(180, lambda: ent_refqty.config(bg=ref_sent["bg"]))

    ttk.Style().configure("Ap.TButton", padding=(6, 2))  # 콤보 높이(≈26)에 맞춤
    btn_apply = ttk.Button(top, text="적", width=3, style="Ap.TButton", command=apply_market)
    btn_apply.pack(side="left", padx=(0, 4))
    ent_refqty = tk.Entry(top, width=6, justify="right", validate="key",
                          validatecommand=vcmd_int, font=T.FONT_NUM_LG)
    ent_refqty.insert(0, "0")
    ent_refqty.pack(side="left", padx=(0, 6))
    ref_sent["bg"] = ent_refqty.cget("bg")  # 깜빡임 뒤 되돌릴 평소 배경(중첩 호출에도 안전)
    ent_refqty.bind("<Return>", lambda _e: send_ref_qty())
    ent_refqty.bind("<FocusOut>", lambda _e: revert_ref_qty())  # Enter 없이 나가면 수정 취소
    ent_refqty.bind("<Escape>", lambda _e: revert_ref_qty())

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
                      "sec": lbl_sec, "run_en": False, "run_ex": False,
                      # 중지 표시(세트 행 검정/흰 글자, §9a) 되돌리기용 원래 배경
                      "row": [(lbl_tg, "#fffbcc"), (lbl_per, "#f0f0f0"), (lbl_rt, "white"),
                              (lbl_diff, "white"), (lbl_sec, "#f0f0f0")],
                      "halted": False})
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
        release = False
        if turning_on and dtag == "fwd":
            # 중지(HALTED)는 사람이 직접 풀어야 재개(exec §2) — 확인 뒤 해제 + 실행(2026-09-07)
            live_sets = _live_sets()
            leg_live = ((live_sets[i] if i < len(live_sets) else {})
                        .get("entry" if side == "en" else "exit") or {})
            if leg_live.get("status") == "halted":
                from .ui_dialog import ask_yes_no

                name = f"{i + 1}세트 {'진입' if side == 'en' else '청산'}"
                reason = str(leg_live.get("halt_reason") or "")
                if not ask_yes_no(root, "중지 해제",
                                  f"{name}이(가) 중지 상태입니다.\n{reason}\n\n"
                                  "헤지 정리를 마쳤으면 '예' — 중지를 풀고 실행합니다."):
                    return
                release = True
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
        # 코어 상태가 명령을 반영하기까지(실시간 채널 0.2~0.3초) 화면 표시를 코어값이 덮어쓰지
        # 않게 한다 — 안 그러면 잠금→풀림→잠금으로 깜빡인다(사용자 실증 2026-09-04).
        w[f"_pend_{side}"] = time.time() + 2.0
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
        if dtag == "fwd":  # 코어 실행(정방향) — 켤 때 세트 입력값을 먼저 보내고 실행 명령
            block = "entry" if side == "en" else "exit"
            if on:
                send(set_payload(i, w, cur_under()), "세트 설정")
                if release:  # 중지 해제 먼저(같은 큐라 순서 보장) → 실행
                    send({"cmd": "autom_release", "underlying": cur_under(), "set": i,
                          "block": block}, "중지 해제")
            send({"cmd": "autom_run", "underlying": cur_under(), "set": i, "block": block,
                  "value": on},
                 "실행" if on else "정지")
        else:
            status.config(text="역방향은 아직 미구현 — 화면 표시만(정방향 실측 후)")

    def clear_acc(dtag: str, group: str) -> None:
        accs = sets[(dtag, 0)].get("_acc", {}).get(group, {})
        for lbl in accs.values():
            lbl.config(text="-")
        if dtag == "fwd":  # 누적은 세트별로 코어가 들고 있다 → 3세트 모두 clear
            block = "entry" if group == "진입" else "exit"
            for idx in range(3):
                send({"cmd": "autom_clear_acc", "underlying": cur_under(), "set": idx,
                      "block": block}, "누적 clear")

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
            else:  # 목표수량·1회주문·전환딜레이 = 세트 상태값 (전환딜레이는 0도 유효한 값)
                val = w.get(key)
                blank = val is None or (val == 0 and key != "delay")
                e.insert(0, "" if blank else str(val))
            e.grid(row=r, column=1, padx=6, pady=3)
            ents[key] = e
        # RT 수동 입력·체결차 Clear는 **1회성** — 열 때마다 꺼진 상태로 시작하고 저장하지 않는다
        # (사용자 확정 2026-09-07). 값이 남아 있으면 실행 켤 때마다 RT를 덮어쓰는 사고가 난다.
        rt_var = tk.BooleanVar(value=False)
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
            if rt_var.get() and not rt_ent.get().strip():  # 체크만 하고 값 없음 → 확인창
                errs.append("RT 진입수량 수동 입력이 켜져 있는데 값이 없습니다")
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
            if dtag == "fwd":  # 코어에 세트 설정 전송(실행 중에도 가능 — 코어가 다음 판정부터 반영)
                send(set_payload(i, w, cur_under()), "세트 설정")
            # 1회성 항목은 보낸 즉시 비운다 — 뒤의 실행 켬(set_payload 재전송)·저장에 안 실리게
            w["rt_manual"] = None
            w["clear_diff"] = False

        btns = tk.Frame(win)
        btns.grid(row=len(rows) + 2, column=0, columnspan=2, pady=(4, 6))
        tk.Button(btns, text="확인", width=8, command=save).pack(side="left", padx=4)
        tk.Button(btns, text="취소", width=8, command=win.destroy).pack(side="left", padx=4)
        _center(win)

    def apply_set_display(dtag: str, i: int) -> None:
        w = sets[(dtag, i)]
        w["tg"].config(text=str(w["target"]) if w["target"] else "-")
        w["per_lbl"].config(text=str(w["per"]) if w["per"] else "-")
        w["sec"].config(text=str(w["delay"]) if w["delay"] is not None else "-")  # 0도 표시
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

        # 배치(사용자 확정 2026-09-04): 한 그리드에 두 줄 — 줄1 [선주문 주문단위 | 선주문(딜레이·
        # 재개·범위)], 줄2 [상대호가 | 후주문 HP]. 행 수가 같은 틀끼리 같은 줄이라 가로선이 맞는다.
        # '선주문 주문단위'(옛 이름 '선주문 호가 단위') — 역산가를 주문 단위로 맞추는 값. 시세
        # 호가단위와 헷갈려 이름 변경(09-08). 한계의 1틱은 시세 호가단위(코어가 가격대로 계산).
        pt = tk.LabelFrame(win, text="선주문 주문단위")
        pt.grid(row=1, column=0, columnspan=2, sticky="new", padx=6, pady=4)
        pt_ents: dict[str, tk.Entry] = {}
        for r, (plabel, pcode) in enumerate(_PRE_TICK_ROWS):
            tk.Label(pt, text=plabel, anchor="w", width=7).grid(  # 라벨 폭 통일 → 입력칸 세로 정렬
                row=r, column=0, sticky="w", padx=4, pady=2)
            e = tk.Entry(pt, width=9, justify="right", validate="key",
                         validatecommand=vcmd_int)
            e.insert(0, str(common["pre_tick"][pcode]))
            e.grid(row=r, column=1, padx=4, pady=2)
            pt_ents[pcode] = e
        # 줄2 왼쪽 — 상대호가 콤보(매수/매도)
        rel = tk.LabelFrame(win, text="상대호가")
        rel.grid(row=2, column=0, columnspan=2, sticky="new", padx=6, pady=4)
        rel_cbs: dict[str, ttk.Combobox] = {}
        for r, (rk, rlabel) in enumerate((("rel_buy", "매수"), ("rel_sell", "매도"))):
            choices = _REL_CHOICES_BUY if rk == "rel_buy" else _REL_CHOICES_SELL
            tk.Label(rel, text=rlabel, anchor="w", width=7).grid(
                row=r, column=0, sticky="w", padx=4, pady=2)
            cb = ttk.Combobox(rel, values=choices, width=14, state="readonly")
            cb.set(choices[common[rk] - 1])
            cb.grid(row=r, column=1, padx=4, pady=2)
            rel_cbs[rk] = cb

        # 줄2 오른쪽 — 후주문 HP(%) (자동T의 HP 여유와 같은 뜻: 매도 = 매수1호가×(1−여유)).
        # 2행짜리라 2행인 상대호가 틀과 같은 줄, 3행짜리 선주문 틀은 3행인 호가단위 틀과 같은 줄.
        hp = tk.LabelFrame(win, text="후주문 HP(%)")
        hp.grid(row=2, column=2, columnspan=2, sticky="new", padx=6, pady=4)
        hp_ents: dict[str, tk.Entry] = {}
        for r, (hk, hlabel) in enumerate((("hl_margin_buy", "매수"), ("hl_margin_sell", "매도"))):
            tk.Label(hp, text=hlabel, anchor="w", width=7).grid(
                row=r, column=0, sticky="w", padx=4, pady=2)
            e_hp = tk.Entry(hp, width=9, justify="right", validate="key",
                            validatecommand=vcmd_dec)
            e_hp.insert(0, f"{common.get(hk, 1.0):g}")
            e_hp.grid(row=r, column=1, padx=4, pady=2)
            hp_ents[hk] = e_hp
        # 줄1 오른쪽 — 선주문 딜레이·재개 딜레이·범위 (3행 — 호가단위 틀과 같은 줄, 행 높이 일치)
        pr = tk.LabelFrame(win, text="선주문")
        pr.grid(row=1, column=2, columnspan=2, sticky="new", padx=6, pady=4)
        pr.columnconfigure(0, weight=1)  # 라벨은 오른쪽 붙임 → 입력칸이 오른쪽 끝에 정렬
        tk.Label(pr, text="딜레이(ms)").grid(row=0, column=0, sticky="e", pady=2)
        e_delay = tk.Entry(pr, width=7, justify="right", validate="key",
                           validatecommand=vcmd_int)
        e_delay.insert(0, str(common["pre_delay"]))
        e_delay.grid(row=0, column=1, padx=4, pady=2, sticky="e")  # 오른쪽 끝을 콤보와 맞춤
        tk.Label(pr, text="재개 딜레이(초)").grid(row=1, column=0, sticky="e", pady=2)
        e_resume = tk.Entry(pr, width=7, justify="right", validate="key",
                            validatecommand=vcmd_int)
        e_resume.insert(0, str(common["resume_delay"]))
        e_resume.grid(row=1, column=1, padx=4, pady=2, sticky="e")
        tk.Label(pr, text="범위(%)").grid(row=2, column=0, sticky="e", pady=2)
        e_range = tk.Entry(pr, width=7, justify="right", validate="key",
                           validatecommand=vcmd_dec)
        e_range.insert(0, f"{common['pre_range']:g}")
        e_range.grid(row=2, column=1, padx=4, pady=2, sticky="e")

        # 리스크방지 — 정/역방향 각 3칸
        risk_ents: dict[str, tk.Entry] = {}

        def _risk_block(col: int, title: str, pfx: str,
                        specs: tuple[tuple[str, str, str], ...]) -> None:
            fr = tk.LabelFrame(win, text=title)
            fr.grid(row=3, column=col, columnspan=2, sticky="new", padx=6, pady=4)
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
            common["resume_delay"] = parse_qty(e_resume.get())
            common["pre_range"] = parse_threshold(e_range.get()) or 0.0
            for rk, cb in rel_cbs.items():
                choices = _REL_CHOICES_BUY if rk == "rel_buy" else _REL_CHOICES_SELL
                common[rk] = choices.index(cb.get()) + 1
            for hk, e_hp in hp_ents.items():
                common[hk] = parse_threshold(e_hp.get()) or 0.0
            for rk, e in risk_ents.items():
                common["risk"][rk] = parse_threshold(e.get()) or 0.0
            refresh_windows_bar()
            win.destroy()
            send(settings_payload(common), "체결쏴 설정")  # 코어 저장·즉시 반영

        btns = tk.Frame(win)
        btns.grid(row=4, column=0, columnspan=4, pady=(6, 6))
        tk.Button(btns, text="확인", width=8, command=save).pack(side="left", padx=4)
        tk.Button(btns, text="취소", width=8, command=win.destroy).pack(side="left", padx=4)
        _center(win)

    def _center(win: tk.Toplevel) -> None:
        center_on_parent(win, root)  # 팝업은 항상 그 화면 중앙(사용자 2026-09-04, DESIGN-ui §7)
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

    # width=1: 상태줄 글이 길어도(중지 사유 등) 창 폭을 밀어 키우지 않게 — 라벨이 요구하는 폭을
    # 1글자로 두고 pack(fill="x")로 창 폭만큼만 보인다(넘치는 글은 잘림, 전문은 로그에). 09-08.
    status = tk.Label(root, anchor="w", relief="groove", width=1,
                      text="UI 미리보기 — 코어 미연결" if preview else "코어 확인 중 ...")
    status.pack(fill="x", padx=4, pady=(2, 4))
    refresh_windows_bar()  # 상단 주문가능시간 표시 초기화

    def _live_book() -> dict[str, Any]:
        """이 창 종목의 실시간 책 스냅샷(autom_live[종목]) — 없으면 빈 dict."""
        data = state_box.get("data") or {}
        live = (data.get("autom_live") or {}).get(cur_under())
        return live if isinstance(live, dict) else {}

    def _live_sets() -> list[dict[str, Any]]:
        rows = _live_book().get("sets")
        return rows if isinstance(rows, list) else []

    def _any_running() -> bool:
        return any(bool((r.get("entry") or {}).get("running"))
                   or bool((r.get("exit") or {}).get("running")) for r in _live_sets())

    def _set_status(text: str) -> None:
        status.config(text=text)

    # 창 닫기(X) — 자동주문 화면 공통 규칙(DESIGN-ui §6): 실행 중이면 확인 뒤 정지하고 닫기.
    # 정지 대상은 **이 창이 보여주는 종목**뿐 — 다른 종목은 다른 창/코어에서 계속(2026-09-08).
    if not preview:
        attach_auto_close(
            root, title="체결쏴 (자동M)", is_running=_any_running,
            send_stop=lambda: send({"cmd": "autom_stop_all", "underlying": cur_under()},
                                   "이 종목 전 세트 정지"),
            set_status=_set_status)

    # --- 화면 저장/복원 (win_state.autoM — 2초 자동저장, order_hl과 동일 방식) ---
    def _collect_fields() -> dict[str, Any]:
        # 세트 값(목표·1회·기준값·RT·체결차)은 코어가 종목별로 든다(단일 진실, 2026-09-08) —
        # 창은 종목·호가단위·기준수량 표시값과 체결쏴 설정(공통)만 저장한다.
        return {
            "under": cb_under.get(), "agg": cb_agg.get(), "refqty": ent_refqty.get(),
            "common": {"windows": list(common["windows"]),
                       "pre_tick": dict(common["pre_tick"]),
                       "pre_delay": common["pre_delay"], "resume_delay": common["resume_delay"],
                       "pre_range": common["pre_range"],
                       "rel_buy": common["rel_buy"], "rel_sell": common["rel_sell"],
                       "hl_margin_buy": common.get("hl_margin_buy", 1.0),
                       "hl_margin_sell": common.get("hl_margin_sell", 1.0),
                       "risk": dict(common["risk"])}}

    def _restore_saved() -> None:
        saved = win_state.saved_fields("autoM")
        if not saved:
            return
        if saved.get("under") in cb_under["values"]:  # 현대차 등 제외된 종목은 복원 안 함
            cb_under.set(saved["under"])
        if isinstance(saved.get("agg"), str):
            state_box["_want_agg"] = saved["agg"]  # 틱 목록이 오면 그때 고른다
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
            for k in ("pre_delay", "resume_delay", "rel_buy", "rel_sell"):
                if isinstance(sc.get(k), int):
                    common[k] = sc[k]
            if isinstance(sc.get("pre_range"), int | float):
                common["pre_range"] = float(sc["pre_range"])
            for hk in ("hl_margin_buy", "hl_margin_sell"):
                if isinstance(sc.get(hk), int | float):
                    common[hk] = float(sc[hk])
            if isinstance(sc.get("risk"), dict):
                for k in common["risk"]:
                    if isinstance(sc["risk"].get(k), int | float):
                        common["risk"][k] = float(sc["risk"][k])
        # 세트 값은 창 저장에서 복원하지 않는다 — 코어 책(종목별)이 원본(_load_inputs_from_core)
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

    def _fmt_num(v: Any, d: int = 0) -> str:
        return f"{float(v):,.{d}f}" if isinstance(v, int | float) else "-"

    def _paint_leg(w: dict[str, Any], side: str, leg: dict[str, Any]) -> None:
        """실행 버튼 색·글자(진행 상태)·기준값 칸 잠금을 코어 상태에 맞춘다."""
        on = bool(leg.get("running"))
        status_code = str(leg.get("status") or "idle")
        key = f"run_{side}"
        if w[key] != on and time.time() < w.get(f"_pend_{side}", 0.0):
            return  # 방금 누른 버튼 — 코어가 반영할 때까지 화면 표시 유지(깜빡임 방지)
        btn = w["btn_en" if side == "en" else "btn_ex"]
        # 캡션은 항상 '진입'/'청산'(사용자 2026-09-04) — 진행 상태는 색(실행중 빨강/파랑,
        # 중지 검정)과 상태줄 상세로만 보여준다.
        text = "진입" if side == "en" else "청산"
        if status_code == "halted":
            btn.config(text=text, bg="black", fg="white", font=T.FONT_NUM_LG)
        elif on:
            btn.config(text=text, bg=T.C_BUY if side == "en" else T.C_SELL, fg="white",
                       font=T.FONT_NUM_LG)
        else:
            btn.config(text=text, bg="SystemButtonFace", fg="black", font=T.FONT_BASE_LG)
        if w[key] == on:
            return
        w[key] = on
        st = "disabled" if on else "normal"
        for ent in (("e_en_sf", "e_en_s") if side == "en" else ("e_ex_sf",)):
            w[ent].config(state=st)

    def _leg_detail(i: int, side: str, leg: dict[str, Any]) -> str | None:
        """상태줄용 진행 상세 — 감시/대기 외 상태만 한 줄."""
        st = str(leg.get("status") or "idle")
        if st in ("idle", "armed"):
            return None
        name = "진입" if side == "en" else "청산"
        parts = [f"{i + 1}세트 {name}: {_STATUS_TEXT.get(st, st)}"]
        if leg.get("pre_order_id"):
            price = leg.get("pre_price")
            px = f"{float(price):,.0f}" if isinstance(price, int | float) else "-"
            parts.append(f"선주문 #{leg['pre_order_id']} {leg.get('pre_qty', 0)}계약 @{px}"
                         f" (체결 {leg.get('pre_filled', 0)}/{leg.get('pre_qty', 0)})")
        if leg.get("post_pending"):
            parts.append(f"HL {leg['post_pending']} 대기")
        if st == "halted" and leg.get("halt_reason"):
            parts.append(str(leg["halt_reason"]))
        return " · ".join(parts)

    def _paint_halt(w: dict[str, Any], halted: bool) -> None:
        """중지 표시 — 세트 행을 검은 배경·흰 글자로(사용자 확정 2026-09-04)."""
        if w["halted"] == halted:
            return
        w["halted"] = halted
        for lbl, orig_bg in w["row"]:
            lbl.config(bg="black" if halted else orig_bg, fg="white" if halted else "black")

    def _load_inputs_from_core(data: dict[str, Any]) -> None:
        """코어가 기억하는 **이 종목 책**(세트 입력값·기준수량·월물)을 화면에 채운다 — 코어가 원본.
        창을 열 때와 종목 콤보를 바꿀 때 1회(2026-09-08: 종목별 독립)."""
        book = ((data.get("autom") or {}).get("books") or {}).get(cur_under()) or {}
        rows = book.get("sets")
        if not isinstance(rows, list):
            return
        month_label = next((k for k, v in MONTH_MAP.items() if v == book.get("future_month")), None)
        if month_label:
            cb_month.set(month_label)
        ref = book.get("ref_qty")
        if isinstance(ref, int):
            ent_refqty.delete(0, "end")
            ent_refqty.insert(0, str(ref))
            ref_sent["qty"] = ref
        for i, raw in enumerate(rows[:3]):
            w = sets[("fwd", i)]
            w["target"] = int(raw.get("target_qty") or 0)
            w["per"] = int(raw.get("per_qty") or 0)
            w["delay"] = int(raw.get("switch_delay_s") or 0)
            for key in ("en_sf", "en_s", "ex_sf"):
                v = raw.get(key)
                w[key] = float(v) * 100.0 if isinstance(v, int | float) else None
            apply_set_display("fwd", i)

    def _load_common_from_core(data: dict[str, Any]) -> None:
        """체결쏴 설정(공통)을 코어 값으로 맞춘다 — 코어가 원본(단일 진실).

        창마다 자기 파일에 들고 있으면 다른 창에서 바꾼 설정을 모른다(실측 2026-09-08: 하이닉스
        창에서 주문가능시간을 줄였는데 삼성 창은 옛 값을 보여 "시간 안인데 왜 안 나가나"). 코어
        값이 바뀔 때만 다시 채운다(설정창 입력 중 덮어쓰기 방지 — 설정창은 열 때 읽음)."""
        am = data.get("autom") or {}
        st = am.get("settings")
        if not isinstance(st, dict):
            return
        sig = json.dumps(st, sort_keys=True) + json.dumps(
            [am.get("risk_fwd_en"), am.get("risk_fwd_ex"), am.get("risk_fwd_gap")])
        if state_box.get("_common_sig") == sig:
            return
        state_box["_common_sig"] = sig
        wins = st.get("windows")
        if isinstance(wins, list) and len(wins) == 2:
            common["windows"] = [str(x) for pair in wins for x in pair]
        if isinstance(st.get("pre_tick"), dict):
            for k in common["pre_tick"]:
                v = st["pre_tick"].get(k)
                if isinstance(v, int):
                    common["pre_tick"][k] = v
        for src, dst in (("pre_delay_ms", "pre_delay"), ("resume_delay_s", "resume_delay"),
                         ("rel_buy", "rel_buy"), ("rel_sell", "rel_sell")):
            if isinstance(st.get(src), int):
                common[dst] = st[src]
        for src, dst in (("pre_range", "pre_range"), ("hl_margin_buy", "hl_margin_buy"),
                         ("hl_margin_sell", "hl_margin_sell")):
            if isinstance(st.get(src), int | float):
                common[dst] = float(st[src]) * 100.0  # 코어는 소수, 화면은 %
        for src, dst in (("risk_fwd_en", "fwd_en"), ("risk_fwd_ex", "fwd_ex"),
                         ("risk_fwd_gap", "fwd_gap")):
            if isinstance(am.get(src), int | float):
                common["risk"][dst] = float(am[src]) * 100.0
        refresh_windows_bar()

    def _refresh_merge_combo(data: dict[str, Any]) -> None:
        """코어가 준 hl_merge_ticks로 호가단위 콤보를 채운다 — 종목이 바뀌거나 처음일 때만 set.
        우선순위: 코어 적용값(hl_merge_active) > 창 저장값 > 최소 틱 (일반주문창과 동일)."""
        live = _live_book()
        ticks = live.get("hl_merge_ticks") or []
        under = cb_under.get()
        if not ticks or (agg_shown["under"] == under and agg_map):
            return
        agg_map.clear()
        vals: list[str] = []
        for t in ticks:
            label = str(t.get("tick"))
            vals.append(label)
            agg_map[label] = (t.get("n_sig_figs"), t.get("mantissa"))
        cb_agg.config(values=vals)
        active = live.get("hl_merge_active")
        core_label = None
        if isinstance(active, dict):
            key = (active.get("n_sig_figs"), active.get("mantissa"))
            core_label = next((s for s, v in agg_map.items() if v == key), None)
        want = state_box.get("_want_agg")
        cb_agg.set(core_label or (want if want in vals else vals[0]))
        agg_shown["under"] = under

    def apply_live() -> None:
        data = state_box.get("data") or {}
        _load_common_from_core(data)  # 체결쏴 설정은 코어 값(공통) — 다른 창의 변경도 반영
        if state_box.get("_loaded_under") != cur_under() and data.get("autom"):
            state_box["_loaded_under"] = cur_under()  # 종목이 바뀌면 그 책으로 다시 채움
            _load_inputs_from_core(data)
        rows = _live_sets()
        details: list[str] = []
        for i, row in enumerate(rows[:3]):
            w = sets[("fwd", i)]
            en, ex = row.get("entry") or {}, row.get("exit") or {}
            _paint_leg(w, "en", en)
            _paint_leg(w, "ex", ex)
            _paint_halt(w, en.get("status") == "halted" or ex.get("status") == "halted")
            for side, leg in (("en", en), ("ex", ex)):
                detail = _leg_detail(i, side, leg)
                if detail:
                    details.append(detail)
            w["rt"].config(text=_fmt_num(row.get("rt")))
            w["diff"].config(text=_fmt_num(row.get("fill_diff")))
        # 상단 모니터 3칸 — 코어가 기준수량으로 계산한 est 괴리(%), 정/역 각각.
        # '적'을 누른 뒤부터 표시(종목·호가단위·기준수량이 코어에 적용된 값이라야 뜻이 있음).
        monitor = _live_book().get("monitor") or {}
        # 표시 여부는 화면 입력칸이 아니라 **코어가 실제로 쓰는 기준수량**으로 판단 —
        # 입력칸을 지우는 중에도 수치가 사라지지 않게(사용자 2026-09-07).
        core_ref = int(_live_book().get("ref_qty") or 0)
        applied = bool(state_box.get("_applied")) and core_ref > 0
        for dtag in ("fwd", "rev"):
            vals = monitor.get(dtag) or {}
            for skey in ("en_sf", "en_s", "ex_sf"):
                v = vals.get(skey)
                text = (f"{float(v) * 100:.2f}"
                        if applied and isinstance(v, int | float) else "-")
                mon[f"{dtag}_{skey}"].config(text=text)
        # 매매결과 누적(정방향) — 진입 = entry 다리, 청산 = exit 다리(세트 합산)
        accs = sets[("fwd", 0)].get("_acc", {})
        for glabel, leg in (("진입", "entry"), ("청산", "exit")):
            labels = accs.get(glabel, {})
            if not labels or not rows:
                continue
            agg = sum_acc(rows, leg)
            hp_key, s_key, fx_key = ("-HP", "+S", "-환") if leg == "entry" else ("+HP", "-S", "+환")
            # 짝이 맞은 체결량 — HL 부분 체결이면 SF도 소수(0.0588 등)라 소수면 자릿수를 붙인다
            sf_q, hl_q = float(agg["sf_qty"] or 0), float(agg["hl_qty"] or 0)
            sf_txt = _fmt_num(sf_q, 2 if sf_q % 1 else 0)
            labels["누적"].config(text=sf_txt)
            labels[hp_key].config(text=_fmt_num(hl_q, 3 if hl_q % 1 else 0))
            labels[s_key].config(text=sf_txt)
            labels[fx_key].config(text=_fmt_num(agg["fx_avg"], 1))
            sprd = agg["sprd"]
            labels["Sprd"].config(text=f"{sprd * 100:.3f}" if sprd is not None else "-")
        _refresh_merge_combo(data)
        # 이 종목이 실행 중이면 호가단위·월물 콤보와 '적'만 잠금(실행 중 상대 상품·호가단위가
        # 바뀌면 판정 기준이 통째로 바뀜). **종목 콤보는 항상 열어 둔다** — 다른 창에서 다른
        # 종목을 돌릴 수 있어야 하므로(사용자 확정 2026-09-08). 자동M 상태는 코어가 종목별로 든다.
        running = _any_running()
        for cb in (cb_agg, cb_month):
            cb.config(state="disabled" if running else "readonly")
        btn_apply.config(state="disabled" if running else "normal")
        if details:  # 진행 중인 세트의 상세(선주문 번호·가격·체결·HL 대기·중지 사유)
            status.config(text=" / ".join(details))

    def refresh() -> None:
        try:
            if preview:
                for key, lbl in mon.items():
                    lbl.config(text=preview_mon.get(key, "-"))
                return
            connected = box_is_live(state_box, time.time())  # 데이터 있고 3초 안에 성공
            if not connected:
                for lbl in mon.values():
                    lbl.config(text="-")
                status.config(text="코어 미접속 — 메인에서 코어 시작")
            else:
                apply_live()
        except tk.TclError:
            return
        _reschedule(refresh, 300)

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
