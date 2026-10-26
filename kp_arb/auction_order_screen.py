"""동시호가 주문 화면(DESIGN-auction-order.md §1·§2·§4·§5, ver2 2026-10-22) — 화면 먼저(코어 연결
전).

배치는 엑셀 ver2 셀 자리를 따른다: 윗줄(종목·월물·호가단위·적) / 왼쪽 예약주문 16행(매수 8·매도 8:
주문Sprd·수량·예상주문가·예약주문 버튼) / 오른쪽 주문시각 6칸 → 원달러 대응 설정 행 → HL주문설정
a~d.
`--preview`로 코어 없이 뜬다. 코어 명령·상태 연결은 화면 모양이 확정된 뒤(§9-2·3).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from .domain.enums import Underlying

UNDERLYINGS = (Underlying.SK_HYNIX, Underlying.SAMSUNG)
NAMES = {Underlying.SK_HYNIX: "하이닉스", Underlying.SAMSUNG: "삼성"}
BY_NAME = {v: k for k, v in NAMES.items()}
ROWS_PER_SIDE = 8                      # 매수 8행 + 매도 8행 고정(ver2 P24)
HL_ROWS = ("a", "b", "c", "d")         # 프로세스3 4건
# 기본값(ver2 G21~K22·F26~K27) — 전부 사용자 입력칸(사용자 2026-10-22 답 4)
DEFAULTS = {"ls_hms": "15:44:55", "ls_ms": "100", "ls_itv": "200",
            "fx_hms": "15:44:58", "fx_ms": "200", "fx_itv": "200",
            "tick": "20", "hedge_pct": "100", "fx_unit": "10"}
# 엑셀 예시 값(화면 확인용 — 실제는 사용자 입력)
SAMPLE_BUY = (("0.3", "20"), ("0.3", "20"), ("0.35", "15"), ("0.35", "15"),
              ("0.4", "10"), ("0.4", "10"), ("0.45", "5"), ("0.45", "5"))
SAMPLE_SELL = (("-0.1", "20"), ("-0.1", "20"), ("-0.15", "15"), ("-0.15", "15"),
               ("-0.2", "10"), ("-0.2", "10"), ("-0.25", "5"), ("-0.25", "5"))


def side_label(side: str) -> str:
    """예약주문 버튼 글 — 매수 행 "정(HL매도)", 매도 행 "역(HL매수)"(ver2 D23·D31, 뜻은
    매수/매도)."""
    return "정(HL매도)" if side == "buy" else "역(HL매수)"


def row_missing(spread_text: str, qty_text: str) -> str | None:
    """예약주문을 켜기 전 행 검사(사용자 2026-10-26 "입력이 없을 때도 켜진다 — 알림 띄우고 못
    켜게") — 빠진 것의 이름("주문Sprd"·"수량"), 다 있으면 None. 수량은 0보다 커야 한다. 순수."""
    missing = []
    try:
        float(spread_text.strip())
    except ValueError:
        missing.append("주문Sprd")
    try:
        if float(qty_text.replace(",", "").strip()) <= 0:
            missing.append("수량")
    except ValueError:
        missing.append("수량")
    return "·".join(missing) if missing else None


def fx_missing(tick_text: str, hedge_text: str, unit_text: str) -> str | None:
    """환 예약실행을 켜기 전 검사(사용자 2026-10-26) — 빠진 것의 이름("틱"·"헤지비율"·
    "1회주문수량"), 다 있으면 None. 틱은 0 이상, 헤지비율·1회주문수량은 0보다 커야 한다. 순수."""
    missing = []
    for name, text, min_ok in (("틱", tick_text, 0), ("헤지비율", hedge_text, 1),
                               ("1회주문수량", unit_text, 1)):
        try:
            if int(text.replace(",", "").strip()) < min_ok:
                missing.append(name)
        except ValueError:
            missing.append(name)
    return "·".join(missing) if missing else None


def hl_missing(offsets: Mapping[str, str]) -> str | None:
    """HL 예약실행을 켜기 전 검사 — 주문설정(오프셋)이 **숫자가 아닌** 행 이름("a·c"), 없으면
    None. 빈 칸은 0(스펙 §5 "비어 있으면 0 → 기준가" — 사용자 2026-10-26 "이것만 원복")이라 막지
    않는다. 순수."""
    bad = []
    for name, text in offsets.items():
        if not text.strip():
            continue
        try:
            float(text.strip())
        except ValueError:
            bad.append(name)
    return "·".join(bad) if bad else None


def pt_missing(pt_text: str) -> bool:
    """정정 버튼 전 검사(사용자 2026-10-26) — 정정PT가 비거나 0 이하면 True. 순수."""
    try:
        return float(pt_text.strip()) <= 0
    except ValueError:
        return True


def any_reserved(res_on: Mapping[tuple[str, int], bool]) -> bool:
    """예약주문 행이 하나라도 켜져 있나 — 켜져 있으면 윗줄(월물·호가단위·적)을 잠근다(사용자
    2026-10-26). 순수."""
    return any(bool(v) for v in res_on.values())


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    import sys
    import tkinter as tk
    from functools import partial
    from tkinter import ttk

    from . import ui_theme as T
    from . import win_state
    from .core_client import screen_log, watch_parent_exit
    from .ui_dialog import show_message
    from .ui_fields import is_decimal_text, is_int_text, is_time_text, is_unsigned_decimal_text
    from .ui_status import StallBanner, StatusCombo, log_tk_errors

    preview = "--preview" in sys.argv
    if not preview:
        watch_parent_exit()
    root = tk.Tk()
    root.title("동시호가 주문")
    root.resizable(True, True)
    if not preview:
        win_state.attach(root, "auction_order")
    T.apply_base(root)
    root.option_add("*Font", T.FONT_BASE_LG)  # 체결쏴·NXT 개장과 같은 11pt
    vcmd_int = (root.register(is_int_text), "%P")
    vcmd_dec = (root.register(is_decimal_text), "%P")
    vcmd_qty = (root.register(is_unsigned_decimal_text), "%P")
    vcmd_time = (root.register(is_time_text), "%P")
    log = screen_log()
    log_tk_errors(root, log)  # 버튼 콜백 예외를 화면 로그에(콘솔이 없어 안 보임)
    w: dict[str, Any] = {"rows": {}, "res": {}, "res_on": {}, "hl": {}}

    def entry(parent: tk.Misc, width: int, vc: Any, text: str = "",
              justify: Literal["left", "center", "right"] = "right") -> tk.Entry:
        e = tk.Entry(parent, width=width, justify=justify, validate="key", validatecommand=vc,
                     font=T.FONT_NUM_LG, disabledforeground="black",
                     disabledbackground="#f0f0f0")
        if text:
            e.insert(0, text)
        return e

    def head(parent: tk.Misc, text: str, width: int = 0) -> tk.Label:
        lbl = tk.Label(parent, text=text, fg="gray25", font=T.FONT_LABEL)
        if width:
            lbl.config(width=width)
        return lbl

    # ===== 윗줄: 종목 · 월물(최근/차근) · HL 호가단위 · 적 (ver2 A20, 기준수량 없음 — 답 5) =====
    top = tk.Frame(root)
    top.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=(6, 2))  # 0행 = 지연 띠
    w["under"] = ttk.Combobox(top, values=[NAMES[u] for u in UNDERLYINGS], width=7,
                              state="readonly")
    w["under"].set(NAMES[UNDERLYINGS[0]])
    w["under"].pack(side="left", padx=(0, 4))
    w["month"] = ttk.Combobox(top, values=["최근", "차근"], width=4, state="readonly")
    w["month"].set("최근")
    w["month"].pack(side="left", padx=(0, 4))
    w["agg"] = ttk.Combobox(top, values=["-"], width=6, state="readonly")
    w["agg"].set("-")
    w["agg"].pack(side="left", padx=(0, 4))
    w["apply"] = ttk.Button(top, text="적", width=3)
    w["apply"].pack(side="left", padx=(0, 8))
    w["sf_exp"] = tk.Label(top, text="주식선물 예상체결가 -", font=T.FONT_LABEL, fg="gray25")
    w["sf_exp"].pack(side="left", padx=(0, 8))
    w["fx_exp"] = tk.Label(top, text="환 예상체결가 -", font=T.FONT_LABEL, fg="gray25")
    w["fx_exp"].pack(side="left")

    # ===== 왼쪽: 예약주문 16행 (ver2 A22~D38) =====
    left = tk.Frame(root)
    left.grid(row=2, column=0, sticky="nw", padx=(6, 4), pady=(2, 0))
    heads = ("주문Sprd", "수량", "예상주문가", "예약주문")
    head_w = (8, 6, 10, 10)
    for c, (h, hw) in enumerate(zip(heads, head_w, strict=True)):
        head(left, h, hw).grid(row=0, column=c, padx=1, sticky="nsew")
    probe = tk.Button(left, text="진입", width=3, padx=0, pady=0, bd=1, highlightthickness=0)
    row_h = int((probe.winfo_reqheight() + 2) * 0.8)  # 체결쏴 세트 행보다 20% 낮게(사용자 10-23)
    probe.destroy()
    # 실제 행 높이는 값 칸(큰 숫자 글꼴 라벨)의 자연 높이가 정해 왔다(≈26px) — 예상주문가에 테두리를
    # 넣어도(10-23) 그 높이를 그대로 지킨다
    probe_lbl = tk.Label(left, text="0", font=T.FONT_NUM_LG)
    row_h = max(row_h, probe_lbl.winfo_reqheight())
    probe_lbl.destroy()

    def paint_res(key: tuple[str, int]) -> None:
        on = bool(w["res_on"].get(key))
        # 색은 버튼 글의 HL 방향 기준(사용자 2026-10-23): 정(HL매도) 파랑, 역(HL매수) 빨강
        color = T.C_SELL if key[0] == "buy" else T.C_BUY
        # 평소엔 글자만 그 색(파랑/빨강), 켜지면 바탕이 그 색·글자 흰색(사용자 2026-10-26)
        bg = color if on else "SystemButtonFace"
        fg = "white" if on else color
        w["res"][key].config(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
        for k in ("spread", "qty"):  # 켜진 행은 입력 잠금, 끄면 풀림(사용자 2026-10-26)
            w["rows"][key][k].config(state="disabled" if on else "normal")
        # 한 행이라도 켜져 있으면 월물·호가단위 콤보와 '적'을 잠근다(종목 콤보는 둔다 — 체결쏴와
        # 같음)
        locked = any_reserved(w["res_on"])
        for k in ("month", "agg"):
            w[k].config(state="disabled" if locked else "readonly")
        w["apply"].config(state="disabled" if locked else "normal")
        # LS주식선물 주문시각 줄(시각·ms·인터벌)도 같이 잠근다(사용자 2026-10-26)
        for k in ("ls_hms", "ls_ms", "ls_itv"):
            w[k].config(state="disabled" if locked else "normal")

    def toggle_res(key: tuple[str, int]) -> None:
        name = f"{'매수' if key[0] == 'buy' else '매도'} {key[1] + 1}행"
        log.info("[동시호가 화면] 예약 버튼 클릭 %s (지금 %s)", name,
                 "켬" if w["res_on"].get(key) else "끔")  # 멈춤 조사용(2026-10-26)
        if not w["res_on"].get(key):  # 켜려면 Sprd·수량이 있어야(사용자 2026-10-26)
            cells = w["rows"][key]
            missing = row_missing(cells["spread"].get(), cells["qty"].get())
            if missing:
                w["status"].show(f"{name} 예약 거부: {missing} 없음")
                show_message(root, "동시호가 주문",
                             f"{name}에 {missing}이(가) 없습니다.\n"
                             "주문Sprd와 수량을 채워야 예약을 켤 수 있습니다.")
                return
        w["res_on"][key] = not bool(w["res_on"].get(key))
        paint_res(key)
        text = (f"{'매수' if key[0] == 'buy' else '매도'} {key[1] + 1}행 예약 "
                f"{'켬' if w['res_on'][key] else '끔'}")
        log.info("[동시호가 화면] %s", text)
        w["status"].show(text)

    r = 1
    for side, samples in (("buy", SAMPLE_BUY), ("sell", SAMPLE_SELL)):
        if side == "sell":
            tk.Frame(left, height=6).grid(row=r, column=0, columnspan=4)  # 매수·매도 사이 빈 줄
            r += 1
        for i in range(ROWS_PER_SIDE):
            key = (side, i)
            cells: dict[str, Any] = {}
            sp, qty = samples[i] if preview else ("", "")
            cells["spread"] = entry(left, 6, vcmd_dec, sp)
            cells["qty"] = entry(left, 6, vcmd_qty, qty)
            # 예상주문가는 테두리 없는 글자만 — 테두리를 넣어 봤더니 행 간격이 0이라 위아래 선이
            # 겹쳐 두꺼워 보여 되돌림(사용자 2026-10-23)
            cells["px"] = tk.Label(left, text="-", width=10, anchor="e", fg="gray25",
                                   font=T.FONT_NUM_LG)
            for c, k in enumerate(("spread", "qty", "px")):
                cells[k].grid(row=r, column=c, padx=1, pady=0, sticky="nsew")
            # 행 높이는 버튼이 정한다(입력칸 24px보다 큼) — 버튼 글꼴을 한 단계 작게(10pt) 해서
            # 행을 낮춘다(사용자 2026-10-23 "높이 10% 더")
            idle_fg = T.C_SELL if side == "buy" else T.C_BUY  # 평소 글자색(사용자 2026-10-26)
            btn = tk.Button(left, text=side_label(side), width=10, padx=0, pady=0, bd=1,
                            highlightthickness=0, font=(T.FAMILY, 10), fg=idle_fg,
                            activeforeground=idle_fg, command=partial(toggle_res, key))
            btn.grid(row=r, column=3, padx=(4, 1), pady=0, sticky="nsew")
            w["rows"][key] = cells
            w["res"][key] = btn
            left.grid_rowconfigure(r, minsize=row_h)
            r += 1

    # ===== 오른쪽 — 세 묶음이 이름 칸(0열) 세로선만 공유하고 값 칸은 자연 폭으로 왼쪽 정렬 =====
    # (사용자 2026-10-23: 값 칸까지 폭을 맞추니 컨트롤이 너무 벌어짐 → 이름 칸만 맞추고 나머지는
    # 왼쪽에 붙여 촘촘하게)
    right = tk.Frame(root)
    right.grid(row=2, column=1, sticky="nw", padx=(8, 6), pady=(2, 0))
    NAME_W = 82   # 이름 칸(논리 픽셀) — "LS주식선물"이 들어가는 폭
    GAP = (0, 6)  # 값 칸 사이 간격

    def section(title: str, row: int, name_col: bool = True) -> tk.LabelFrame:
        box = tk.LabelFrame(right, text=title, fg="gray25", font=T.FONT_LABEL, padx=4, pady=2)
        box.grid(row=row, column=0, sticky="ew", pady=(0 if row == 0 else 6, 0))
        if name_col:  # 이름 칸은 주문시각에만 — 다른 묶음은 왼쪽에 빈 공간 없이(사용자 10-23)
            box.grid_columnconfigure(0, minsize=NAME_W)
        return box

    def put(widget: tk.Widget, row: int, col: int, **kw: Any) -> None:
        widget.grid(row=row, column=col, padx=GAP, pady=kw.pop("pady", 1),
                    sticky=kw.pop("sticky", "w"), **kw)

    # --- 주문시각 6칸 (ver2 G20~K22, 사용자 2026-10-22 답 4 "6개는 사용자 입력칸") ---
    tm = section("주문시각", 0)
    for c, h in enumerate(("주문시각", "ms", "인터벌(ms)"), start=1):
        put(head(tm, h), 0, c, pady=0)
    for rr, (name, pre) in enumerate((("LS주식선물", "ls"), ("원달러선물", "fx")), start=1):
        tk.Label(tm, text=name, anchor="w").grid(row=rr, column=0, sticky="w")
        w[f"{pre}_hms"] = entry(tm, 9, vcmd_time, DEFAULTS[f"{pre}_hms"], "center")
        w[f"{pre}_ms"] = entry(tm, 5, vcmd_int, DEFAULTS[f"{pre}_ms"])
        w[f"{pre}_itv"] = entry(tm, 5, vcmd_int, DEFAULTS[f"{pre}_itv"])
        for c, k in enumerate(("hms", "ms", "itv"), start=1):
            put(w[f"{pre}_{k}"], rr, c)

    # --- 원달러 대응(프로세스2) (ver2 F26~K27) — 배치는 사용자 델파이 목업 Form2(2026-10-23 오후,
    # "정렬과 배치만 참고"): 왼쪽 이름(오른쪽 맞춤)+입력칸 세 줄(틱·헤지비율·1회주문수량), 오른쪽은
    # 첫 줄 [예약실행], 둘째·셋째 줄 환예상체결가·진입계약수 이름+값. 대응개시 체크는 뺌(10-23) ---
    p2 = section("원달러 대응", 1, name_col=False)
    w["tick"] = entry(p2, 7, vcmd_int, DEFAULTS["tick"])
    w["hedge_pct"] = entry(p2, 7, vcmd_int, DEFAULTS["hedge_pct"])
    w["fx_unit"] = entry(p2, 7, vcmd_int, DEFAULTS["fx_unit"])
    for rr, (name, k) in enumerate((("틱", "tick"), ("헤지비율", "hedge_pct"),
                                    ("1회주문수량", "fx_unit"))):
        tk.Label(p2, text=name, anchor="e").grid(row=rr, column=0, sticky="e", padx=(0, 2))
        put(w[k], rr, 1)
    w["fx_run"] = tk.Button(p2, text="예약실행", width=8, padx=0, pady=0, bd=1,
                            highlightthickness=0)
    w["fx_run"].grid(row=0, column=2, columnspan=2, padx=GAP, pady=1, sticky="ew")
    w["fx_exp2"] = tk.Label(p2, text="-", width=9, anchor="e", font=T.FONT_NUM_LG)
    w["net_qty"] = tk.Label(p2, text="-", width=9, anchor="e", font=T.FONT_NUM_LG)
    for rr, (name, k) in enumerate((("환예상체결가", "fx_exp2"), ("진입계약수", "net_qty")),
                                   start=1):
        tk.Label(p2, text=name, anchor="w").grid(row=rr, column=2, sticky="w")
        w[k].grid(row=rr, column=3, sticky="e", padx=GAP)

    # --- HL주문설정(프로세스3) a~d (ver2 F31~K35) — 버튼 글 "정정"·"취소"(사용자 2026-10-23) ---
    p3 = section("HL주문설정", 2, name_col=False)
    for c, h in enumerate(("주문설정", "정정PT"), start=1):
        put(head(p3, h), 0, c, pady=0)
    w["hl_run"] = tk.Button(p3, text="예약실행", width=8, padx=0, pady=0, bd=1,
                            highlightthickness=0)
    # 머리글 줄의 정정·취소 자리를 가로로 꽉 채운다(목업 Form2)
    w["hl_run"].grid(row=0, column=3, columnspan=2, padx=GAP, pady=(0, 2), sticky="ew")

    def amend_row(name: str) -> None:
        """정정 버튼 — 정정PT가 없으면 알림으로 막는다(사용자 2026-10-26). 실제 정정은 코어 연결
        뒤."""
        if pt_missing(w["hl"][name]["pt"].get()):
            w["status"].show(f"정정 {name} 거부: 정정PT 없음")
            show_message(root, "동시호가 주문", f"{name} 행의 정정PT가 없습니다." + chr(10)
                         + "정정PT(0보다 큰 USD)를 채워야 정정할 수 있습니다.")
            return
        w["status"].show(f"정정 {name} — 코어 연결 전(화면 초안)")

    for rr, name in enumerate(HL_ROWS, start=1):
        tk.Label(p3, text=name, width=2, anchor="w").grid(row=rr, column=0, sticky="w")
        off = entry(p3, 6, vcmd_dec)
        pt = entry(p3, 6, vcmd_dec)
        put(off, rr, 1)
        put(pt, rr, 2)
        amend = tk.Button(p3, text="정정", width=5, padx=0, pady=0, bd=1,
                          highlightthickness=0, state="disabled",
                          command=partial(amend_row, name))
        cancel = tk.Button(p3, text="취소", width=5, padx=0, pady=0, bd=1,
                           highlightthickness=0, state="disabled")
        put(amend, rr, 3, sticky="ew")   # 정정·취소가 예약실행 폭을 반씩 채운다(목업 Form2)
        put(cancel, rr, 4, sticky="ew")
        w["hl"][name] = {"off": off, "pt": pt, "amend": amend, "cancel": cancel}

    # --- 두 묶음의 세로선 맞추기(사용자 2026-10-23 "정렬이 안 되어 있네", 목업 Form2): 왼쪽(이름+
    # 입력칸) 끝과 오른쪽(예약실행·정정/취소) 시작을 같은 x에 — 칸 수가 달라(2 vs 3, 2 vs 2) grid의
    # uniform으로는 안 되니 자연 폭을 잰 뒤 좁은 쪽 마지막 칸을 minsize로 늘린다. 그러면 예약실행
    # 둘의 폭도 같아진다.
    right.update_idletasks()
    # HL주문설정 묶음의 윗선(제목 글 가운데를 지나는 테두리)을 왼쪽 첫 역(HL매수) 버튼의 위에
    # 맞춘다(사용자 2026-10-23 빨간 줄) — 위 묶음 간격(pady)을 그만큼 늘린다. 이미 더 아래면 그대로.
    from tkinter import font as tkfont

    def y_in_root(widget: tk.Misc) -> int:
        """창 기준 y — 창이 아직 안 떠서(winfo_rooty 0) 부모 사슬의 winfo_y를 더한다."""
        y = 0
        cur: tk.Misc | None = widget
        while cur is not None and cur is not root:
            y += cur.winfo_y()
            cur = cur.master
        return y

    title_half = tkfont.Font(font=T.FONT_LABEL).metrics("linespace") // 2
    gap = y_in_root(w["res"][("sell", 0)]) - (y_in_root(p3) + title_half)
    if gap > 0:
        p3.grid_configure(pady=(6 + gap, 0))

    def part_w(box: tk.LabelFrame, cols: tuple[int, ...]) -> int:
        widths = []
        for c in cols:
            bbox = box.grid_bbox(c, 0)
            widths.append(int(bbox[2]) if bbox else 0)
        return sum(widths)

    # 주문시각 입력칸의 왼쪽도 원달러 대응 입력칸과 같은 선에(사용자 2026-10-23 빨간 줄)
    name_w = max(NAME_W, part_w(tm, (0,)), part_w(p2, (0,)))
    tm.grid_columnconfigure(0, minsize=name_w)
    p2.grid_columnconfigure(0, minsize=name_w)
    left_w = max(part_w(p2, (0, 1)), part_w(p3, (0, 1, 2)))
    p2.grid_columnconfigure(1, minsize=left_w - part_w(p2, (0,)))
    p3.grid_columnconfigure(2, minsize=left_w - part_w(p3, (0, 1)))
    right_w = max(part_w(p2, (2, 3)), part_w(p3, (3, 4)))
    p2.grid_columnconfigure(3, minsize=right_w - part_w(p2, (2,)))
    p3.grid_columnconfigure(3, minsize=right_w // 2)      # 정정·취소 반반 → 예약실행과 같은 폭
    p3.grid_columnconfigure(4, minsize=right_w - right_w // 2)

    def paint_run(key: str, on: bool) -> None:
        bg = T.C_BUY if on else "SystemButtonFace"
        fg = "white" if on else "black"
        w[key].config(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)

    run_on: dict[str, bool] = {"fx_run": False, "hl_run": False}

    def toggle_run(key: str) -> None:
        if key == "hl_run" and not run_on[key]:  # 주문설정은 비면 0(§5) — 숫자 아닌 글만 막는다
            missing = hl_missing({n: w["hl"][n]["off"].get() for n in HL_ROWS})
            if missing:
                w["status"].show(f"HL 예약실행 거부: 주문설정 {missing} 숫자 아님")
                show_message(root, "동시호가 주문",
                             f"HL주문설정 {missing} 행의 주문설정이 숫자가 아닙니다." + chr(10)
                             + "USD 오프셋(비우면 0)을 넣어야 예약실행을 켤 수 있습니다.")
                return
        if key == "fx_run" and not run_on[key]:  # 켜려면 틱·헤지비율·1회주문수량이 있어야(10-26)
            missing = fx_missing(w["tick"].get(), w["hedge_pct"].get(), w["fx_unit"].get())
            if missing:
                w["status"].show(f"환 예약실행 거부: {missing} 없음")
                show_message(root, "동시호가 주문",
                             f"원달러 대응에 {missing}이(가) 없습니다." + chr(10)
                             + "틱·헤지비율·1회주문수량을 채워야 예약실행을 켤 수 있습니다.")
                return
        run_on[key] = not run_on[key]
        paint_run(key, run_on[key])
        if key == "fx_run":  # 환 예약실행 켜면 틱·헤지비율·1회주문수량 + 원달러선물 주문시각 줄
            for k in ("tick", "hedge_pct", "fx_unit", "fx_hms", "fx_ms", "fx_itv"):  # 잠금(10-26)
                w[k].config(state="disabled" if run_on[key] else "normal")
        text = (f"{'환 예약실행' if key == 'fx_run' else 'HL 예약실행'} "
                f"{'켬' if run_on[key] else '끔'}")
        log.info("[동시호가 화면] %s", text)
        w["status"].show(text)

    w["fx_run"].config(command=partial(toggle_run, "fx_run"))
    w["hl_run"].config(command=partial(toggle_run, "hl_run"))

    # ===== 상태줄 = 읽기 전용 콤보(체결쏴·NXT 개장과 같음, 사용자 2026-10-23) — 글 앞에 시각,
    # 펼치면 지난 글 50건. 예약·예약실행 켬/끔도 여기에 남긴다.
    status_box = StatusCombo(root, T.FONT_LABEL)
    status_box.widget.grid(row=3, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 6))
    # LS 처리 지연 띠(사용자 2026-10-26) — 코어 연결(§9-3) 뒤 /state의 ws로 켜고 끈다. 지금은 숨김.
    w["banner"] = StallBanner(root, (T.FAMILY, 10, "bold"),
                              lambda wd: wd.grid(row=0, column=0, columnspan=2, sticky="ew"))
    root.grid_columnconfigure(1, weight=1)
    status_box.show("-")
    w["status"] = status_box  # 예약 버튼 콜백이 쓴다(버튼은 상태줄보다 먼저 만들어짐)
    if preview:
        status_box.show("UI 미리보기 — 코어 미연결")
        for i in (0, 2):  # 켜진 행 모양 확인용
            w["res_on"][("buy", i)] = True
            paint_res(("buy", i))
        w["res_on"][("sell", 1)] = True
        paint_res(("sell", 1))
        w["sf_exp"].config(text="주식선물 예상체결가 1,766,000")
        w["fx_exp"].config(text="환 예상체결가 1,327.30")
        w["fx_exp2"].config(text="1,327.30")
        w["net_qty"].config(text="+35")
        for (side, i), cells in w["rows"].items():
            sign = 1 if side == "buy" else -1
            cells["px"].config(text=f"{1_766_000 + (3 - i) * 1000 * sign:,}")
    root.mainloop()


if __name__ == "__main__":
    main()
