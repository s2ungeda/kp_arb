"""오전 NXT 개장 매매 화면 — DESIGN-nxt-open.md §8a, 체결쏴 주식 화면 골격(사용자 2026-10-20).

체결쏴와 같은 틀: 1줄 = 종목 콤보·HL 호가단위 콤보(일반주문창·체결쏴와 같은 틱 머지)·개장 시각·
창(+초 ~ +초) 입력(제목 글자 없이 칸만 — 사용자 2026-10-20),
2줄 = 진입 수치(빨강)·청산 수치(파랑)·환율(출처), 그 아래 표 = 진입 1~3단, 그 밑에 청산 1~3단
(스프레드 %·수량·주문가 %·역산가·결과), 맨 아래 상태줄(창 상태·명령 결과).
화면은 입력·표시만 — 판정·발주는 코어(nxt_open_engine). 화면 스레드에 네트워크 없음:
명령은 뒷단 스레드, 상태는 공유메모리/HTTP 폴링(core_client).
저장은 종목을 바꿀 때·창을 닫을 때(체결쏴 결정 82와 같은 요령).
"""
from __future__ import annotations

from typing import Any

from .domain.enums import Underlying
from .nxt_open import DEFAULT_END_S, DEFAULT_OPEN_HMS, DEFAULT_START_S, TIERS

UNDERLYINGS = (Underlying.SK_HYNIX, Underlying.SAMSUNG)  # 체결쏴와 같이 현대차 제외
NAMES = {Underlying.SK_HYNIX: "하이닉스", Underlying.SAMSUNG: "삼성"}
BY_NAME = {v: k for k, v in NAMES.items()}
WINDOW_TEXT = {"none": "대기", "before": "창 대기", "open": "창 열림", "closed": "창 끝"}


def pct_text(frac: float | None, digits: int = 3) -> str:
    """소수 → 퍼센트 글(0.012 → '1.200'). None은 빈칸."""
    return "" if frac is None else f"{frac * 100:.{digits}f}"


def frac_of(text: str) -> float | None:
    """퍼센트 입력 → 소수(1.2 → 0.012). 빈칸·오타는 None."""
    try:
        return round(float(text.strip()) / 100.0, 8)  # 0.9/100 = 0.009000000000000001 방지
    except ValueError:
        return None


def int_of(text: str) -> int:
    try:
        return int(text.strip().replace(",", ""))
    except ValueError:
        return 0


def qty_of(text: str) -> float:
    """단 수량 칸 → 수(HL 주문이라 소수 가능, 사용자 2026-10-21). 빈칸·오타는 0."""
    try:
        return float(text.strip().replace(",", ""))
    except ValueError:
        return 0.0


def qty_text(qty: Any) -> str:
    """수량 → 칸 글. 0·없음은 빈칸, 100.0은 "100", 0.5는 "0.5"(뒤 0 제거, 최대 소수 6자리)."""
    if not qty:
        return ""
    return f"{float(qty):.6f}".rstrip("0").rstrip(".")


def monitor_text(frac: Any) -> str:
    """모니터 수치 — 체결쏴처럼 퍼센트 소수 2자리(0.26). 없으면 '-'."""
    return "-" if frac is None else f"{float(frac) * 100:.2f}"


def row_locked(running: bool, complete: bool, fired: bool) -> bool:
    """단 입력칸을 잠글지 — 실행 중이고 완전한 단이면 잠그되, **오늘 이미 발주된 단은 푼다**(사용자
    2026-10-23 "발주된 세트는 입력 막혀 있던 거 풀어주는 게 좋을 듯" — 그날은 다시 안 나가니 고쳐도
    됨). 순수."""
    return running and complete and not fired


def row_complete(spread: str, qty: str, price: str) -> bool:
    """표 한 단의 세 칸이 다 채워졌나(Sprd·수량>0·주문가) — 실행 중 잠글 단 판정(사용자 2026-10-21:
    완전한 단만 잠그고 반만 채운 단은 실행 중에도 고칠 수 있게)."""
    return frac_of(spread) is not None and qty_of(qty) > 0 and frac_of(price) is not None


