"""동시호가 주문 화면(DESIGN-auction-order.md §1·§2·§4·§5, ver2 2026-10-22) — 화면 먼저(코어 연결
전).

배치는 엑셀 ver2 셀 자리를 따른다: 윗줄(종목·월물·호가단위·적) / 왼쪽 예약주문 16행(매도 8 위·
매수 8 아래: 주문Sprd·수량·예상주문가·예약주문 버튼) / 오른쪽 주문시각 6칸 → 원달러 대응 설정 행 →
HL주문설정 a~d.
`--preview`로 코어 없이 뜬다. 코어 연결(§9-3, 2026-10-26): 행·설정은 예약 버튼·예약실행·정정·'적'을
누를 때와 종목 바꿀 때·닫을 때 코어로(auction_rows / auction_settings — 체결쏴 결정 74 방식),
예약실행은 auction_run, 정정·취소는 auction_amend/auction_cancel.
표시(예상체결가·예상주문가·발주 결과·환 분할·HL 행·진입계약수)는 /state의 auction_live.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from .domain.enums import Underlying

UNDERLYINGS = (Underlying.SK_HYNIX, Underlying.SAMSUNG)
NAMES = {Underlying.SK_HYNIX: "하이닉스", Underlying.SAMSUNG: "삼성"}
BY_NAME = {v: k for k, v in NAMES.items()}
ROWS_PER_SIDE = 8                      # 매수 8행 + 매도 8행 고정(ver2 P24)
HOLD_S = 2.0   # 버튼을 누른 뒤 이 동안은 화면 값을 지키고 코어 상태로 덮지 않는다(깜박임 방지)
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
    """예약주문 버튼 글 — 매도 행 "LS매도", 매수 행 "LS매수"(사용자 2026-10-29; ver2 D23·D31의
    "정(HL매도)"/"역(HL매수)"는 버림)."""
    return "LS매수" if side == "buy" else "LS매도"


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


def saved_under(saved: Mapping[str, Any]) -> str | None:
    """창 저장(win_state fields)의 보던 종목 이름 — 콤보에 있는 이름일 때만(제외된 종목 무시).
    NXT 개장·체결쏴와 같은 요령(사용자 2026-10-28 "종목 콤보가 저장이 안 되네")."""
    name = saved.get("under")
    return name if isinstance(name, str) and name in BY_NAME else None


def fx_missing(tick_text: str, hedge_text: str, unit_text: str) -> str | None:
    """환 예약실행을 켜기 전 검사(사용자 2026-10-26) — 빠진 것의 이름("틱"·"헤지비율"·
    "1회주문수량"), 다 있으면 None. 틱은 정수면 됨(음수도 — 사용자 2026-10-27 "틱 마이너스도 입력
    가능하게": 매수는 예상체결가 아래·매도는 위), 헤지비율·1회주문수량은 0보다 커야 한다. 순수."""
    missing = []
    for name, text, min_ok in (("틱", tick_text, None), ("헤지비율", hedge_text, 1),
                               ("1회주문수량", unit_text, 1)):
        try:
            if min_ok is not None and int(text.replace(",", "").strip()) < min_ok:
                missing.append(name)
            elif min_ok is None:
                int(text.replace(",", "").strip())
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
    """정정 버튼 전 검사(사용자 2026-10-26) — 정정PT가 비거나 0이면 True. 음수는 허용(사용자
    2026-10-28 "테스트라 체결 안 되게 정정하려고 — '-'도 입력 가능하게": 상대호가 반대쪽으로).
    순수."""
    try:
        return float(pt_text.strip()) == 0
    except ValueError:
        return True


def rows_payload(u: str, buy: list[tuple[str, str, bool]],
                 sell: list[tuple[str, str, bool]]) -> dict[str, Any]:
    """행 설정 명령(auction_rows) 본문 — (스프레드 글, 수량 글, 켬) 목록 → 숫자. 스프레드 칸은
    **%**(체결쏴·NXT 개장과 같음: 0.3 = 0.3%) → 코어엔 소수(0.003). 빈 글은 None/0. 순수.
    (2026-10-26 시험: 0.1을 그대로 보내 10%로 역산돼 한 건도 안 나감 → %로 고침)"""
    def conv(rows: list[tuple[str, str, bool]]) -> list[dict[str, Any]]:
        out = []
        for sp, qty, on in rows:
            sp_t, q_t = sp.strip(), qty.replace(",", "").strip()
            out.append({"spread": float(sp_t) / 100.0 if sp_t else None,
                        "qty": int(float(q_t)) if q_t else 0, "on": bool(on)})
        return out
    return {"cmd": "auction_rows", "underlying": u, "buy": conv(buy), "sell": conv(sell)}


def settings_payload(u: str, texts: Mapping[str, str], month: str,
                     hl_off: Mapping[str, str], hl_pt: Mapping[str, str]) -> dict[str, Any]:
    """설정 명령(auction_settings) 본문 — 시각 칸은 글 그대로, 숫자 칸은 int, HL 오프셋·정정PT는
    float(비면 0). 순수."""
    def num(text: str, default: int) -> int:
        t = text.replace(",", "").strip()
        try:
            return int(float(t)) if t else default
        except ValueError:  # '-'만 쳐 둔 틱 칸 등
            return default

    def fnum(text: str) -> float:
        t = text.strip()
        return float(t) if t else 0.0
    return {"cmd": "auction_settings", "underlying": u, "month": month,
            "ls_hms": texts["ls_hms"].strip(), "ls_ms": num(texts["ls_ms"], 0),
            "ls_itv_ms": num(texts["ls_itv"], 0),
            "fx_hms": texts["fx_hms"].strip(), "fx_ms": num(texts["fx_ms"], 0),
            "fx_itv_ms": num(texts["fx_itv"], 0),
            "tick": num(texts["tick"], 0), "hedge_pct": num(texts["hedge_pct"], 0),
            "fx_unit": num(texts["fx_unit"], 0),
            "hl_off": {k: fnum(v) for k, v in hl_off.items()},
            "hl_pt": {k: fnum(v) for k, v in hl_pt.items()}}


def book_texts(book: Mapping[str, Any]) -> dict[str, str]:
    """코어 종목 상태(auction.books[u]) → 설정 칸 글. 순수."""
    st = dict(book.get("settings") or {})

    def g(key: str, default: str) -> str:
        v = st.get(key)
        return default if v in (None, "") else str(v)
    return {"ls_hms": g("ls_hms", DEFAULTS["ls_hms"]), "ls_ms": g("ls_ms", DEFAULTS["ls_ms"]),
            "ls_itv": g("ls_itv_ms", DEFAULTS["ls_itv"]),
            "fx_hms": g("fx_hms", DEFAULTS["fx_hms"]), "fx_ms": g("fx_ms", DEFAULTS["fx_ms"]),
            "fx_itv": g("fx_itv_ms", DEFAULTS["fx_itv"]),
            "tick": g("tick", DEFAULTS["tick"]), "hedge_pct": g("hedge_pct", DEFAULTS["hedge_pct"]),
            "fx_unit": g("fx_unit", DEFAULTS["fx_unit"])}


def row_texts(row: Mapping[str, Any]) -> tuple[str, str, bool]:
    """코어 행(스프레드 소수) → (스프레드 % 글, 수량 글, 켬). 순수."""
    sp, qty = row.get("spread"), row.get("qty")
    return (("" if sp is None else f"{round(float(sp) * 100.0, 6):g}"),
            ("" if not qty else f"{int(float(qty))}"), bool(row.get("on")))


def num_text(v: float | None) -> str:
    """오프셋·정정PT 글 — 0은 빈칸. 순수."""
    return "" if not v else f"{float(v):g}"


def px_text(v: float | None) -> str:
    return "-" if v is None else f"{v:,.0f}"


def pct2(frac: float | None) -> str:
    """모니터 스프레드 — 소수 → % 두 자리(체결쏴 모니터와 같은 모양). 없으면 "-"."""
    return "-" if frac is None else f"{frac * 100:.2f}"


def result_line(side: str, i: int, r: Mapping[str, Any]) -> str:
    """행 발주 결과 한 줄(상태줄) — "매수1 발주 #123 20@1,767,000" / "매수3 실패 …". 순수."""
    name = f"{'매수' if side == 'buy' else '매도'}{i + 1}"
    if r.get("error"):
        return f"{name} 실패 {r['error']}"
    if r.get("skipped"):
        return f"{name} 안 냄 — {r['skipped']}"
    return f"{name} 발주 #{r.get('order_id')} {r.get('qty')}@{px_text(r.get('price'))}"


def hl_line(name: str, row: Mapping[str, Any]) -> str:
    """HL 행 상태 한 줄 — "HL a 접수 #9 7.5@1333.0 잔량 7.5". 순수."""
    st = str(row.get("status") or "")
    if st == "실패":
        return f"HL {name} 실패 {row.get('error')}"
    rem = row.get("remaining")
    tail = f" 잔량 {rem:g}" if isinstance(rem, int | float) else ""
    return (f"HL {name} {st} #{row.get('order_id')} {row.get('qty')}"
            f"@{row.get('price')}{tail}").replace("  ", " ")


def px_style(row: Mapping[str, Any] | None, fires: bool) -> tuple[str, str]:
    """예상주문가 칸 (글자색, 바탕색) — 발주 초록·실패 빨강·안 냄 주황, 아직 안 본 행 중 지금 값으로
    나갈 행은 **흰 바탕**(사용자 2026-10-26 "나갈 거로 예상되는 행에 색" → 2026-10-27 "바탕색을
    흰색으로" — 연두에서 바꿈), 그 밖엔 회색. 순수."""
    if row:
        if row.get("error"):
            return "red", "SystemButtonFace"
        if row.get("skipped"):
            return "dark orange", "SystemButtonFace"
        return "dark green", "SystemButtonFace"
    return ("black", "white") if fires else ("gray25", "SystemButtonFace")


def hl_fill_text(info: Mapping[str, Any] | None) -> str:
    """HL주문설정 아래 'HL체결' 한 줄(§5 체결 표시, 사용자 2026-10-29) — "매도 30/30 평균 1332.2
    완료". 낸 주문이 없으면 "-", 체결 전이면 "매도 0/30". 순수."""
    if not info or not info.get("total"):
        return "-"
    total = float(info["total"])
    qty = float(info.get("qty") or 0.0)
    word = {"buy": "매수", "sell": "매도"}.get(str(info.get("side") or ""), "")
    text = f"{word} {qty:g}/{total:g}".strip()
    avg = info.get("avg")
    if isinstance(avg, int | float) and qty > 0:
        text += f" 평균 {avg:g}"
    if qty >= total - 1e-9:
        text += " 완료"
    return text


def hl_buttons_enabled(row: Mapping[str, Any]) -> bool:
    """정정·취소 버튼 — 그 행의 HL 주문이 열려 있을 때만(§5 접수 확인 뒤 활성). 순수."""
    return bool(row.get("open"))


def any_reserved(res_on: Mapping[tuple[str, int], bool]) -> bool:
    """예약주문 행이 하나라도 켜져 있나 — 켜져 있으면 윗줄(월물·호가단위·적)을 잠근다(사용자
    2026-10-26). 순수."""
    return any(bool(v) for v in res_on.values())


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    import os
    import queue
    import sys
    import threading
    import time
    import tkinter as tk
    from functools import partial
    from tkinter import ttk

    from . import ui_theme as T
    from . import win_state
    from .core_client import core_request, run_state_feed, screen_log, watch_parent_exit
    from .ui_close import attach_auto_close, main_closing
    from .ui_dialog import ask_yes_no, show_message
    from .ui_fields import (
        is_decimal_text,
        is_int_text,
        is_signed_int_text,
        is_time_text,
        is_unsigned_decimal_text,
    )
    from .ui_status import StallBanner, StatusCombo, log_tk_errors, ls_stalled

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
    vcmd_sint = (root.register(is_signed_int_text), "%P")  # 틱 — 음수 허용(사용자 2026-10-27)
    vcmd_dec = (root.register(is_decimal_text), "%P")
    vcmd_qty = (root.register(is_unsigned_decimal_text), "%P")
    vcmd_time = (root.register(is_time_text), "%P")
    log = screen_log()
    log_tk_errors(root, log)  # 버튼 콜백 예외를 화면 로그에(콘솔이 없어 안 보임)

    # --- 코어 연결: 전송은 뒷단 스레드(jobs → results), 상태는 run_state_feed(공유메모리/폴링) ---
    jobs: queue.Queue[tuple[dict[str, Any], str]] = queue.Queue()
    results: queue.Queue[tuple[str, dict[str, Any] | None]] = queue.Queue()
    state_box: dict[str, Any] = {"data": None}

    def sender() -> None:
        while True:
            payload, label = jobs.get()
            results.put((label, core_request("/command", payload, timeout=10.0)))
            jobs.task_done()

    def poller() -> None:
        run_state_feed(state_box, log_tag="동시호가", channel="state",
                       fallback_path="/state", poll_s=1.0)

    if not preview:
        threading.Thread(target=sender, daemon=True).start()
        threading.Thread(target=poller, daemon=True).start()

    def send(payload: dict[str, Any], label: str) -> None:
        if preview:
            return
        jobs.put(({**payload, "sent_at": time.time()}, label))

    cur: dict[str, Any] = {"u": UNDERLYINGS[0], "loaded": False,
                           "sent_rows": None, "sent_settings": None, "seen_rows": set(),
                           "seen_hl": {}, "fx_note": "", "note": ""}
    w: dict[str, Any] = {"rows": {}, "res": {}, "res_on": {}, "hl": {}, "hold": {}}

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
    # HL 호가단위 — 코어 공용 값을 글자로만(바꾸는 콤보는 일반주문창 한 곳뿐, 사용자 2026-10-30)
    w["agg"] = tk.Label(top, text="-", fg="black", bg="white", relief="solid", bd=1, width=5)
    w["agg"].pack(side="left", padx=(0, 4))
    w["apply"] = ttk.Button(top, text="적", width=3)
    w["apply"].pack(side="left", padx=(0, 8))
    # 시험 모드(§7a, 사용자 2026-10-26): 장중엔 예상체결가가 없어 주식선물 1호가 중간값·원달러선물
    # 현재가로 대신 — 동시호가 단계에 들어가면 코어가 무시한다. 저장 안 함(매번 켠다).
    w["test_var"] = tk.BooleanVar(value=False)
    w["test"] = tk.Checkbutton(top, text="시험(현재가)", variable=w["test_var"],
                               fg="dark red", font=T.FONT_LABEL)
    # 윗줄 오른쪽 끝 = 현재 시각(PC 시계, 주문시각과 같은 기준 — 사용자 2026-10-26), 그 왼쪽에 시험
    # 체크. 시험 체크와 아래 시험 매수/매도 띠는 **화면에서 뺐다**(사용자 2026-10-29 "테스트
    # 완료했으니 제거" — 10-26 "배포 때 제외" → 10-27 "당분간 포함"의 매듭). 코어 쪽 시험 모드·가상
    # 체결은 그대로 있어 환경변수 KP_AUCTION_TEST=1로 켜면 다시 보인다(개발 전용).
    show_test = os.environ.get("KP_AUCTION_TEST") == "1"
    w["clock"] = tk.Label(top, text="--:--:--", font=T.FONT_NUM_LG, fg="gray25")
    w["clock"].pack(side="right")
    if show_test:
        w["test"].pack(side="right", padx=(0, 10))

    def tick_clock() -> None:
        try:
            w["clock"].config(text=time.strftime("%H:%M:%S"))
            root.after(200, tick_clock)
        except tk.TclError:
            pass  # 창 닫힘

    tick_clock()
    # 모니터 수치(사용자 2026-10-26): 역방향매수(빨강)·정방향매도(파랑) — 주식선물 예상체결가로 센
    # 스프레드(%). 환 예상체결가는 원달러 대응에서 보여 주니 윗줄에서 뺌, 주식선물 예상체결가는
    # 주문시각 묶음 안으로.
    # 체결쏴 모니터와 같은 모양 — 제목 없이 색 상자(역방향매수 빨강, 정방향매도 파랑)에 흰 글씨
    w["mon_rev"] = tk.Label(top, text="-", width=6, anchor="center", font=T.FONT_NUM_LG,
                            bg=T.C_BUY, fg="white")
    w["mon_rev"].pack(side="left", padx=(0, 6))
    w["mon_fwd"] = tk.Label(top, text="-", width=6, anchor="center", font=T.FONT_NUM_LG,
                            bg=T.C_SELL, fg="white")
    w["mon_fwd"].pack(side="left")

    # ===== 왼쪽: 예약주문 16행 (ver2 A22~D38) =====
    left = tk.Frame(root)
    left.grid(row=2, column=0, sticky="nw", padx=(6, 4), pady=(2, 0))
    # '예약주문' 제목 복귀(사용자 2026-10-27 "일괄 버튼 제거하고 예전 제목") — 10-26의 일괄
    # [실행][중지] 버튼은 뺐다.
    heads = ("주문Sprd%", "수량", "예상주문가", "예약주문")
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
    left.grid_rowconfigure(0, minsize=row_h)  # 머리글 줄도 행 높이로

    def paint_res(key: tuple[str, int]) -> None:
        on = bool(w["res_on"].get(key))
        # 색은 버튼 글(LS 쪽) 기준: LS매수 빨강, LS매도 파랑(2026-10-29 글을 LS로 바꾸며 10-23의
        # HL 방향 기준 색도 같이 뒤집음)
        color = T.C_BUY if key[0] == "buy" else T.C_SELL
        # 평소엔 글자만 그 색(파랑/빨강), 켜지면 바탕이 그 색·글자 흰색(사용자 2026-10-26)
        bg = color if on else "SystemButtonFace"
        fg = "white" if on else color
        w["res"][key].config(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
        for k in ("spread", "qty"):  # 켜진 행은 입력 잠금, 끄면 풀림(사용자 2026-10-26)
            w["rows"][key][k].config(state="disabled" if on else "normal")
        # 한 행이라도 켜져 있으면 월물 콤보와 '적'을 잠근다(종목 콤보는 둔다 — 체결쏴와 같음)
        locked = any_reserved(w["res_on"])
        w["month"].config(state="disabled" if locked else "readonly")
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
        w["hold"][key] = time.time() + HOLD_S  # 코어 상태가 올 때까지 화면 값을 지킨다(깜박임 방지)
        paint_res(key)
        text = (f"{'매수' if key[0] == 'buy' else '매도'} {key[1] + 1}행 예약 "
                f"{'켬' if w['res_on'][key] else '끔'}")
        log.info("[동시호가 화면] %s", text)
        w["status"].show(text)
        send_settings()      # 버튼을 누르는 순간의 칸 값이 함께 코어로(결정 74 방식)
        send_rows(force=True)

    r = 1
    # 매도 8행이 위, 매수 8행이 아래 — 호가창 관례(사용자 2026-10-29; ver2 목업은 매수가 위였으나
    # 목업 오류)
    for side, samples in (("sell", SAMPLE_SELL), ("buy", SAMPLE_BUY)):
        if side == "buy":
            tk.Frame(left, height=6).grid(row=r, column=0, columnspan=4)  # 매도·매수 사이 빈 줄
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
            idle_fg = T.C_BUY if side == "buy" else T.C_SELL  # 평소 글자색(LS 기준, 10-29)
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
    # 주식선물 예상체결가 — 주문시각 묶음 안(사용자 2026-10-26)
    tk.Label(tm, text="예상체결가", anchor="w").grid(row=3, column=0, sticky="w")
    w["sf_exp"] = tk.Label(tm, text="-", anchor="w", font=T.FONT_NUM_LG)
    w["sf_exp"].grid(row=3, column=1, columnspan=3, sticky="w", padx=GAP)

    # --- 원달러 대응(프로세스2) (ver2 F26~K27) — 배치는 사용자 델파이 목업 Form2(2026-10-23 오후,
    # "정렬과 배치만 참고"): 왼쪽 이름(오른쪽 맞춤)+입력칸 세 줄(틱·헤지비율·1회주문수량), 오른쪽은
    # 첫 줄 [예약실행], 둘째·셋째 줄 환예상체결가·진입계약수 이름+값. 대응개시 체크는 뺌(10-23) ---
    p2 = section("원달러 대응", 1, name_col=False)
    w["tick"] = entry(p2, 7, vcmd_sint, DEFAULTS["tick"])
    w["hedge_pct"] = entry(p2, 7, vcmd_int, DEFAULTS["hedge_pct"])
    w["fx_unit"] = entry(p2, 7, vcmd_int, DEFAULTS["fx_unit"])
    for rr, (name, k) in enumerate((("틱", "tick"), ("헤지비율", "hedge_pct"),
                                    ("1회주문수량", "fx_unit"))):
        tk.Label(p2, text=name, anchor="e").grid(row=rr, column=0, sticky="e", padx=(0, 2))
        put(w[k], rr, 1)
    w["fx_run"] = tk.Button(p2, text="예약실행", width=8, padx=0, pady=0, bd=1,
                            highlightthickness=0)
    w["fx_run"].grid(row=0, column=2, columnspan=2, padx=GAP, pady=1, sticky="ew")
    # 너비 고정 없음 — 시험 모드의 "(현재가 대용)" 꼬리까지 들어가야 한다(2026-10-27: width=9에
    # 오른쪽 정렬이라 숫자가 왼쪽으로 잘려 "현재가 대용)"만 보였음)
    w["fx_exp2"] = tk.Label(p2, text="-", anchor="e", font=T.FONT_NUM_LG)
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
            log.info("[동시호가 화면] 정정 %s 거부 — 정정PT 없음(%r)", name,
                     w["hl"][name]["pt"].get())
            w["status"].show(f"정정 {name} 거부: 정정PT 없음")
            show_message(root, "동시호가 주문", f"{name} 행의 정정PT가 없습니다." + chr(10)
                         + "정정PT(USD, 0이 아닌 값 — 음수는 상대호가 반대쪽)를 채워야 "
                         + "정정할 수 있습니다.")
            return
        log.info("[동시호가 화면] 정정 %s 클릭 — 정정PT %s", name, w["hl"][name]["pt"].get())
        send_settings(force=True)  # 정정PT 최신 값을 코어에 먼저
        send({"cmd": "auction_amend", "underlying": cur["u"].value, "row": name}, f"정정 {name}")

    def cancel_row(name: str) -> None:
        send({"cmd": "auction_cancel", "underlying": cur["u"].value, "row": name}, f"취소 {name}")

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
                           highlightthickness=0, state="disabled",
                           command=partial(cancel_row, name))
        put(amend, rr, 3, sticky="ew")   # 정정·취소가 예약실행 폭을 반씩 채운다(목업 Form2)
        put(cancel, rr, 4, sticky="ew")
        w["hl"][name] = {"off": off, "pt": pt, "amend": amend, "cancel": cancel}

    # --- HL체결 한 줄(§5 체결 표시, 사용자 2026-10-29 "체결수량과 평균체결가를 HL주문설정 아래 남는
    # 공간에"): 이 바퀴 4건의 체결수량 합/낸 수량, 평균체결가, 전량이면 '완료' ---
    fb = tk.Frame(right)
    fb.grid(row=3, column=0, sticky="w", pady=(4, 0))
    tk.Label(fb, text="HL체결", anchor="w", font=T.FONT_LABEL).grid(row=0, column=0, sticky="w")
    w["hl_fill"] = tk.Label(fb, text="-", anchor="w", font=T.FONT_NUM_LG)
    w["hl_fill"].grid(row=0, column=1, sticky="w", padx=GAP)

    # --- 두 묶음의 세로선 맞추기(사용자 2026-10-23 "정렬이 안 되어 있네", 목업 Form2): 왼쪽(이름+
    # 입력칸) 끝과 오른쪽(예약실행·정정/취소) 시작을 같은 x에 — 칸 수가 달라(2 vs 3, 2 vs 2) grid의
    # uniform으로는 안 되니 자연 폭을 잰 뒤 좁은 쪽 마지막 칸을 minsize로 늘린다. 그러면 예약실행
    # 둘의 폭도 같아진다.
    right.update_idletasks()
    # HL주문설정 묶음의 윗선(제목 글 가운데를 지나는 테두리)을 왼쪽 아래 묶음(LS매수) 첫 버튼의 위에
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
    gap = y_in_root(w["res"][("buy", 0)]) - (y_in_root(p3) + title_half)
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
        set_run_state(key, not run_on[key])
        w["hold"][key] = time.time() + HOLD_S
        text = (f"{'환 예약실행' if key == 'fx_run' else 'HL 예약실행'} "
                f"{'켬' if run_on[key] else '끔'}")
        log.info("[동시호가 화면] %s", text)
        w["status"].show(text)
        send_settings(force=True)  # 켜기 직전 값으로 코어 설정을 맞춘 뒤
        send({"cmd": "auction_run", "underlying": cur["u"].value,
              "proc": "fx" if key == "fx_run" else "hl", "value": run_on[key]}, text)

    def set_run_state(key: str, on: bool) -> None:
        """예약실행 표시·잠금 — 코어 상태(auction_live)가 진실, 누른 직후엔 먼저 칠한다."""
        if run_on[key] is on and w.get("_run_painted", {}).get(key) is on:
            return
        run_on[key] = on
        w.setdefault("_run_painted", {})[key] = on
        paint_run(key, on)
        if key == "fx_run":  # 환 예약실행 켜면 틱·헤지비율·1회주문수량 + 원달러선물 주문시각 줄
            for k in ("tick", "hedge_pct", "fx_unit", "fx_hms", "fx_ms", "fx_itv"):  # 잠금(10-26)
                w[k].config(state="disabled" if on else "normal")

    w["fx_run"].config(command=partial(toggle_run, "fx_run"))
    w["hl_run"].config(command=partial(toggle_run, "hl_run"))

    # --- 3단계 시험용 가상 체결(§7a, 사용자 2026-10-28): HL주문설정 아래 오른쪽 — 매수·매도
    # 계약수를 체결된 셈 치고(주식선물 실주문 없음) 확인창을 거쳐 코어 auction_test_order.
    # HL 예약실행이 켜져 있어야 하고, 바로 3단계가 나간다(HL은 실거래). 화면에는 KP_AUCTION_TEST=1일
    # 때만 보인다(사용자 2026-10-29 "테스트 완료했으니 제거").
    tb = tk.Frame(right)
    if show_test:
        tb.grid(row=4, column=0, sticky="e", pady=(4, 0))
    # '시험 허용' 체크 — 켜져 있을 때만 버튼이 살고, 버튼을 누르는 순간 풀린다(사용자 2026-10-28
    # "체크 되어 있을 때만 시험매수/매도, 클릭 시 체크 해제"). 창을 열면 꺼짐, 저장 안 함.
    w["test_allow_var"] = tk.BooleanVar(value=False)
    w["test_btns"] = []

    def sync_test_buttons() -> None:
        st = "normal" if w["test_allow_var"].get() else "disabled"
        for btn in w["test_btns"]:
            btn.config(state=st)

    w["test_allow"] = tk.Checkbutton(tb, text="시험 허용", variable=w["test_allow_var"],
                                     fg="dark red", font=T.FONT_LABEL, command=sync_test_buttons)
    w["test_allow"].grid(row=0, column=0, padx=(0, 4))
    tk.Label(tb, text="시험 수량", font=T.FONT_LABEL, fg="dark red").grid(row=0, column=1)
    w["test_qty"] = entry(tb, 4, vcmd_int, "1")  # 주식선물 계약수(사용자 2026-10-28 "수량 입력")
    w["test_qty"].grid(row=0, column=2, padx=(2, 6))

    def test_order(side: str) -> None:
        if preview or not w["test_allow_var"].get():
            return
        w["test_allow_var"].set(False)  # 한 번 누르면 풀림 — 다음 시험은 다시 체크
        sync_test_buttons()
        word = "매수" if side == "buy" else "매도"
        try:
            qty = int(w["test_qty"].get().strip())
        except ValueError:
            qty = 0
        if qty <= 0:
            show_message(root, "동시호가 주문", "시험 수량(주식선물 계약수)을 1 이상으로 넣으세요.")
            return
        if not ask_yes_no(root, "동시호가 주문",
                          f"{w['under'].get()} 주식선물 {word} {qty}계약이 체결된 셈 칩니다"
                          + "(시험용, 주식선물 실주문 없음).\n"
                          + f"바로 HL {qty * 10}단위의 3단계 헤지가 나갑니다(HL은 실거래).\n"
                          + "HL 예약실행이 켜져 있어야 합니다. 계속할까요?"):
            return
        w["status"].show(f"시험 가상 체결 {word} {qty}계약 보냄")
        send({"cmd": "auction_test_order", "underlying": cur["u"].value, "side": side,
              "qty": qty}, "시험 주문")

    for col, (side_k, text) in enumerate((("buy", "시험 매수"), ("sell", "시험 매도")),
                                         start=3):
        btn = tk.Button(tb, text=text, width=9, padx=0, pady=0, bd=1, highlightthickness=0,
                        fg="dark red", font=T.FONT_LABEL, command=partial(test_order, side_k))
        btn.grid(row=0, column=col, padx=(4, 0))
        w["test_btns"].append(btn)
    sync_test_buttons()

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

    # ===== 코어 연결 — 입력 → 명령, 상태 → 표시 =====
    def set_entry(e: tk.Entry, val: str) -> None:
        """입력칸 글 바꾸기 — 잠긴(disabled) 칸은 프로그램 쓰기도 막히므로 잠깐 풀고 쓴다."""
        locked = str(e.cget("state")) == "disabled"
        if locked:
            e.config(state="normal")
        e.delete(0, "end")
        e.insert(0, val)
        if locked:
            e.config(state="disabled")

    def rows_now() -> tuple[list[tuple[str, str, bool]], list[tuple[str, str, bool]]]:
        def side_rows(side: str) -> list[tuple[str, str, bool]]:
            return [(w["rows"][(side, i)]["spread"].get(), w["rows"][(side, i)]["qty"].get(),
                     bool(w["res_on"].get((side, i)))) for i in range(ROWS_PER_SIDE)]
        return side_rows("buy"), side_rows("sell")

    def send_rows(force: bool = False) -> None:
        """16행을 코어에(Enter·칸 이동·켬/끔 때) — 같은 내용이면 안 보낸다."""
        if not cur["loaded"]:
            return
        try:
            payload = rows_payload(cur["u"].value, *rows_now())
        except ValueError:
            w["status"].show("행 저장 안 함 — 숫자가 아닌 칸이 있습니다")
            return
        key = (payload["buy"], payload["sell"])
        if not force and key == cur["sent_rows"]:
            return
        cur["sent_rows"] = key
        send(payload, "행 저장")

    def settings_now() -> dict[str, Any]:
        texts = {k: w[k].get() for k in ("ls_hms", "ls_ms", "ls_itv", "fx_hms", "fx_ms", "fx_itv",
                                          "tick", "hedge_pct", "fx_unit")}
        month = "next" if w["month"].get() == "차근" else "near"
        return settings_payload(cur["u"].value, texts, month,
                                {n: w["hl"][n]["off"].get() for n in HL_ROWS},
                                {n: w["hl"][n]["pt"].get() for n in HL_ROWS})

    def send_settings(force: bool = False) -> None:
        if not cur["loaded"]:
            return
        try:
            payload = settings_now()
        except ValueError:
            w["status"].show("설정 저장 안 함 — 숫자가 아닌 칸이 있습니다")
            return
        key = {k: v for k, v in payload.items() if k != "cmd"}
        if not force and key == cur["sent_settings"]:
            return
        cur["sent_settings"] = key
        send(payload, "설정 저장")

    # 저장 시점(사용자 2026-10-26 "바꿔" — 체결쏴 결정 74와 같은 방식): Enter·칸 이동은 아무 일도 안
    # 하고, **예약 버튼·예약실행·정정을 누를 때와 종목을 바꿀 때·창을 닫을 때** 그 순간 값이 코어로
    # 간다('적'은 윗줄만). "저장 안 된 값" 알림은 두지 않는다(사용자 2026-10-26 "안 남겨도 됨").
    def apply_top() -> None:
        """'적' — 윗줄만: 월물(체결쏴와 같은 역할; 호가단위는 2026-10-30부터 일반주문창 콤보만).
        설정 칸·행은 예약 버튼·예약실행을 누를 때 간다(사용자 2026-10-26 "'적' 눌러서 저장은 안
        하기로")."""
        send({"cmd": "auction_settings", "underlying": cur["u"].value,
              "month": "next" if w["month"].get() == "차근" else "near"}, "월물")

    w["apply"].config(command=apply_top)

    def toggle_test() -> None:
        on = bool(w["test_var"].get())
        w["hold"]["test"] = time.time() + HOLD_S
        w["status"].show(f"시험 모드(현재가 대용) {'켬' if on else '끔'}")
        send({"cmd": "auction_run", "underlying": cur["u"].value, "proc": "test",
              "value": on}, "시험 모드")

    w["test"].config(command=toggle_test)

    def load_book(book: dict[str, Any]) -> None:
        """코어 종목 상태 → 입력칸·켬 상태(종목을 바꾸거나 처음 받을 때 한 번)."""
        for side_k in ("buy", "sell"):
            rows = list(book.get(side_k) or [])
            for i in range(ROWS_PER_SIDE):
                sp, qty, on = row_texts(rows[i] if i < len(rows) and isinstance(rows[i], dict)
                                        else {})
                cells = w["rows"][(side_k, i)]
                set_entry(cells["spread"], sp)
                set_entry(cells["qty"], qty)
                w["res_on"][(side_k, i)] = on
        for key in w["rows"]:
            paint_res(key)
        texts = book_texts(book)
        for k, v in texts.items():
            set_entry(w[k], v)
        st = dict(book.get("settings") or {})
        for n in HL_ROWS:
            set_entry(w["hl"][n]["off"], num_text((st.get("hl_off") or {}).get(n)))
            set_entry(w["hl"][n]["pt"], num_text((st.get("hl_pt") or {}).get(n)))
        w["month"].set("차근" if book.get("month") == "next" else "최근")
        # 방금 받은 값 = 저장된 값(이것과 다르면 "저장 안 된 값 있음")
        try:
            sent = rows_payload(cur["u"].value, *rows_now())
            cur["sent_rows"] = (sent["buy"], sent["sell"])
            cur["sent_settings"] = {k: v for k, v in settings_now().items() if k != "cmd"}
        except ValueError:
            cur["sent_rows"] = cur["sent_settings"] = None

    def sync_rows_on(book: dict[str, Any]) -> None:
        """켬 상태는 코어가 진실 — 거부돼 되돌아온 행을 화면에 맞춘다(매 갱신, 다를 때만 칠함)."""
        for side_k in ("buy", "sell"):
            rows = list(book.get(side_k) or [])
            for i in range(ROWS_PER_SIDE):
                on = bool(rows[i].get("on")) if i < len(rows) and isinstance(rows[i], dict) \
                    else False
                if (bool(w["res_on"].get((side_k, i))) is not on
                        and time.time() > w["hold"].get((side_k, i), 0.0)):
                    w["res_on"][(side_k, i)] = on
                    paint_res((side_k, i))

    def refresh_agg(live: dict[str, Any]) -> None:
        # 코어가 지금 적용 중인 HL 호가단위(hl_merge_active의 틱 글)를 글자로 — 바뀔 때만
        active = live.get("hl_merge_active")
        label = str(active.get("tick") or "-") if isinstance(active, dict) else "-"
        if w["agg"].cget("text") != label:
            w["agg"].config(text=label)

    def on_under_change(_e: object = None) -> None:
        if cur["loaded"]:
            send_rows()
            send_settings()
        cur["u"], cur["loaded"] = BY_NAME.get(w["under"].get(), UNDERLYINGS[0]), False
        cur["seen_rows"], cur["seen_hl"], cur["fx_note"], cur["note"] = set(), {}, "", ""
        w["agg"].config(text="-")
        for key in ("fx_run", "hl_run"):
            set_run_state(key, False)
        w["test_var"].set(False)

    w["under"].bind("<<ComboboxSelected>>", on_under_change)

    def drain() -> None:
        try:
            while True:
                label, result = results.get_nowait()
                if result is None:
                    w["status"].show(f"{label} 실패 — 코어 미접속")
                elif not result.get("ok"):
                    w["status"].show(f"{label} 거부 — {'; '.join(result.get('errors', []))}")
                elif label not in ("행 저장", "설정 저장"):
                    w["status"].show(f"{label}됨")
        except queue.Empty:
            pass

    def refresh() -> None:
        try:
            data = state_box.get("data") or {}
            w["banner"].set(ls_stalled(data.get("ws") or []))
            u = cur["u"]
            book = ((data.get("auction") or {}).get("books") or {}).get(u.value)
            if not cur["loaded"] and isinstance(book, dict):
                load_book(book)
                cur["loaded"] = True
            elif isinstance(book, dict):
                sync_rows_on(book)
            live = (data.get("auction_live") or {}).get(u.value) or {}
            refresh_agg(live)
            sf, fx = live.get("sf_expected"), live.get("fx_expected")
            tag = " (현재가 대용)" if live.get("testing") else ""
            w["sf_exp"].config(text=f"{px_text(sf)}{tag}")
            if "test" in live and time.time() > w["hold"].get("test", 0.0):
                w["test_var"].set(bool(live.get("test")))  # 코어 상태가 진실(누른 뒤 2초는 화면 값)
            w["fx_exp2"].config(text=(f"{fx:,.2f}" if fx else "-") + tag)
            w["mon_rev"].config(text=pct2(live.get("mon_rev")))
            w["mon_fwd"].config(text=pct2(live.get("mon_fwd")))
            net = live.get("net")
            w["net_qty"].config(text=(f"{'+' if net['side'] == 'buy' else '-'}{net['qty']}"
                                      if net else "-"))
            prices = live.get("prices") or {}
            rows_live = live.get("rows") or {}
            fires = live.get("fires") or {}
            for (side_k, i), cells in w["rows"].items():
                key = f"{side_k}:{i}"
                r = rows_live.get(key)
                # 프로세스1이 본 행(발주·실패·안 냄)은 그때 가격을 그대로 둔다 — 그 뒤 계산은 안 함
                # (사용자 2026-10-26). 아직 안 본 행만 지금 값으로 갱신
                shown = r.get("price") if r and r.get("price") else prices.get(key)
                cells["px"].config(text=px_text(shown))
                fg, bg = px_style(r, bool(fires.get(key)))
                cells["px"].config(fg=fg, bg=bg)
                if r and key not in cur["seen_rows"]:
                    cur["seen_rows"].add(key)
                    w["status"].show(result_line(side_k, i, r))
            for key in ("fx_run", "hl_run"):
                if key in live and time.time() > w["hold"].get(key, 0.0):
                    core_on = bool(live.get(key))
                    if core_on is not run_on[key]:  # 진단(2026-10-26 "끔인데 버튼은 켜져 있음")
                        log.info("[동시호가 화면] %s 표시 %s → 코어 %s로 맞춤", key,
                                 "켬" if run_on[key] else "끔", "켬" if core_on else "끔")
                    set_run_state(key, core_on)
            if live.get("fx_note") and live["fx_note"] != cur["fx_note"]:
                cur["fx_note"] = live["fx_note"]
                w["status"].show(live["fx_note"])
            for n in HL_ROWS:
                row = (live.get("hl") or {}).get(n) or {}
                enabled = hl_buttons_enabled(row)
                for k in ("amend", "cancel"):
                    w["hl"][n][k].config(state="normal" if enabled else "disabled")
                sig = (row.get("order_id"), row.get("status"), row.get("remaining"))
                if row.get("status") and cur["seen_hl"].get(n) != sig:
                    cur["seen_hl"][n] = sig
                    w["status"].show(hl_line(n, row))
            w["hl_fill"].config(text=hl_fill_text(live.get("hl_fill")))
            if live.get("note") and live["note"] != cur["note"]:
                cur["note"] = live["note"]
                w["status"].show(live["note"])
            drain()
            if main_closing():
                root.after(50, on_close)
                return
        except tk.TclError:
            return
        except Exception:  # noqa: BLE001 - 갱신 한 번의 오류로 화면이 멈춘 것처럼 보이지 않게
            log.exception("[동시호가 화면] 갱신 오류")
        root.after(150, refresh)

    def is_running() -> bool:
        return any_reserved(w["res_on"]) or run_on["fx_run"] or run_on["hl_run"]

    def describe_running() -> str:
        parts = []
        n_on = sum(1 for v in w["res_on"].values() if v)
        if n_on:
            parts.append(f"예약 {n_on}행")
        if run_on["fx_run"]:
            parts.append("환 예약실행")
        if run_on["hl_run"]:
            parts.append("HL 예약실행")
        return f"{NAMES[cur['u']]} " + "·".join(parts)

    def send_stop() -> None:
        """닫기 확인 뒤 정지 — 예약 전부 끔 + 예약실행 둘 끔(코어에)."""
        for key in list(w["res_on"]):
            w["res_on"][key] = False
        send_rows(force=True)
        for proc in ("fx", "hl"):
            send({"cmd": "auction_run", "underlying": cur["u"].value, "proc": proc,
                  "value": False}, "예약실행 끔")
        run_on["fx_run"] = run_on["hl_run"] = False

    # --- 창 저장/복원(win_state, NXT 개장·체결쏴와 같은 요령) — 보던 종목(사용자 2026-10-28 "종목
    # 콤보가 저장이 안 되네"). 위치는 attach가 2초마다. 행·설정은 코어가 종목별로 들고 있어 제외.
    def collect_fields() -> dict[str, Any]:
        return {"under": w["under"].get()}

    def restore_saved() -> None:
        name = saved_under(win_state.saved_fields("auction_order"))
        if name is None or name == w["under"].get():
            return
        w["under"].set(name)  # on_under_change를 부르지 않는다 — 아직 받은 값이 없어 저장할 게 없음
        cur["u"], cur["loaded"] = BY_NAME[name], False

    def persist_fields() -> None:
        try:
            win_state.save_fields("auction_order", collect_fields())
            root.after(2000, persist_fields)
        except tk.TclError:
            pass  # 창 닫힘

    if not preview:
        restore_saved()
        root.after(2000, persist_fields)

    def before_close() -> None:
        if preview:
            return
        win_state.save_fields("auction_order", collect_fields())
        if cur["loaded"]:
            send_rows()
            send_settings()
        deadline = time.time() + 3.0
        while jobs.unfinished_tasks and time.time() < deadline:
            time.sleep(0.05)

    on_close = attach_auto_close(root, title="동시호가 주문", is_running=is_running,
                                 send_stop=send_stop, set_status=w["status"].show,
                                 before_close=before_close, skip_confirm=main_closing,
                                 describe_running=describe_running)
    if not preview:
        root.after(150, refresh)
    if preview:
        status_box.show("UI 미리보기 — 코어 미연결")
        for i in (0, 2):  # 켜진 행 모양 확인용
            w["res_on"][("buy", i)] = True
            paint_res(("buy", i))
        w["res_on"][("sell", 1)] = True
        paint_res(("sell", 1))
        w["sf_exp"].config(text="1,766,000")
        w["fx_exp2"].config(text="1,327.30")
        w["mon_rev"].config(text="-0.12")
        w["mon_fwd"].config(text="0.21")
        w["net_qty"].config(text="+35")
        w["hl_fill"].config(text=hl_fill_text({"side": "sell", "qty": 22.5, "total": 30.0,
                                               "avg": 1332.2}))
        for (side, i), cells in w["rows"].items():
            sign = 1 if side == "buy" else -1
            cells["px"].config(text=f"{1_766_000 + (3 - i) * 1000 * sign:,}")
            # 켜진 행 중 "나갈 행"(흰 바탕)·발주 끝(초록 글자) 모양도 미리보기에 — 매수1 나갈 행,
            # 매수3 발주됨, 매도2 안 냄(주황)
            sample = {("buy", 0): px_style(None, True),
                      ("buy", 2): px_style({"order_id": "O1"}, False),
                      ("sell", 1): px_style({"skipped": "안 맞음"}, False)}.get((side, i))
            if sample is not None:
                cells["px"].config(fg=sample[0], bg=sample[1])
    root.mainloop()


if __name__ == "__main__":
    main()