def tiers_payload(rows: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """표 입력(스프레드 %, 수량, 주문가격 %) 3행 → 코어 설정 dict 목록."""
    out: list[dict[str, Any]] = []
    for spread, qty, price in rows:
        out.append({"spread": frac_of(spread), "qty": qty_of(qty), "price_spread": frac_of(price)})
    return out


def settings_payload(u: Underlying, open_hms: str, start: str, end: str,
                     entries: list[tuple[str, str, str]],
                     exits: list[tuple[str, str, str]]) -> dict[str, Any]:
    """입력 → nxt_open_settings 명령 본문. HL 호가단위는 manual_hl_merge로 따로(코어 공용 상태)."""

    def _f(text: str, default: float) -> float:
        try:
            return float(text.strip())
        except ValueError:
            return default

    return {"cmd": "nxt_open_settings", "underlying": u.value,
            "open_hms": open_hms.strip() or DEFAULT_OPEN_HMS,
            "start_s": _f(start, DEFAULT_START_S), "end_s": _f(end, DEFAULT_END_S),
            "entries": tiers_payload(entries), "exits": tiers_payload(exits)}


def merge_choices(ticks: list[dict[str, Any]]) -> tuple[list[str], dict[str, tuple[Any, Any]]]:
    """코어 hl_merge_ticks → (콤보 글 목록, 글 → (n_sig_figs, mantissa)). 체결쏴와 같은 요령."""
    labels: list[str] = []
    table: dict[str, tuple[Any, Any]] = {}
    for t in ticks:
        label = str(t.get("tick"))
        labels.append(label)
        table[label] = (t.get("n_sig_figs"), t.get("mantissa"))
    return labels, table


def active_label(table: dict[str, tuple[Any, Any]], active: Any) -> str | None:
    """코어가 지금 적용 중인 머지에 해당하는 콤보 글 — 틱 글(tick)이 있으면 그것, 없으면
    (n_sig_figs, mantissa)로 찾는다."""
    if not isinstance(active, dict):
        return None
    tick = active.get("tick")
    if tick is not None and str(tick) in table:
        return str(tick)
    key = (active.get("n_sig_figs"), active.get("mantissa"))
    return next((s for s, v in table.items() if v == key), None)


def results_summary(results: list[dict[str, Any]]) -> str:
    """발주 결과 한 줄(상태줄용) — 결과 열은 없앴다(사용자 2026-10-20 목업)."""
    parts: list[str] = []
    for r in results:
        name = f"{'진입' if r.get('block') == 'entry' else '청산'}{int(r.get('index', 0)) + 1}"
        if r.get("order_id"):
            parts.append(f"{name} #{r['order_id']} {r.get('qty', '')}@{r.get('price', '')}")
        else:
            parts.append(f"{name} 실패 {str(r.get('error') or '')[:30]}")
    return ", ".join(parts)


def status_text(live: dict[str, Any]) -> str:
    """상태 줄 — 최대한 짧게(사용자 2026-10-20): 창 상태(+첫 체결 시각) / 발주 결과 / 비고.
    발주 결과는 **마지막 한 건만**(사용자 2026-10-23 "하나씩만 보여줘") — 앞 건들은 상태줄 콤보의
    이력에 한 줄씩 남아 있다(ui_status.push_history)."""
    if live.get("fired"):
        summary = results_summary((live.get("results") or [])[-1:])
        return f"발주 {live.get('fired_at') or ''} {summary}".strip()
    if live.get("note"):
        return str(live["note"])
    text = WINDOW_TEXT.get(str(live.get("window")), "-")
    if live.get("t0"):
        text += f" {live['t0']}"
    return text


def running_text(live_all: dict[str, dict[str, Any]]) -> str:
    """켜진 종목·세트 글 — {"samsung": {"run_entry": True, "run_exit": True}, …} →
    "삼성 진입·청산"(여러 종목은 쉼표). 아무것도 안 켜졌으면 빈 글. 순수."""
    parts: list[str] = []
    for u in UNDERLYINGS:
        live = live_all.get(u.value) or {}
        sets = [name for key, name in (("run_entry", "진입"), ("run_exit", "청산"))
                if live.get(key)]
        if sets:
            parts.append(f"{NAMES[u]} {'·'.join(sets)}")
    return ", ".join(parts)


def saved_under(saved: dict[str, Any]) -> str | None:
    """창 저장(win_state fields)의 보던 종목 이름 — 콤보에 있는 이름일 때만(제외된 종목 무시)."""
    name = saved.get("under")
    return name if isinstance(name, str) and name in BY_NAME else None


def top_states(locked: bool) -> dict[str, str]:
    """윗줄 칸의 잠금 상태(사용자 2026-10-21): 진입·청산 어느 쪽이든 실행 중이면 종목 콤보만 빼고
    호가단위·적·기준수량·시간을 전부 잠근다. 콤보는 평소 readonly(고르기만), 버튼·입력칸은 normal.
    순수 — 화면은 이 표대로 config(state=…)만 한다."""
    combo = "disabled" if locked else "readonly"
    plain = "disabled" if locked else "normal"
    return {"agg": combo, "apply": plain, "ref": plain, "open": plain, "start": plain,
            "end": plain}


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
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
    from .ui_dialog import show_message
    from .ui_fields import is_decimal_text, is_int_text, is_time_text, is_unsigned_decimal_text
    from .ui_status import StallBanner, StatusCombo, log_tk_errors, ls_stalled

    preview = "--preview" in sys.argv
    if not preview:
        watch_parent_exit()
    root = tk.Tk()
    root.title("NXT 개장 매매")
    root.resizable(True, True)
    if not preview:  # 미리보기(캡처용)는 창 위치·종목을 저장하지 않는다 — 실제 창 기록과 분리
        win_state.attach(root, "nxt_open")
    T.apply_base(root)
    root.option_add("*Font", T.FONT_BASE_LG)  # 체결쏴와 같은 11pt
    vcmd_int = (root.register(is_int_text), "%P")
    vcmd_dec = (root.register(is_decimal_text), "%P")
    vcmd_qty = (root.register(is_unsigned_decimal_text), "%P")  # 단 수량: 소수 가능(HL 주문)
    vcmd_time = (root.register(is_time_text), "%P")
    log = screen_log()
    log_tk_errors(root, log)  # 버튼 콜백 예외를 화면 로그에(콘솔이 없어 안 보임, 2026-10-26)

    jobs: queue.Queue[tuple[dict[str, Any], str]] = queue.Queue()
    results: queue.Queue[tuple[str, dict[str, Any] | None]] = queue.Queue()
    state_box: dict[str, Any] = {"data": None}

    def sender() -> None:
        while True:
            payload, label = jobs.get()
            results.put((label, core_request("/command", payload, timeout=10.0)))
            jobs.task_done()

    def poller() -> None:
        run_state_feed(state_box, log_tag="NXT개장", channel="state",
                       fallback_path="/state", poll_s=1.0)

    if not preview:
        threading.Thread(target=sender, daemon=True).start()
        threading.Thread(target=poller, daemon=True).start()

    def send(payload: dict[str, Any], label: str) -> None:
        jobs.put(({**payload, "sent_at": time.time()}, label))

    # applied: 모니터 수치는 '적'을 누른 뒤부터 표시(체결쏴와 같은 규칙, 사용자 2026-09-04·10-20)
    cur: dict[str, Any] = {"u": UNDERLYINGS[0], "loaded": False, "agg_under": None,
                           "applied": False}
    local: dict[Underlying, dict[str, Any]] = {}  # 종목 바꿀 때 화면에 기억해 둔 입력값
    agg_map: dict[str, tuple[Any, Any]] = {}      # 호가단위 글 → (n_sig_figs, mantissa)
    w: dict[str, Any] = {}

    # ===== 1줄: 종목·HL 호가단위·적·기준수량·시간 (체결쏴 주식 윗줄 순서, 사용자 2026-10-20) =====
    top = tk.Frame(root)
    top.grid(row=1, column=0, sticky="ew", padx=6, pady=(6, 2))  # 0행 = LS 처리 지연 띠
    w["under"] = ttk.Combobox(top, values=[NAMES[u] for u in UNDERLYINGS], width=7,
                              state="readonly")
    w["under"].set(NAMES[UNDERLYINGS[0]])
    w["under"].pack(side="left", padx=(0, 4))
    # HL 호가단위(틱 머지) — 일반주문창·체결쏴와 같은 코어 공용 상태(hl_merge). 코어가 준
    # hl_merge_ticks로 채우고, '적'을 누르면 manual_hl_merge + 기준수량을 보낸다(체결쏴와 같음).
    w["agg"] = ttk.Combobox(top, values=["-"], width=6, state="readonly")
    w["agg"].set("-")
    w["agg"].pack(side="left", padx=(0, 4))
    w["apply"] = ttk.Button(top, text="적", width=3, command=lambda: apply_top())
    w["apply"].pack(side="left", padx=(0, 4))
    w["ref"] = tk.Entry(top, width=6, justify="right", validate="key",
                        validatecommand=vcmd_int, font=T.FONT_NUM_LG)  # 기준수량(모니터 est용)
    w["ref"].insert(0, "1")
    w["ref"].pack(side="left", padx=(0, 8))  # 기준수량은 '적'을 누를 때 코어로(결정 74 방식)
    # 개장 시각 · 창(첫 체결 + 시작초 ~ + 끝초) — 제목 글자 없이 칸만(사용자 2026-10-20)
    w["open"] = tk.Entry(top, width=9, justify="center", validate="key",
                         validatecommand=vcmd_time, font=T.FONT_NUM_LG)
    w["open"].insert(0, DEFAULT_OPEN_HMS)
    w["open"].pack(side="left", padx=(0, 4))
    w["start"] = tk.Entry(top, width=4, justify="right", validate="key",
                          validatecommand=vcmd_dec, font=T.FONT_NUM_LG)
    w["start"].insert(0, f"{DEFAULT_START_S:g}")
    w["start"].pack(side="left")
    tk.Label(top, text="초 ~").pack(side="left", padx=(1, 2))  # 시간 단위(사용자 2026-10-21)
    w["end"] = tk.Entry(top, width=4, justify="right", validate="key",
                        validatecommand=vcmd_dec, font=T.FONT_NUM_LG)
    w["end"].insert(0, f"{DEFAULT_END_S:g}")
    w["end"].pack(side="left")
    tk.Label(top, text="초").pack(side="left", padx=(1, 0))

    # ===== 표(사용자 2026-10-20): 진입 세트 | 청산 세트를 좌우로, 구분 열 없음 =====
    # 0행 = 모니터 수치(각 Sprd 헤더 위)·환율, 1행 = 헤더(Sprd 수량 주문가 역산가 실행),
    # 2~4행 = 3단. 실행은 3행 병합 버튼(HL매도/HL매수). 역산가는 주문가 옆 표시 열(사용자
    # 2026-10-22 — 숨은 패널 대신), 결과 열은 없다.
    tbl = tk.Frame(root)
    tbl.grid(row=2, column=0, sticky="ew", padx=6, pady=(4, 0))
    heads = ("Sprd", "수량", "주문가", "역산가", "실행")  # Sprd = 체결쏴 표기
    widths = (5, 6, 5, 8, 7)   # 입력칸 폭은 체결쏴 세트 행과 같게(5·6·5), 역산가 8(1,303.50)
    head_w = (7, 7, 7, 8, 7)   # 헤더 폭이 곧 칸 폭(입력칸은 nsew로 채움 — 체결쏴 세트 행과 같음)
    gap_col = len(heads)       # 두 세트 사이 빈 열
    w["px"] = {}
    w["rows"] = {}
    w["run"] = {}
    w["run_painted"] = {}  # 마지막으로 칠한 상태 — 바뀔 때만 다시 칠한다(매 갱신 칠하면 깜박임)
    w["run_hold"] = {}     # 버튼별 보류 시각 — 누른 뒤 이 시각까지는 코어 상태로 덮어쓰지 않는다
    w["fired"] = set()     # 오늘 발주된 단(코어 fired_tiers) — 실행 중이어도 입력을 푼다
    # 행 높이 = 체결쏴 세트 행과 같게. 체결쏴는 같은 줄의 진입/청산 버튼(11pt)이 행 높이를 정하고
    # 입력칸이 거기 맞춰 늘어난다. 여기는 버튼이 3행 병합이라 같은 버튼의 높이를 재서 최소 높이로.
    probe = tk.Button(tbl, text="진입", width=3, padx=0, pady=0, bd=1, highlightthickness=0)
    row_h = probe.winfo_reqheight() + 2  # pady 1 위아래
    probe.destroy()
    tk.Label(tbl, text="", width=3).grid(row=0, column=gap_col)
    for col0, (block, cond, color) in enumerate((("entry", "HL매도", T.C_BUY),
                                                ("exit", "HL매수", T.C_SELL))):
        base = col0 * (len(heads) + 1)
        # 모니터 수치 — 그 세트의 Sprd 헤더 바로 위(진입 빨강·청산 파랑)
        w[f"{block}_disp"] = tk.Label(tbl, text="-", bg=color, fg="white", anchor="center",
                                      font=T.FONT_NUM_LG)
        w[f"{block}_disp"].grid(row=0, column=base, padx=1, pady=(0, 2), sticky="nsew")
        for c, (h, hw) in enumerate(zip(heads, head_w, strict=True)):  # 헤더 9pt 회색, 테두리 없음
            tk.Label(tbl, text=h, width=hw, fg="gray25", font=T.FONT_LABEL).grid(
                row=1, column=base + c, padx=1, sticky="nsew")
        btn = tk.Button(tbl, text=cond, width=widths[4], padx=0, pady=0, bd=1,
                        highlightthickness=0)  # command는 toggle_run 정의 뒤에 붙인다
        btn.grid(row=2, column=base + 4, rowspan=TIERS, padx=(4, 1), pady=1, sticky="nsew")
        w["run"][block] = btn
        for i in range(TIERS):
            r = 2 + i
            cells: dict[str, Any] = {}
            for c, key, vc in ((0, "spread", vcmd_dec), (1, "qty", vcmd_qty),
                               (2, "price", vcmd_dec)):
                e = tk.Entry(tbl, width=widths[c], justify="right", validate="key",
                             validatecommand=vc, font=T.FONT_NUM_LG,
                             disabledforeground="black", disabledbackground="#f0f0f0")
                e.grid(row=r, column=base + c, padx=1, pady=1, sticky="nsew")
                # 저장은 실행 켤 때·'적'·종목 바꿀 때·창 닫을 때(체결쏴 결정 74 방식, 사용자
                # 2026-10-26 "바꿔") — Enter·칸 이동은 아무 일도 안 한다
                cells[key] = e
            # 역산가 — 주문가 옆 표시 칸(입력 아님, 테두리 없는 회색 글). 그 세트의 실행이 켜진
            # 동안만 갱신하고 꺼지면 마지막 값을 둔다(사용자 2026-10-21·22).
            lbl = tk.Label(tbl, text="-", width=widths[3], anchor="e", fg="gray25",
                           font=T.FONT_NUM_LG)
            lbl.grid(row=r, column=base + 3, padx=1, pady=1, sticky="nsew")
            w["px"][(block, i)] = lbl
            tbl.grid_rowconfigure(r, minsize=row_h)
            w["rows"][(block, i)] = cells
    # 환율 — 0행 오른쪽 끝(체결쏴처럼 작은 회색 글씨)
    w["fx"] = tk.Label(tbl, text="환율 -", font=T.FONT_LABEL, fg="gray25", anchor="e")
    # 청산 세트의 수량·주문가·역산가·실행 열 위에 걸쳐 오른쪽 정렬 — 빈 열 하나에만 두면 창을 안
    # 넓힌 때 폭이 0이라 글이 잘린다(사용자 2026-10-21 "환율이 가려진다"). 늘어나는 빈 열은 걸치지
    # 않는다 — 걸치면 창을 넓힐 때 환율이 오른쪽 끝으로 따라간다(사용자 2026-10-21 "위치 고정").
    w["fx"].grid(row=0, column=gap_col + 2, columnspan=4, sticky="e", padx=(0, 2))
    tbl.grid_columnconfigure(gap_col + 6, weight=1)  # 남는 폭은 여기로(입력칸은 안 늘어남)

    # ===== 상태줄: 창 상태 + 명령 결과 =====
    # width=1 + 가로 채움 — 긴 글이 창을 넓히지 않게(주문 리스트 상태줄과 같음, 사용자 2026-10-20)
    # 상태줄 = 읽기 전용 콤보(사용자 2026-10-23): 최신 글을 보여 주고 펼치면 지난 글 50건. width=1 +
    # 가로 채움 — 긴 글이 창을 넓히지 않게. 펼친 목록은 넓혀서 긴 글도 보이게(postcommand).
    status_box = StatusCombo(root, T.FONT_LABEL)  # 체결쏴와 공용(ui_status)
    status_box.widget.grid(row=3, column=0, sticky="ew", padx=6, pady=(4, 6))
    # LS 처리 지연 띠 — 지연 동안만 맨 위(사용자 2026-10-26)
    banner = StallBanner(root, (T.FAMILY, 10, "bold"),
                         lambda wd: wd.grid(row=0, column=0, sticky="ew"))
    show_status = status_box.show
    root.grid_columnconfigure(0, weight=1)
    note: dict[str, Any] = {"text": "", "until": 0.0}  # 명령 결과는 5초만 보여 주고 창 상태로

    def set_status(text: str, err: bool = False) -> None:
        note["text"], note["until"] = text, time.time() + 5.0
        show_status(("거부 " if err and not text.startswith(("거부", "실패")) else "") + text)
        log.info("[NXT개장 화면] %s", text)

    def rows_of(block: str) -> list[tuple[str, str, str]]:
        return [(w["rows"][(block, i)]["spread"].get(), w["rows"][(block, i)]["qty"].get(),
                 w["rows"][(block, i)]["price"].get()) for i in range(TIERS)]

    def payload_of(u: Underlying) -> dict[str, Any]:
        return settings_payload(u, w["open"].get(), w["start"].get(), w["end"].get(),
                                rows_of("entry"), rows_of("exit"))

    ref_sent = {"qty": -1}

    def send_ref_qty(force: bool = False, applied: bool = False) -> None:
        """기준수량만 코어로 — Enter·포커스 이동 때 바로(체결쏴와 같음). applied='적'을 누른 것:
        코어가 오늘 날짜를 적고 NXT 체결(NS3)을 구독한다(사용자 2026-10-27)."""
        qty = int_of(w["ref"].get())
        if not force and qty == ref_sent["qty"]:
            return
        ref_sent["qty"] = qty
        if not preview:
            body: dict[str, Any] = {"cmd": "nxt_open_settings", "underlying": cur["u"].value,
                                    "ref_qty": qty}
            if applied:
                body["applied"] = True
            send(body, "기준수량")

    def apply_top() -> None:
        """'적' — 호가단위(manual_hl_merge) + 기준수량을 코어에(체결쏴 apply_market과 같은 역할).
        누른 뒤부터 모니터 수치를 보여 준다."""
        cur["applied"] = True
        label = w["agg"].get()
        if label in agg_map and not preview:
            nsf, mant = agg_map[label]
            send({"cmd": "manual_hl_merge", "underlying": cur["u"].value,
                  "tick": label.replace(",", ""), "n_sig_figs": nsf, "mantissa": mant},
                 "호가단위")
        send_ref_qty(force=True, applied=True)
        save_if_changed()  # 단 값도 '적'을 누르면 함께(결정 74 방식)

    def set_entry(e: tk.Entry, val: str) -> None:
        """입력칸 글 바꾸기 — 잠긴(disabled) 칸은 프로그램 쓰기도 막히므로 잠깐 풀고 쓴다."""
        locked = str(e.cget("state")) == "disabled"
        if locked:
            e.config(state="normal")
        e.delete(0, "end")
        e.insert(0, val)
        if locked:
            e.config(state="disabled")

    def lock_top(locked: bool) -> None:
        """윗줄 잠금(사용자 2026-10-21) — 진입·청산 어느 쪽이든 켜져 있으면 호가단위·적·기준수량·
        시간은 못 바꾸고 종목 콤보만 열어 둔다(다른 종목을 보러 갈 수는 있게)."""
        for key, state in top_states(locked).items():
            w[key].config(state=state)

    def paint_run(block: str, on: bool) -> None:
        """실행 버튼 색 — 바뀔 때만 칠한다(매 갱신 칠하면 깜박임). 진입 빨강·청산 파랑.
        마우스를 올리거나 누를 때 쓰는 활성 색(activebackground)도 같은 색으로 — 안 그러면 올려놓은
        동안 기본 회색이 비쳐 켤 때·끌 때 색이 바뀌는 때가 달라 보인다(사용자 2026-10-20)."""
        if w["run_painted"].get(block) is on:
            return
        w["run_painted"][block] = on
        bg = (T.C_BUY if block == "entry" else T.C_SELL) if on else "SystemButtonFace"
        fg = "white" if on else "black"
        w["run"][block].config(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
        lock_rows(block, on)
        lock_top(any(bool(w["run_painted"].get(b)) for b in ("entry", "exit")))

    def lock_rows(block: str, on: bool) -> None:
        """켜진 동안 그 세트의 **완전한 단**만 잠근다(사용자 2026-10-21 "제대로 된 입력이
        2세트뿐이면 2세트만 입력 불가로") — 반만 채운 단은 열어 두고, 채워서 저장하면 판정에
        들어가며 잠긴다."""
        for i in range(TIERS):
            cells = w["rows"][(block, i)]
            complete = row_complete(cells["spread"].get(), cells["qty"].get(),
                                    cells["price"].get())
            lock = row_locked(on, complete, (block, i) in w["fired"])
            for key in ("spread", "qty", "price"):
                cells[key].config(state="disabled" if lock else "normal")

    def toggle_run(block: str) -> None:
        """표의 병합 실행 버튼(HL매도/HL매수) — 체결쏴 진입/청산 버튼과 같은 켜기/끄기.
        누르는 즉시 칠하고(한 박자 늦게 바뀌던 것 — 사용자 2026-10-20), 코어 상태가 오면 그걸로
        맞춘다."""
        u = cur["u"]
        value = not bool(w["run_painted"].get(block))
        label = "진입" if block == "entry" else "청산"
        if value and not any(row_complete(*row) for row in rows_of(block)):
            # 완전한 단이 하나도 없으면 켜지 않고 알림(사용자 2026-10-21 "청산처럼 모든 세트가
            # 조건에 안 맞으면 알림") — 상태줄 한 줄로는 지나치기 쉬움
            set_status(f"{label} 켬 거부: 완전한 단 없음", err=True)
            # 알림창은 부모 창 가운데(ui_dialog, 사용자 2026-10-26) — tk messagebox는 모니터 가운데
            show_message(root, "NXT 개장 매매", f"{label} 세트에 완전한 단이 없습니다.\n"
                         "Sprd·수량·주문가를 다 채운 단이 하나는 있어야 켤 수 있습니다.")
            return
        if value:
            save_current()  # 켤 때 입력값부터 코어에
        paint_run(block, value)
        w["run_hold"][block] = time.time() + 2.0  # 코어가 답하기 전 옛 상태로 되칠해지지 않게
        send({"cmd": "nxt_open_run", "underlying": u.value, "block": block, "value": value},
             f"{'진입' if block == 'entry' else '청산'} {'켬' if value else '끔'}")

    for block in ("entry", "exit"):
        w["run"][block].config(command=partial(toggle_run, block))

    def save_current() -> None:
        u = cur["u"]
        local[u] = payload_of(u)
        if not preview:
            send(local[u], "저장")
        for block in ("entry", "exit"):  # 실행 중에 채운 단은 저장되는 순간 판정에 들어가며 잠긴다
            if w["run_painted"].get(block):
                lock_rows(block, True)

    def save_if_changed() -> None:
        """보낸 값과 다를 때만 저장(같은 값으로 매번 보내지 않게) — '적'·종목 바꿀 때 쓴다."""
        u = cur["u"]
        if local.get(u) != payload_of(u):
            save_current()

    def refresh_agg(live: dict[str, Any]) -> None:
        """코어 hl_merge_ticks로 호가단위 콤보 채우기 — 종목이 바뀌거나 처음, 또는 틱 표가 바뀌었을
        때(가격이 자릿수를 넘음 — 하이닉스 1,000달러, 2026-10-27) set."""
        ticks = live.get("hl_merge_ticks") or []
        if not ticks:
            return
        labels, table = merge_choices(ticks)
        if cur["agg_under"] == cur["u"] and agg_map and list(w["agg"]["values"]) == labels:
            return
        agg_map.clear()
        agg_map.update(table)
        w["agg"].config(values=labels)
        w["agg"].set(active_label(agg_map, live.get("hl_merge_active")) or labels[0])
        cur["agg_under"] = cur["u"]

    def load_inputs(st: dict[str, Any]) -> None:
        set_entry(w["ref"], str(st.get("ref_qty", 1)))  # 윗줄도 잠겨 있을 수 있어 set_entry로
        ref_sent["qty"] = int_of(w["ref"].get())
        for key, val in (("open", st.get("open_hms") or DEFAULT_OPEN_HMS),
                         ("start", f"{float(st.get('start_s', DEFAULT_START_S)):g}"),
                         ("end", f"{float(st.get('end_s', DEFAULT_END_S)):g}")):
            set_entry(w[key], str(val))
        for block, key in (("entry", "entries"), ("exit", "exits")):
            tiers = list(st.get(key) or [])
            for i in range(TIERS):
                t = tiers[i] if i < len(tiers) and isinstance(tiers[i], dict) else {}
                cells = w["rows"][(block, i)]
                for ck, val in (("spread", pct_text(t.get("spread"))),
                                ("qty", qty_text(t.get("qty"))),
                                ("price", pct_text(t.get("price_spread")))):
                    set_entry(cells[ck], val)
        cur["loaded"] = True

    def on_under_change(_e: object = None) -> None:
        if cur["loaded"]:  # 보던 종목의 입력값을 코어에(미리보기면 화면에만) 남기고
            save_current()  # (아직 코어 값을 못 받았으면 빈 칸을 저장해 지우는 일이 없게)
        u = BY_NAME.get(w["under"].get(), UNDERLYINGS[0])
        cur["u"], cur["loaded"], cur["agg_under"] = u, False, None
        agg_map.clear()
        w["agg"].config(values=["-"])
        w["agg"].set("-")
        if u in local:
            load_inputs(local[u])  # 화면이 기억한 값 먼저, 코어 값은 다음 갱신에서
            cur["loaded"] = True

    w["under"].bind("<<ComboboxSelected>>", on_under_change)

    def refresh() -> None:
        data = state_box.get("data") or {}
        banner.set(ls_stalled(data.get("ws") or []))
        u = cur["u"]
        settings = (data.get("nxt_open") or {}).get("books") or {}
        if not cur["loaded"] and isinstance(settings.get(u.value), dict):
            load_inputs(settings[u.value])
        live = (data.get("nxt_open_live") or {}).get(u.value) or {}
        refresh_agg(live)
        shown = cur["applied"]  # '적' 전엔 "-"
        w["entry_disp"].config(text=monitor_text(live.get("entry_disp")) if shown else "-")
        w["exit_disp"].config(text=monitor_text(live.get("exit_disp")) if shown else "-")
        fx = live.get("fx")
        w["fx"].config(text=f"환율 {fx:,.2f} ({live.get('fx_src') or '-'})" if fx else "환율 -")
        for block, key in (("entry", "entry_prices"), ("exit", "exit_prices")):
            # 역산가는 그 세트의 실행이 켜진 동안만 갱신(사용자 2026-10-21 — 저장 전 값과 헷갈리지
            # 않게). 꺼지면 마지막 값을 그대로 둔다("-"로 지우지 않음).
            if live.get(f"run_{block}"):
                prices = live.get(key) or []
                for i in range(TIERS):
                    px = prices[i] if i < len(prices) else None
                    w["px"][(block, i)].config(text="-" if px is None else f"{px:,.2f}")
            if live and time.time() > w["run_hold"].get(block, 0.0):
                paint_run(block, bool(live.get(f"run_{block}")))  # 보류 뒤엔 코어 상태가 진실
        fired = {(str(b), int(i)) for b, i in (live.get("fired_tiers") or [])}
        if fired != w["fired"]:  # 발주된 단이 생기면 그 단의 입력을 푼다(사용자 2026-10-23)
            w["fired"] = fired
            for block in ("entry", "exit"):
                if w["run_painted"].get(block):
                    lock_rows(block, True)
        try:
            while True:
                label, result = results.get_nowait()
                if result is None:
                    set_status(f"{label}: 코어 미접속", err=True)
                elif not result.get("ok"):
                    set_status(f"{label} 거부: {'; '.join(result.get('errors', []))}", err=True)
                else:
                    set_status(label)
        except queue.Empty:
            pass
        # 발주 결과는 단 하나당 한 줄씩 이력에(사용자 2026-10-23 "하나씩만") — 새로 늘어난 결과만
        results_now = list(live.get("results") or []) if live else []
        seen = int(state_box.get("_results_seen", 0))
        if len(results_now) < seen:  # 오늘 상태 초기화(개장 시각 변경·날짜 바뀜) → 처음부터
            seen = 0
        for r in results_now[seen:]:
            show_status(f"발주 {live.get('fired_at') or ''} {results_summary([r])}".strip())
        state_box["_results_seen"] = len(results_now)
        if time.time() > note["until"] and not preview:
            show_status(status_text(live) if live else "코어 미연결")
        if main_closing():
            root.after(50, on_close)
            return
        root.after(150, refresh)

    # 닫기 규칙은 **보고 있는 종목만**(체결쏴와 같음, 사용자 2026-10-22 "안 그럼 한번 잘못 연
    # 화면은 닫을 수가 없다") — 다른 종목이 켜져 있어도 묻지 않고 닫고, 그 실행은 코어에 남는다.
    def cur_live() -> dict[str, Any]:
        live_all = (state_box.get("data") or {}).get("nxt_open_live") or {}
        return dict(live_all.get(cur["u"].value) or {})

    def is_running() -> bool:
        live = cur_live()
        return bool(live.get("run_entry")) or bool(live.get("run_exit"))

    def describe_running() -> str:
        """닫기 확인창용 — 켜진 종목·세트("삼성 진입·청산")."""
        return running_text({cur["u"].value: cur_live()})

    def send_stop() -> None:
        for block in ("entry", "exit"):
            send({"cmd": "nxt_open_run", "underlying": cur["u"].value, "block": block,
                  "value": False}, "실행 끔")

    # --- 창 저장/복원(win_state, 체결쏴와 같은 요령) — 보던 종목(사용자 2026-10-21 "종목콤보
    # 저장이 안 됨"). 위치는 attach가 2초마다. 단 값·기준수량은 코어가 종목별로 들고 있어 제외.
    def collect_fields() -> dict[str, Any]:
        return {"under": w["under"].get()}

    def restore_saved() -> None:
        name = saved_under(win_state.saved_fields("nxt_open"))
        if name is None or name == w["under"].get():
            return
        w["under"].set(name)  # on_under_change를 부르지 않는다 — 아직 받은 값이 없어 저장할 게 없음
        cur["u"], cur["loaded"], cur["agg_under"] = BY_NAME[name], False, None

    def persist_fields() -> None:
        try:
            win_state.save_fields("nxt_open", collect_fields())
            root.after(2000, persist_fields)
        except tk.TclError:
            pass  # 창 닫힘

    if not preview:
        restore_saved()
        root.after(2000, persist_fields)

    def before_close() -> None:
        if preview:
            return
        win_state.save_fields("nxt_open", collect_fields())
        if cur["loaded"]:  # 닫을 때 보던 종목의 입력값 저장(체결쏴 결정 82와 같은 요령)
            send(payload_of(cur["u"]), "저장")
        # 화면 닫힘 = 실행 종료 → '적'으로 붙인 NXT 체결 구독 근거를 지운다(보던 종목만, 사용자
        # 2026-10-27 "실행이 종료됐을 때 구독 취소")
        send({"cmd": "nxt_open_settings", "underlying": cur["u"].value, "applied": False},
             "구독 해제")
        deadline = time.time() + 3.0
        while jobs.unfinished_tasks and time.time() < deadline:
            time.sleep(0.05)

    on_close = attach_auto_close(root, title="NXT 개장 매매", is_running=is_running,
                                 send_stop=send_stop, set_status=set_status,
                                 before_close=before_close, skip_confirm=main_closing,
                                 describe_running=describe_running)
    if preview:
        show_status("UI 미리보기 — 코어 미연결")
    root.after(150, refresh)
    root.mainloop()


if __name__ == "__main__":
    main()
