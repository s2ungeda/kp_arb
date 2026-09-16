"""자동M 주문 화면 (체결쏴 — HL·주식선물 선주문 maker→후주문 taker) — 코어 클라이언트.

    python -m kp_arb.order_autom     (운영은 main.bat 메뉴에서)

원본: docs/STG_2 목업(layout_1·체결쏴 설정·세트설정) + DESIGN-auto-m-exec.md §11(전략·화면 스펙).
화면 골격 하나를 사양(ScreenSpec)으로 갈라 띄운다(2026-09-16): 주식선물(SF_SPEC, 이 모듈의
main 기본)·주식(STOCK_SPEC, order_autom_stock). 옛 바로쏴(자동T) 화면은 같은 날 삭제 — 체결쏴 T
모드(사양 하나 더)로 대체 예정. 공용 순수 헬퍼·상수는 ui_fields(단일 출처).
**화면 스레드는 네트워크 금지**(CLAUDE.md).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import partial
from typing import Any

from .auto_m import SET_COUNT_FWD, SET_COUNT_REV, price_offset_errors
from .ui_fields import (
    UNDER_MAP,
    UNDERLYINGS,
    format_qty,
    is_decimal_text,
    is_int_text,
    is_qty_text,
    is_signed_int_text,
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
# 국내 체결은 SF라 목업의 'S'를 'SF'로 표기(사용자 2026-09-09).
_ACC_ROWS_FWD = (("진입", ("-HP", "+SF", "-환")), ("청산", ("+HP", "-SF", "+환")))
_ACC_ROWS_REV = (("진입", ("+HP", "-SF", "+환")), ("청산", ("-HP", "+SF", "-환")))


@dataclass(frozen=True)
class ScreenSpec:
    """체결쏴 화면 사양 — 같은 골격을 주식선물(SF_SPEC)·주식(STOCK_SPEC)으로 띄운다(2026-09-16).

    주식 버전(사용자: "레이아웃은 주식선물과 유사, 역방향 제외, 주식용 공통설정 따로")은 선주문이
    현물이라 SF 기준값 칸이 없고(진입 S·청산 S 두 칸), 월물 콤보가 없다. 판정·수량 비율 같은
    전략 결정은 뒤에(사용자 "결정은 나중에") — 여기는 화면 골격만.
    """

    product: str                  # "sf" | "stock" — 코어 명령의 product 값
    title: str                    # 창 제목
    product_tag: str              # 방향 제목 옆 작은 글씨 "(주식선물)" / "(주식)"
    state_key: str                # win_state 저장 키
    screen_tag: str               # 코어 명령 screen 태그
    log_tag: str                  # 화면 로그 태그
    directions: tuple[tuple[str, str, str, str, str], ...]  # (태그, 이름, 진입SF, 진입S, 청산)
    acc_rows: dict[str, tuple[tuple[str, tuple[str, str, str]], ...]]  # 방향 → 매매결과 라벨
    has_en_sf: bool               # 진입 SF 기준값 칸(주식은 없음)
    has_month: bool               # 선물 월물 콤보(주식은 없음)
    qty_unit: str                 # 상태줄 수량 단위 "계약" / "주"
    settings_title: str           # 공통설정 창 제목
    pre_tick: dict[str, int]      # 선주문 주문단위 기본값(종목별)
    diff_head: str = "체결차(HP)"  # 체결차 컬럼 머리(주식은 단위 표기 없이 "체결차", 09-16)
    entry_s_label: str = "진입S"   # 세트설정 창 진입 S 칸 라벨(주식은 "진입" — SF 칸이 없으니)
    color_dialog: bool = False    # 바탕색: 콤보(기본) 대신 색 고르기 대화상자(주식, 09-16)
    qty_commas: bool = False      # 세트설정 목표수량·1회주문수량 천 단위 쉼표 표시(주식, 09-16)
    credit_boxes: bool = False    # 세트설정 '진입 신용'·'청산 신용상환' 체크(주식, 09-16)


SF_SPEC = ScreenSpec(
    product="sf", title="체결쏴(자동M)-주식선물", product_tag="(주식선물)", state_key="autoM",
    screen_tag="autoM", log_tag="자동M", directions=_DIRECTIONS,
    acc_rows={"fwd": _ACC_ROWS_FWD, "rev": _ACC_ROWS_REV}, has_en_sf=True, has_month=True,
    qty_unit="계약", settings_title="체결쏴 설정",
    pre_tick={"sk_hynix": 3000, "samsung": 500, "hyundai": 1000})

# 주식: 정방향만, 기준값 칸은 진입 S(-HP/+S)·청산 S(+HP/-S), 매매결과 +S/-S(주식 평균 체결가).
# 주문단위 기본값은 주식 호가단위(20만~50만 원 100원, 50만 원 이상 500원)로 — 결정 전 임시.
STOCK_SPEC = ScreenSpec(
    product="stock", title="체결쏴(자동M)-주식", product_tag="(주식)", state_key="autoMS",
    screen_tag="autoMS", log_tag="자동M주식",
    directions=(("fwd", "정방향", "", "-HP/+S", "+HP/-S"),),
    acc_rows={"fwd": (("진입", ("-HP", "+S", "-환")), ("청산", ("+HP", "-S", "+환")))},
    has_en_sf=False, has_month=False, qty_unit="주", settings_title="체결쏴 설정(주식)",
    pre_tick={"sk_hynix": 1000, "samsung": 100, "hyundai": 500},
    diff_head="체결차", entry_s_label="진입", color_dialog=True, qty_commas=True,
    credit_boxes=True)


def run_caption(side: str, credit: bool) -> str:
    """실행 버튼 글자 — 주식 신용 구분(사용자 2026-09-16): 진입 신용이면 '신용', 청산 신용상환이면
    '상환', 아니면 '진입'/'청산'. 진행 상태는 여전히 색으로만(캡션 규칙 2026-09-04 유지)."""
    if side == "en":
        return "신용" if credit else "진입"
    return "상환" if credit else "청산"
# 방향별 세트 줄 수 = 코어 세트 수(정 4·역 2, 사용자 확정 2026-09-15). 배치 검토는 값을 바꿔 띄운다.
SET_ROWS: dict[str, int] = {"fwd": SET_COUNT_FWD, "rev": SET_COUNT_REV}


def section_height(rows: int) -> int:
    """방향 섹션이 차지하는 그리드 줄 수 = 제목 1 + 컬럼헤더 1 + 세트 줄. 매매결과 블록(Sprd+3칸)이
    헤더 줄부터 4줄을 쓰므로 세트가 3줄 미만이어도 높이는 5 이상."""
    return 2 + max(rows, 3)

# 바탕색 선택(상단 콤보, 사용자 2026-09-10) — 채도 낮은 옅은 색만(흰 칸·노란 칸·빨강/파랑
# 모니터 칸이 묻히지 않게). "기본"은 시스템 기본(회색).
_BG_CHOICES: dict[str, str | None] = {"기본": None, "흰색": "#ffffff", "하늘": "#e8f0f8",
                                      "민트": "#eaf4ee", "베이지": "#f5f0e6"}  # 흰색 추가 09-15

# 선물 월물 콤보(상단) — 표시 → 코어 settings.future_month 값 (DESIGN §5.11)
MONTH_MAP = {"최근": "near", "차근": "next"}  # 표시는 '최근/차근'(사용자 2026-09-03)


def pct_to_frac(value: float | None) -> float | None:
    """화면 %(0.5) → 코어 소수(0.005). 빈값은 None 그대로."""
    return None if value is None else value / 100.0


def set_payload(index: int, w: dict[str, Any], underlying: str,
                direction: str = "fwd") -> dict[str, Any]:
    """세트 화면 상태 → 코어 autom_set 명령(종목별 책, direction fwd|rev). 기준값은 %→소수."""
    return {
        "cmd": "autom_set", "underlying": underlying, "set": index, "direction": direction,
        "target_qty": int(w.get("target") or 0), "per_qty": int(w.get("per") or 0),
        "switch_delay_s": int(w.get("delay") or 0),
        "price_offset": int(w.get("offset") or 0),  # 주문가 기준배수(원, 2026-09-15)
        "en_sf": pct_to_frac(w.get("en_sf")), "en_s": pct_to_frac(w.get("en_s")),
        "ex_sf": pct_to_frac(w.get("ex_sf")),
        "rt_manual": w.get("rt_manual"), "clear_diff": bool(w.get("clear_diff")),
        # 주식 신용(2026-09-16): 진입 신용매수 / 청산 신용상환 — 코어 쪽 처리는 결정 뒤
        "credit_en": bool(w.get("credit_en")), "credit_ex": bool(w.get("credit_ex")),
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
        # 역방향 리스크방지도 코어 저장(2026-09-14) — 옛 화면 상태에 키가 없으면 기본값
        "risk_rev_en": float(common["risk"].get("rev_en", 0.5)) / 100.0,
        "risk_rev_ex": float(common["risk"].get("rev_ex", 0.0)) / 100.0,
        "risk_rev_gap": float(common["risk"].get("rev_gap", 0.1)) / 100.0,
    }


def fx_caption(used: object, src: object) -> str:
    """정방향 모니터 옆 환율 표시 — '환율 1,349.60 (현물)'. 값이 없으면 '환율 -'. 순수 로직."""
    if not isinstance(used, int | float) or used <= 0:
        return "환율 -"
    tail = f" ({src})" if src else ""
    return f"환율 {float(used):,.2f}{tail}"


ACC_CLEAR_WAIT_S = 3.0  # 누적 clear 뒤 코어 스냅샷이 지워진 값을 실어 올 때까지 "-" 유지 상한


def acc_clear_pending(pending: dict[tuple[str, str], float], key: tuple[str, str],
                      hl_qty: float, now: float) -> bool:
    """매매결과 clear 직후 옛 합계를 다시 그리지 않기 위한 판정(순수, 2026-09-15).

    clear를 누르면 화면은 "-"를 쓰고 코어에 명령을 보내는데, 그 사이 도착하는 스냅샷은 아직
    옛 값이라 한 번 되살아났다가 지워지며 깜빡였다. pending에 (방향, 그룹)이 있는 동안 스냅샷
    합계가 0이 아니면 그리지 않는다(True). 합계가 0으로 오거나 상한 시간을 넘기면 pending에서
    지우고 그린다(False) — 코어가 명령을 못 받았어도 화면이 영원히 굳지 않게."""
    started = pending.get(key)
    if started is None:
        return False
    if hl_qty <= 0 or now - started >= ACC_CLEAR_WAIT_S:
        del pending[key]
        return False
    return True


def sum_acc(rows: list[dict[str, Any]], leg: str) -> dict[str, float | None]:
    """세트별 누적(autom_live)을 방향 하나로 합산 — 수량은 **짝이 맞은(적은 쪽)** 체결량 합
    (사용자 확정 2026-09-08: LS·HL 누적 체결량이 다르면 적은 쪽 기준, SF 1 = HL 10), 환·HL평균가·
    SF평균가·Sprd는 그 HL 수량 가중(값이 없는 세트는 그 항목의 가중에서 뺀다)."""
    hl = sf = 0.0
    weighted: dict[str, list[float]] = {k: [0.0, 0.0] for k in ("fx_avg", "hl_avg", "sf_avg",
                                                                 "sprd")}  # [가중합, 수량]
    for row in rows:
        acc = row.get(leg) or {}
        raw_hl = float(acc.get("hl_qty") or 0)
        raw_sf = float(acc.get("sf_qty") or 0)
        q = float(acc.get("matched_hl", min(raw_hl, raw_sf * 10)) or 0)
        hl += q
        sf += float(acc.get("matched_sf", q / 10) or 0)
        if q <= 0:
            continue
        for key, slot in weighted.items():
            if acc.get(key) is not None:
                slot[0] += float(acc[key]) * q
                slot[1] += q
    out: dict[str, float | None] = {"hl_qty": hl, "sf_qty": sf}
    for key, (w, wq) in weighted.items():
        out[key] = w / wq if wq > 0 else None
    return out

# 진입/청산 진행 상태(exec §2)의 짧은 표시 — 상태줄 상세용(버튼 캡션은 항상 '진입'/'청산')
# 상태줄 진행 상태 표기 — pre_resting은 '접수'(옛 '걸림', 사용자 2026-09-08)
_STATUS_TEXT = {"armed": "감시", "pre_resting": "접수", "pre_partial": "부분",
                "post_pending": "HL", "settle_delay": "쉼", "halted": "중지"}

# 선주문 주문단위 설정 종목 순서 (목업 라벨 → underlying 코드)
_PRE_TICK_ROWS = (("하이닉스", "sk_hynix"), ("삼성전자", "samsung"), ("현대차", "hyundai"))
# 상대호가 콤보 — 선주문 진입범위 §6.3: 매수는 상대호가−1틱, 매도는 +1틱
_REL_CHOICES_BUY = [f"상대{n}호가 - 1틱" for n in range(1, 6)]
_REL_CHOICES_SELL = [f"상대{n}호가 + 1틱" for n in range(1, 6)]


_SET_INPUT_KEYS = ("target_qty", "per_qty", "switch_delay_s", "price_offset",
                   "en_sf", "en_s", "ex_sf")


def set_inputs_sig(book: dict[str, Any]) -> str:
    """코어 책의 세트 입력값(3세트) 서명 — 바뀌었을 때만 화면을 다시 채우기 위한 비교 키.

    세트설정이 코어에 반영되는 시점은 두 곳뿐(사용자 확정 2026-09-09): 세트설정 창 '확인'과
    진입/청산 실행 버튼을 켤 때. 그때 코어 값이 바뀌므로 같은 종목을 연 다른 창도 이 서명이
    달라진 것을 보고 입력값을 다시 읽는다.
    """
    rows = book.get("sets")
    if not isinstance(rows, list):
        return ""
    rev = book.get("rev_sets")  # 역방향 세트(2026-09-14) — 옛 코어 스냅샷엔 없을 수 있음
    both = rows[:SET_ROWS["fwd"]] + (rev[:SET_ROWS["rev"]] if isinstance(rev, list) else [])
    return json.dumps([[r.get(k) for k in _SET_INPUT_KEYS] for r in both
                       if isinstance(r, dict)], sort_keys=True)


def rt_manual_errors(dtag: str, text: str) -> list[str]:
    """RT 수동 입력 부호 검사(순수) — 정방향은 0 이상, 역방향은 0 이하(§7A: 역방향 RT는 음수
    그대로). '-'만 있거나 숫자가 아니면 오류."""
    raw = text.strip()
    try:
        value = int(raw)
    except ValueError:
        return ["RT 진입수량은 정수로 입력하세요"]
    if dtag == "rev" and value > 0:
        return ["역방향 RT는 0 또는 음수(−)로 입력하세요 — 보유 1계약 = -1"]
    if dtag != "rev" and value < 0:
        return ["정방향 RT는 0 이상으로 입력하세요"]
    return []


def check_risk(dtag: str, en_sf: float | None, ex_sf: float | None,
               risk_en: float, risk_ex: float, risk_gap: float) -> list[str]:
    """자동M 리스크방지 입력 검증 (exec §11.9). 위반 메시지 목록(빈 목록=통과).

    검사 시점은 실행 버튼을 켤 때와 세트설정 저장 때뿐(사용자 2026-09-09). 진입S는 검사하지
    않는다(사용자 2026-09-09) — 진입SF·청산만. None(미입력) 값은 건너뛴다. gap 검증은
    진입SF·청산 둘 다 있을 때만.
    정방향: 진입SF > 기준, 청산 < 기준, 진입SF−청산 > gap.
    역방향: 진입SF < 기준, 청산 > 기준, 청산−진입SF > gap.
    """
    errs: list[str] = []
    if dtag == "fwd":
        if en_sf is not None and en_sf <= risk_en:
            errs.append(f"정방향 진입SF는 {risk_en:g} 초과여야 합니다")
        if ex_sf is not None and ex_sf >= risk_ex:
            errs.append(f"정방향 청산은 {risk_ex:g} 미만이어야 합니다")
        if en_sf is not None and ex_sf is not None and en_sf - ex_sf <= risk_gap:
            errs.append(f"정방향 진입SF−청산은 {risk_gap:g} 초과여야 합니다")
    else:
        if en_sf is not None and en_sf >= risk_en:
            errs.append(f"역방향 진입SF는 {risk_en:g} 미만이어야 합니다")
        if ex_sf is not None and ex_sf <= risk_ex:
            errs.append(f"역방향 청산은 {risk_ex:g} 초과여야 합니다")
        if en_sf is not None and ex_sf is not None and ex_sf - en_sf <= risk_gap:
            errs.append(f"역방향 청산−진입SF는 {risk_gap:g} 초과여야 합니다")
    return errs


def main(spec: ScreenSpec = SF_SPEC) -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """자동M 화면 실행 — spec으로 주식선물(기본)·주식 화면을 같은 골격으로 띄운다."""
    import os
    import queue
    import sys
    import threading
    import time
    import tkinter as tk
    from collections.abc import Callable
    from tkinter import ttk

    from . import ui_theme as T
    from . import win_state
    from .core_client import (
        box_is_live,
        core_request,
        run_state_feed,
        screen_log,
        watch_parent_exit,
    )
    from .ui_close import attach_auto_close
    from .ui_dialog import center_on_parent

    preview = "--preview" in sys.argv  # UI만 확인 — 코어 접속·부모감시 없이 레이아웃만
    if not preview:
        watch_parent_exit()
    root = tk.Tk()
    from .core_client import log_screen_timing
    log_screen_timing(root, __name__)  # 시동 계측: 화면 시작·표시 시각(screen 로그)
    root.title(spec.title)  # 캡션에 상품명(사용자 2026-09-10)
    root.resizable(True, True)
    win_state.attach(root, spec.state_key)
    dirs = tuple(d[0] for d in spec.directions)  # 이 화면의 방향 태그("fwd"[, "rev"])
    T.apply_base(root)
    root.option_add("*Font", T.FONT_BASE_LG)  # 큰 화면 — 자동T와 같은 11pt
    vcmd_int = (root.register(is_int_text), "%P")
    vcmd_sint = (root.register(is_signed_int_text), "%P")  # 역방향 RT 수동 입력('-' 허용)
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
        jobs.put(({**payload, "screen": spec.screen_tag, "product": spec.product}, label))

    # 화면 상태(로컬) — v1은 표시·입력만. 공통설정(체결쏴 설정) 기본값은 목업 기준.
    common: dict[str, Any] = {
        "windows": ["08:30:10", "08:46:20", "15:35:30", "15:46:55"],
        "pre_tick": dict(spec.pre_tick),
        "pre_delay": 1000, "resume_delay": 10, "pre_range": 0.4,
        "rel_buy": 1, "rel_sell": 1,
        "hl_margin_buy": 1.0, "hl_margin_sell": 1.0,  # 후주문 HP 여유(%) — 지정가 taker

        "risk": {"fwd_en": 0.0, "fwd_ex": 0.5, "fwd_gap": 0.1,
                 "rev_en": 0.5, "rev_ex": 0.0, "rev_gap": 0.1},
    }
    # 세트: 진입 SF·S 2개 + 청산 SF 1개 (자동T는 진입/청산 1개씩)
    sets: dict[tuple[str, int], dict[str, Any]] = {}
    for d in dirs:
        for i in range(SET_ROWS[d]):
            sets[(d, i)] = {"target": 0, "per": 0, "delay": 0, "offset": 0, "en_sf": None,
                            "en_s": None, "ex_sf": None, "rt_manual": None,
                            "clear_diff": False, "credit_en": False, "credit_ex": False}

    # ===================== 상단 바 =====================
    top = tk.Frame(root)
    top.pack(fill="x", padx=4, pady=(2, 2))
    # 종목 콤보 — 현대차 제외(사용자 2026-09-04, 시세 화면과 동일). 코어 취급 종목은 그대로.
    # 앞의 '종목' 제목 라벨은 뺐다(사용자 2026-09-11, 상단 공간 확보).
    cb_under = ttk.Combobox(top, values=[u for u in UNDERLYINGS if u != "현대차"],
                            width=7, state="readonly")
    cb_under.set("하이닉스")
    cb_under.pack(side="left", padx=(0, 4))
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
    if spec.has_month:  # 주식 화면엔 월물이 없다(콤보는 만들되 안 붙임 — 아래 코드 공용)
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
        if spec.has_month:
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
    # 바탕색 콤보(사용자 2026-09-10) — 설정 버튼 왼쪽. 창 구분용으로 바탕(창·프레임·헤더 라벨)만
    # 물들이고 입력칸·모니터 칸·중지 행 색은 그대로. 선택은 창 저장(win_state)에 남긴다.
    default_bg = root.cget("bg")
    bg_state = {"cur": default_bg, "name": "기본"}  # name = 콤보 이름 또는 '#rrggbb'(대화상자)
    cb_bg = ttk.Combobox(top, values=list(_BG_CHOICES), width=5, state="readonly")
    cb_bg.set("기본")
    fg_state: dict[str, str | None] = {"cur": None}  # 글자색('#rrggbb', None=기본)
    if spec.color_dialog:  # 주식 화면(사용자 2026-09-16): 콤보 대신 색 고르기 대화상자
        def pick_color(kind: str) -> None:
            from tkinter import colorchooser

            start = bg_state["cur"] if kind == "bg" else (fg_state["cur"] or "black")
            title = "바탕색 선택" if kind == "bg" else "글자색 선택"
            _rgb, hexv = colorchooser.askcolor(color=start, parent=root, title=title)
            if hexv:
                (apply_bg if kind == "bg" else apply_fg)(str(hexv))

        def open_color_menu() -> None:
            # 바탕색·글자색 두 가지(사용자 2026-09-16) — 작은 창에서 고른다. '기본'은 글자색 되돌림
            win = tk.Toplevel(root)
            win.title("색상")
            win.resizable(False, False)
            win.transient(root)
            for text, cmd in (("바탕색", lambda: pick_color("bg")),
                              ("글자색", lambda: pick_color("fg")),
                              ("글자색 기본", lambda: apply_fg(None))):
                tk.Button(win, text=text, width=12, command=cmd).pack(padx=10, pady=4)
            tk.Button(win, text="닫기", width=12, command=win.destroy).pack(padx=10, pady=(4, 10))
            center_on_parent(win, root)

        tk.Button(top, text="색상", command=open_color_menu).pack(side="right", padx=(0, 6))
    else:
        cb_bg.pack(side="right", padx=(0, 6))
    # 주문가능시간 표시 — '주문가능' 글자는 뺀다(콤보 자리 확보, 사용자 2026-09-10)
    lbl_windows = tk.Label(top, text="", fg="gray25")
    lbl_windows.pack(side="right", padx=(0, 8))

    def apply_bg(name: str) -> None:
        """바탕색 적용 — 지금 바탕색을 쓰는 창·프레임·라벨만 새 색으로(칸 색은 건드리지 않음).
        name은 콤보 이름('기본'·'흰색'…) 또는 대화상자가 준 '#rrggbb'."""
        target = (name if name.startswith("#") else _BG_CHOICES.get(name)) or default_bg
        bg_state["name"] = name if (name.startswith("#") or name in _BG_CHOICES) else "기본"
        cur = bg_state["cur"]

        def walk(w: Any) -> None:
            for child in w.winfo_children():
                if isinstance(child, tk.Frame | tk.Label | tk.LabelFrame):
                    try:
                        if child.cget("bg") == cur:
                            child.config(bg=target)
                    except tk.TclError:
                        pass
                walk(child)

        root.config(bg=target)
        walk(root)
        bg_state["cur"] = target

    cb_bg.bind("<<ComboboxSelected>>", lambda _e: apply_bg(cb_bg.get()))

    def apply_fg(color: str | None) -> None:
        """글자색 적용(사용자 2026-09-16) — 바탕 위에 바로 놓인 라벨(제목·컬럼 머리·매매결과 항목
        이름·주문가능시간·환율 캡션·상태줄)만. 흰/노란 칸의 숫자, 빨강·파랑 모니터 칸과 실행 버튼의
        흰 글자, 중지 행, 입력칸은 그대로(읽힘 유지). None = 기본(검정)으로 되돌림."""
        target = color or "black"
        bg_now = bg_state["cur"]

        def walk(w: Any) -> None:
            for child in w.winfo_children():
                if isinstance(child, tk.Label):
                    try:
                        if child.cget("bg") == bg_now:  # 바탕 위 라벨 = 칸이 아닌 글자
                            child.config(fg=target)
                    except tk.TclError:
                        pass
                walk(child)

        walk(root)
        fg_state["cur"] = color

    def refresh_windows_bar() -> None:
        w = common["windows"]
        lbl_windows.config(text=f"{w[0]}~{w[1]}  /  {w[2]}~{w[3]}")

    # ===================== 방향 섹션 2개 =====================
    def build_section(grid: Any, rbase: int, dtag: str, name: str, en_sf: str,
                      en_s: str, ex_sf: str, acc_rows: tuple[Any, ...]) -> None:
        # 두 방향을 공유 그리드에 rbase 오프셋으로 → 컬럼 공유 = 완벽 정렬.
        # 컬럼 순서 — 주식선물 11칸(진입SF·진입S·청산SF), 주식 10칸(진입S·청산S — SF 칸 없음)
        order = (["tg", "per"] + (["en_sf"] if spec.has_en_sf else []) +
                 ["en_s", "btn_en", "ex_sf", "btn_ex", "set", "rt", "diff", "sec"])
        col = {k: c for c, k in enumerate(order)}
        head_of = {"tg": "목표수량", "per": "1회주문", "en_sf": en_sf, "en_s": en_s,
                   "btn_en": "실행", "ex_sf": ex_sf, "btn_ex": "실행", "set": "설정",
                   "rt": "RT선진입", "diff": spec.diff_head, "sec": "초"}  # 체결차 단위 = HL(09-11)
        heads = tuple(head_of[k] for k in order)
        nset = len(heads)

        # 제목 "정방향 (주식선물)" — 상품명은 작은 글씨로 붙여 목표수량·1회주문 두 칸 안에 들어가게
        # (모니터 수치 칸을 밀지 않게, 사용자 2026-09-10)
        title = tk.Frame(grid)
        title.grid(row=rbase, column=0, columnspan=2, sticky="w", pady=(0, 2))
        tk.Label(title, text=name, font=T.FONT_NUM_LG).pack(side="left")
        tk.Label(title, text=spec.product_tag, font=T.FONT_LABEL, fg="gray25").pack(
            side="left", padx=(2, 0), pady=(3, 0))
        # 모니터 수치를 제목 옆, 각 기준값 컬럼 위치에 맞춰 배치(주식은 진입S·청산S 두 칸)
        for skey, color in (("en_sf", T.C_BUY), ("en_s", T.C_BUY), ("ex_sf", T.C_SELL)):
            if skey not in col:
                continue
            mlbl = tk.Label(grid, text="-", bg=color, fg="white", anchor="center",
                            font=T.FONT_NUM_LG)
            mlbl.grid(row=rbase, column=col[skey], padx=1, pady=(0, 2), sticky="nsew")
            mon[f"{dtag}_{skey}"] = mlbl
        if dtag == "fwd":
            # 지금 HL 환산에 쓰는 환율(값·출처) — 정방향 모니터 수치 옆 빈 자리(RT선진입~초 칸 위,
            # 사용자 2026-09-11). 코어 스냅샷 fx.used/src, 출처는 현물|선물이론.
            mon["fx"] = tk.Label(grid, text="환율 -", font=T.FONT_LABEL, fg="gray25", anchor="e")
            mon["fx"].grid(row=rbase, column=col["rt"], columnspan=3, sticky="e", padx=(0, 2))
        ttk.Separator(grid, orient="vertical").grid(
            row=rbase, column=nset, rowspan=section_height(SET_ROWS[dtag]), sticky="ns", padx=3)
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

        for i in range(SET_ROWS[dtag]):  # 세트 줄(기본 3)
            r = rbase + i + 2
            w = sets[(dtag, i)]
            lbl_tg = tk.Label(grid, text="-", width=6, anchor="e", bg="#fffbcc",
                              relief="solid", bd=1, font=T.FONT_BASE_LG)  # 목표수량
            lbl_tg.grid(row=r, column=col["tg"], padx=1, pady=1, sticky="nsew")
            lbl_per = tk.Label(grid, text="-", width=5, anchor="e", bg="#f0f0f0",
                               relief="solid", bd=1, font=T.FONT_BASE_LG)  # 1회주문
            lbl_per.grid(row=r, column=col["per"], padx=1, pady=1, sticky="nsew")
            e_en_sf = tk.Entry(grid, width=5, justify="right", validate="key",
                               validatecommand=vcmd_dec, font=T.FONT_NUM_LG)  # 진입 SF
            if spec.has_en_sf:  # 주식은 칸을 만들되 안 붙인다(값은 비어 있음 — 아래 코드 공용)
                e_en_sf.grid(row=r, column=col["en_sf"], padx=1, pady=1, sticky="nsew")
            e_en_s = tk.Entry(grid, width=5, justify="right", validate="key",
                              validatecommand=vcmd_dec, font=T.FONT_NUM_LG)  # 진입 S
            e_en_s.grid(row=r, column=col["en_s"], padx=1, pady=1, sticky="nsew")
            btn_en = tk.Button(grid, text="진입", width=3, padx=0, pady=0,
                               bd=1, highlightthickness=0)
            btn_en.grid(row=r, column=col["btn_en"], padx=1, pady=1, sticky="nsew")
            e_ex_sf = tk.Entry(grid, width=5, justify="right", validate="key",
                               validatecommand=vcmd_dec, font=T.FONT_NUM_LG)  # 청산 SF(주식: S)
            e_ex_sf.grid(row=r, column=col["ex_sf"], padx=1, pady=1, sticky="nsew")
            btn_ex = tk.Button(grid, text="청산", width=3, padx=0, pady=0,
                               bd=1, highlightthickness=0)
            btn_ex.grid(row=r, column=col["btn_ex"], padx=1, pady=1, sticky="nsew")
            btn_set = tk.Button(grid, text="설정", width=3, padx=0, pady=0,
                                bd=1, highlightthickness=0,
                                command=partial(open_set_dialog, dtag, i))
            btn_set.grid(row=r, column=col["set"], padx=1, pady=1, sticky="nsew")
            lbl_rt = tk.Label(grid, text="-", width=7, anchor="e", bg="white",
                              relief="solid", bd=1, font=T.FONT_BASE_LG)  # RT선진입
            lbl_rt.grid(row=r, column=col["rt"], padx=1, pady=1, sticky="nsew")
            lbl_diff = tk.Label(grid, text="-", width=6, anchor="e", bg="white",
                                relief="solid", bd=1, font=T.FONT_BASE_LG)  # 체결차
            lbl_diff.grid(row=r, column=col["diff"], padx=1, pady=1, sticky="nsew")
            lbl_sec = tk.Label(grid, text="-", width=3, anchor="e", bg="#f0f0f0",
                               relief="solid", bd=1, font=T.FONT_BASE_LG)  # 전환딜레이 초
            lbl_sec.grid(row=r, column=col["sec"], padx=1, pady=1, sticky="nsew")
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

        # 매매결과 값 — Sprd=컬럼헤더 줄, -HP/+SF/-환=세트1~3 줄. 탑·끝 라인 정렬.
        # 세트가 3줄을 넘으면 블록을 그만큼 내려 **끝 줄을 마지막 세트에 맞추고 위를 비운다**
        # (사용자 2026-09-15, 4세트 배치 검토).
        shift = max(0, SET_ROWS[dtag] - 3)
        if shift:
            for lcol, vcol, _comps in acc_cols.values():
                for acc_col in (lcol, vcol):
                    for child in grid.grid_slaves(row=rbase, column=acc_col):
                        child.grid_configure(row=rbase + shift)
        for glabel, (lcol, vcol, comps) in acc_cols.items():
            labels: dict[str, tk.Label] = {"누적": cum_labels[glabel]}
            for ri, comp in enumerate(("Sprd", *comps)):
                tk.Label(grid, text=comp, fg="gray30", font=T.FONT_LABEL).grid(
                    row=rbase + shift + ri + 1, column=lcol, padx=(2, 0), sticky="e")
                v = tk.Label(grid, text="-", width=7, anchor="e", relief="solid",
                             bd=1, font=T.FONT_BASE_LG,
                             bg="#fffbcc" if comp == "Sprd" else "white")
                v.grid(row=rbase + shift + ri + 1, column=vcol, padx=1, pady=1, sticky="nsew")
                labels[comp] = v
            sets[(dtag, 0)].setdefault("_acc", {})[glabel] = labels

    # --- 콜백들(v1: 로컬 동작) ---
    def toggle_run(dtag: str, i: int, side: str) -> None:
        w = sets[(dtag, i)]
        key = f"run_{side}"
        turning_on = not w[key]
        release = False
        if turning_on:
            # 중지(HALTED)는 사람이 직접 풀어야 재개(exec §2) — 확인 뒤 해제 + 실행(2026-09-07)
            live_sets = _live_sets(dtag)
            row_live = live_sets[i] if i < len(live_sets) else {}
            # 중지는 세트 단위 — 진입·청산 어느 쪽이 중지든 해제 확인을 먼저(실측 2026-09-15:
            # 역방향 청산만 중지로 남은 채 진입을 켜니 검은 행 위에서 진입이 돌았다)
            halted_legs = [(k, row_live.get(k) or {}) for k in ("entry", "exit")
                           if isinstance(row_live, dict)
                           and (row_live.get(k) or {}).get("status") == "halted"]
            if halted_legs:
                from .ui_dialog import ask_yes_no

                names = "·".join("진입" if k == "entry" else "청산" for k, _ in halted_legs)
                reason = "\n".join(str(lg.get("halt_reason") or "") for _, lg in halted_legs)
                diff = row_live.get("fill_diff") if isinstance(row_live, dict) else None
                diff_txt = _fmt_num(diff) if isinstance(diff, int | float) else "-"
                if not ask_yes_no(root, "중지 해제",
                                  f"{i + 1}세트가 중지 상태입니다({names}).\n{reason}\n"
                                  f"세트 체결차(장부): {diff_txt}\n\n"
                                  "해제해도 체결차 장부는 그대로입니다. 헤지를 정리했으면\n"
                                  "세트설정의 '체결차 Clear'로 0을 만든 뒤 해제하세요.\n"
                                  "'예' — 세트 중지를 풀고(진입·청산 모두) 실행합니다."):
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
                if en_sf is None and spec.has_en_sf:
                    errs.append("진입SF를 입력하세요")
                if en_s is None:
                    errs.append("진입S를 입력하세요")
            else:  # 청산 실행 — 1회주문·청산 필수
                if w["per"] <= 0:
                    errs.append("1회주문수량을 입력하세요")
                if ex_sf is None:
                    errs.append("청산을 입력하세요")
            # 리스크방지의 진입 기준값 — SF 화면은 진입SF, 주식 화면은 진입S(결정 대기, 화면 골격)
            errs += check_risk(dtag, en_sf if spec.has_en_sf else en_s, ex_sf, *_risk_of(dtag))
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
        # 코어 실행(정·역 공통, 역방향 2026-09-14) — 켤 때 세트 입력값을 먼저 보내고 실행 명령
        block = "entry" if side == "en" else "exit"
        if on:
            send(set_payload(i, w, cur_under(), dtag), "세트 설정")
            if release:  # 중지 해제 먼저(같은 큐라 순서 보장) → 실행
                send({"cmd": "autom_release", "underlying": cur_under(), "set": i,
                      "block": block, "direction": dtag}, "중지 해제")
        send({"cmd": "autom_run", "underlying": cur_under(), "set": i, "block": block,
              "value": on, "direction": dtag},
             "실행" if on else "정지")

    def clear_acc(dtag: str, group: str) -> None:
        accs = sets[(dtag, 0)].get("_acc", {}).get(group, {})
        for lbl in accs.values():
            lbl.config(text="-")
        # 누적은 세트별로 코어가 들고 있다 → 그 방향 전 세트를 **한 명령**으로 clear. 세트마다
        # 따로 보내면 그 사이 스냅샷이 반쯤 지워진 합계를 싣고, 화면은 그 옛 값을 한 번 그렸다가
        # 다시 지워 깜빡였다(사용자 2026-09-15). 코어 스냅샷에 지워진 값이 올 때까지는 "-" 유지.
        block = "entry" if group == "진입" else "exit"
        state_box.setdefault("_acc_clear", {})[(dtag, group)] = time.monotonic()
        send({"cmd": "autom_clear_acc", "underlying": cur_under(), "set": "all",
              "block": block, "direction": dtag}, "누적 clear")

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
        # 기준배수(원, 2026-09-15) = 역산가를 주문단위로 맞출 때의 기준점(0이면 0원 기준 배수).
        # 목업(STG_2/세트설정.png) 6칸 뒤에 한 줄 추가.
        # 주식(사용자 2026-09-16): 목표수량·1회주문수량은 천 단위 쉼표로 보이고 쉼표 입력 허용
        vcmd_qty = (root.register(is_qty_text), "%P") if spec.qty_commas else vcmd_int
        rows = [("목표수량", "target", vcmd_qty), ("1회주문수량", "per", vcmd_qty),
                ("전환딜레이(초)", "delay", vcmd_int), ("진입SF", "en_sf", vcmd_dec),
                (spec.entry_s_label, "en_s", vcmd_dec), ("청산", "ex_sf", vcmd_dec),
                ("주문가 기준배수(원)", "offset", vcmd_int)]
        if not spec.has_en_sf:  # 주식: 진입SF 칸 없음
            rows = [row for row in rows if row[1] != "en_sf"]
        ents: dict[str, tk.Entry] = {}
        inline_map = {"en_sf": "e_en_sf", "en_s": "e_en_s", "ex_sf": "e_ex_sf"}
        for r, (label, key, vc) in enumerate(rows):
            tk.Label(win, text=label, anchor="w").grid(
                row=r, column=0, sticky="w", padx=6, pady=3)
            e = tk.Entry(win, width=10, justify="right", validate="key",
                         validatecommand=vc)
            if key in inline_map:  # 진입SF·진입S·청산 = 화면 인라인 현재값
                e.insert(0, w[inline_map[key]].get())
            else:  # 목표수량·1회주문·전환딜레이·기준배수 = 세트 상태값 (딜레이·기준배수는 0도 값)
                val = w.get(key)
                blank = val is None or (val == 0 and key not in ("delay", "offset"))
                commas = spec.qty_commas and key in ("target", "per")
                e.insert(0, "" if blank else (format_qty(int(val or 0)) if commas else str(val)))
            e.grid(row=r, column=1, padx=6, pady=3)
            ents[key] = e
        # RT 수동 입력·체결차 Clear는 **1회성** — 열 때마다 꺼진 상태로 시작하고 저장하지 않는다
        # (사용자 확정 2026-09-07). 값이 남아 있으면 실행 켤 때마다 RT를 덮어쓰는 사고가 난다.
        rt_var = tk.BooleanVar(value=False)
        # 역방향 RT는 0 또는 음수(보유 = −n)라 '-'를 칠 수 있어야 한다(사용자 2026-09-15).
        # 정방향은 숫자만.
        rt_ent = tk.Entry(win, width=10, justify="right", validate="key",
                          validatecommand=vcmd_sint if dtag == "rev" else vcmd_int)
        tk.Checkbutton(win, text="RT 진입수량 수동 입력" + (" (0 또는 −)" if dtag == "rev" else ""),
                       variable=rt_var).grid(row=len(rows), column=0, sticky="w", padx=6)
        rt_ent.grid(row=len(rows), column=1, padx=6, pady=2)
        diff_var = tk.BooleanVar(value=False)
        tk.Checkbutton(win, text="체결차 Clear", variable=diff_var).grid(
            row=len(rows) + 1, column=0, sticky="w", padx=6, pady=(0, 4))
        # 주식 신용(사용자 2026-09-16): 진입 신용매수 / 청산 신용상환 — 세트 상태값(저장·복원)
        credit_en_var = tk.BooleanVar(value=bool(w.get("credit_en")))
        credit_ex_var = tk.BooleanVar(value=bool(w.get("credit_ex")))
        extra_rows = 0
        if spec.credit_boxes:
            tk.Checkbutton(win, text="진입 신용", variable=credit_en_var).grid(
                row=len(rows) + 2, column=0, sticky="w", padx=6)
            tk.Checkbutton(win, text="청산 신용상환", variable=credit_ex_var).grid(
                row=len(rows) + 2, column=1, sticky="w", padx=6)
            extra_rows = 1

        def save() -> None:
            en_sf = parse_threshold(ents["en_sf"].get()) if "en_sf" in ents else None
            en_s = parse_threshold(ents["en_s"].get())
            ex_sf = parse_threshold(ents["ex_sf"].get())
            target = parse_qty(ents["target"].get())
            per = parse_qty(ents["per"].get())
            errs: list[str] = []  # 필수 입력 검사(목표·1회주문·진입SF·진입S·청산)
            if target <= 0:
                errs.append("목표수량을 입력하세요")
            if per <= 0:
                errs.append("1회주문수량을 입력하세요")
            if en_sf is None and spec.has_en_sf:
                errs.append("진입SF를 입력하세요")
            if en_s is None:
                errs.append("진입S를 입력하세요")
            if ex_sf is None:
                errs.append("청산을 입력하세요")
            errs += check_risk(dtag, en_sf if spec.has_en_sf else en_s, ex_sf, *_risk_of(dtag))
            offset = parse_qty(ents["offset"].get())
            sf_tick = _live_book().get("sf_tick")  # 코어가 실은 지금 가격대의 SF 호가단위
            errs += price_offset_errors(offset, int(common["pre_tick"].get(cur_under(), 0)),
                                        int(sf_tick) if isinstance(sf_tick, int) else None)
            if rt_var.get() and not rt_ent.get().strip():  # 체크만 하고 값 없음 → 확인창
                errs.append("RT 진입수량 수동 입력이 켜져 있는데 값이 없습니다")
            elif rt_var.get():
                errs += rt_manual_errors(dtag, rt_ent.get())
            if errs:  # 필수 미입력·위반 — 경고만, 저장·닫기 안 함
                warn_center("\n".join(errs))
                return
            w["target"], w["per"] = target, per
            w["delay"] = parse_qty(ents["delay"].get())
            w["offset"] = offset
            w["en_sf"], w["en_s"], w["ex_sf"] = en_sf, en_s, ex_sf
            w["rt_manual"] = parse_qty(rt_ent.get()) if rt_var.get() else None
            w["clear_diff"] = diff_var.get()
            w["credit_en"], w["credit_ex"] = credit_en_var.get(), credit_ex_var.get()
            apply_set_display(dtag, i)
            win.destroy()
            # 코어에 세트 설정 전송(실행 중에도 가능 — 코어가 다음 판정부터 반영)
            send(set_payload(i, w, cur_under(), dtag), "세트 설정")
            # 1회성 항목은 보낸 즉시 비운다 — 뒤의 실행 켬(set_payload 재전송)·저장에 안 실리게
            w["rt_manual"] = None
            w["clear_diff"] = False

        btns = tk.Frame(win)
        btns.grid(row=len(rows) + 2 + extra_rows, column=0, columnspan=2, pady=(4, 6))
        tk.Button(btns, text="확인", width=8, command=save).pack(side="left", padx=4)
        tk.Button(btns, text="취소", width=8, command=win.destroy).pack(side="left", padx=4)
        _center(win)

    def apply_set_display(dtag: str, i: int) -> None:
        w = sets[(dtag, i)]
        # 신용 구분은 실행 버튼 글자로(진입→신용, 청산→상환) — 색은 진행 상태 그대로(09-16)
        w["btn_en"].config(text=run_caption("en", bool(w.get("credit_en"))))
        w["btn_ex"].config(text=run_caption("ex", bool(w.get("credit_ex"))))
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
        win.title(spec.settings_title)
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
        # 줄2 왼쪽 — 상대호가 콤보. 라벨은 매수/매도(SF 주문 방향) — 09-11에 '진입/청산'으로
        # 바꿨다가 역방향(진입=SF 매도)에선 헷갈려 09-14 사용자가 되돌림. 코어도 주문 방향으로
        # 고른다(§6.3).
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
        tk.Label(pr, text="시장멈춤 재개 딜레이(초)").grid(row=1, column=0, sticky="e", pady=2)
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
        if "rev" in dirs:  # 주식 화면은 역방향 없음(사용자 2026-09-16)
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
    rbase = 0
    for di, (dtag, name, en_sf, en_s, ex_sf) in enumerate(spec.directions):
        # 정방향 0~(높이−1), 구분선 1줄, 역방향 그 다음 — 기본 3·3세트면 0~4 / 5 / 6~10
        if di > 0:
            rbase += section_height(SET_ROWS[spec.directions[di - 1][0]]) + 1
            ttk.Separator(board, orient="horizontal").grid(
                row=rbase - 1, column=0, columnspan=16, sticky="ew", pady=3)
        build_section(board, rbase, dtag, name, en_sf, en_s, ex_sf, spec.acc_rows[dtag])

    if preview:  # 최대 폭 샘플로 칸 폭 테스트 (세트설정 목업: 진입SF 0.5·진입S 0.5·청산 -0.1)
        acc_sample = {"누적": "99,999", "Sprd": "-0.825", "-HP": "9,999",
                      "+HP": "9,999", "+SF": "99,999", "-SF": "99,999",
                      "+S": "999,999", "-S": "999,999",  # 주식 평균 체결가(원)
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
            if spec.credit_boxes and _i == 1:  # 2세트만 신용 표본 — 버튼 글자 신용/상환 대조
                w["credit_en"] = w["credit_ex"] = True
                w["btn_en"].config(text=run_caption("en", True))
                w["btn_ex"].config(text=run_caption("ex", True))
        for d in dirs:
            for labels in sets[(d, 0)].get("_acc", {}).values():
                for comp, lbl in labels.items():
                    lbl.config(text=acc_sample.get(comp, "-"))

    # width=1: 상태줄 글이 길어도(중지 사유 등) 창 폭을 밀어 키우지 않게 — 라벨이 요구하는 폭을
    # 1글자로 두고 pack(fill="x")로 창 폭만큼만 보인다(넘치는 글은 잘림, 전문은 로그에). 09-08.
    status_text = "UI 미리보기 — 코어 미연결" if preview else "코어 확인 중 ..."
    last_tag, last_rows = spec.directions[-1][0], SET_ROWS[spec.directions[-1][0]]
    if last_rows < 3:
        # 마지막 방향의 세트가 3줄 미만이면 매매결과 블록 옆 빈 줄에 상태줄을 넣어 창 높이를 아낀다
        # (사용자 2026-09-15, 역방향 2세트 배치). 세트 컬럼(0~10) 폭만 차지.
        status = tk.Label(board, anchor="w", relief="groove", width=1, text=status_text)
        status.grid(row=rbase + 2 + last_rows, column=0, columnspan=11, sticky="ew",
                    padx=1, pady=(3, 1))
    else:
        status = tk.Label(root, anchor="w", relief="groove", width=1, text=status_text)
        status.pack(fill="x", padx=4, pady=(2, 4))
    del last_tag
    from .ui_dialog import attach_full_text_popup

    attach_full_text_popup(status, root)  # 더블클릭 → 잘린 상태줄 전문을 힌트 창으로(2026-09-15)
    refresh_windows_bar()  # 상단 주문가능시간 표시 초기화

    def _live_book() -> dict[str, Any]:
        """이 창 종목의 실시간 책 스냅샷(autom_live[종목]) — 없으면 빈 dict."""
        data = state_box.get("data") or {}
        live = (data.get("autom_live") or {}).get(cur_under())
        return live if isinstance(live, dict) else {}

    def _live_sets(dtag: str = "fwd") -> list[dict[str, Any]]:
        rows = _live_book().get("sets" if dtag == "fwd" else "rev_sets")
        return rows if isinstance(rows, list) else []

    def _any_running() -> bool:
        return any(bool((r.get("entry") or {}).get("running"))
                   or bool((r.get("exit") or {}).get("running"))
                   for d in dirs for r in _live_sets(d))

    def _set_status(text: str) -> None:
        status.config(text=text)

    # 창 닫기(X) — 자동주문 화면 공통 규칙(DESIGN-ui §6): 실행 중이면 확인 뒤 정지하고 닫기.
    # 정지 대상은 **이 창이 보여주는 종목**뿐 — 다른 종목은 다른 창/코어에서 계속(2026-09-08).
    if not preview:
        attach_auto_close(
            root, title=spec.title, is_running=_any_running,
            send_stop=lambda: send({"cmd": "autom_stop_all", "underlying": cur_under()},
                                   "이 종목 전 세트 정지"),
            set_status=_set_status)

    # --- 화면 저장/복원 (win_state.autoM — 2초 자동저장, order_hl과 동일 방식) ---
    def _collect_fields() -> dict[str, Any]:
        # 세트 값(목표·1회·기준값·RT·체결차)은 코어가 종목별로 든다(단일 진실, 2026-09-08) —
        # 창은 종목·호가단위·기준수량 표시값과 체결쏴 설정(공통)만 저장한다.
        return {
            "under": cb_under.get(), "agg": cb_agg.get(), "refqty": ent_refqty.get(),
            "bg": bg_state["name"],  # 콤보 이름 또는 '#rrggbb'
            "fg": fg_state["cur"],   # 글자색 '#rrggbb' 또는 None(기본)
            "common": {"windows": list(common["windows"]),
                       "pre_tick": dict(common["pre_tick"]),
                       "pre_delay": common["pre_delay"], "resume_delay": common["resume_delay"],
                       "pre_range": common["pre_range"],
                       "rel_buy": common["rel_buy"], "rel_sell": common["rel_sell"],
                       "hl_margin_buy": common.get("hl_margin_buy", 1.0),
                       "hl_margin_sell": common.get("hl_margin_sell", 1.0),
                       "risk": dict(common["risk"])}}

    def _restore_saved() -> None:
        saved = win_state.saved_fields(spec.state_key)
        if not saved:
            return
        if saved.get("under") in cb_under["values"]:  # 현대차 등 제외된 종목은 복원 안 함
            cb_under.set(saved["under"])
        if isinstance(saved.get("agg"), str):
            state_box["_want_agg"] = saved["agg"]  # 틱 목록이 오면 그때 고른다
        if isinstance(saved.get("refqty"), str):
            ent_refqty.delete(0, "end")
            ent_refqty.insert(0, saved["refqty"])
        bg_saved = saved.get("bg")
        if isinstance(bg_saved, str) and (bg_saved in _BG_CHOICES
                                          or re.fullmatch(r"#[0-9a-fA-F]{6}", bg_saved)):
            if bg_saved in _BG_CHOICES:
                cb_bg.set(bg_saved)
            apply_bg(bg_saved)  # 바탕색 — 창을 다 그린 뒤 적용(콤보 이름 또는 대화상자 색)
        fg_saved = saved.get("fg")
        if isinstance(fg_saved, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", fg_saved):
            apply_fg(fg_saved)  # 글자색(주식 화면 색 고르기, 2026-09-16)
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

    if preview and os.environ.get("KP_PREVIEW_BG"):  # 미리보기 캡처용 바탕색(이름 또는 #rrggbb)
        if os.environ["KP_PREVIEW_BG"] in _BG_CHOICES:
            cb_bg.set(os.environ["KP_PREVIEW_BG"])
        apply_bg(os.environ["KP_PREVIEW_BG"])
    if preview and os.environ.get("KP_PREVIEW_FG"):  # 미리보기 캡처용 글자색(#rrggbb)
        apply_fg(os.environ["KP_PREVIEW_FG"])
    if not preview:  # 미리보기는 저장/복원 제외(샘플과 실제 저장 분리)
        _restore_saved()

        def _persist_fields() -> None:
            try:
                win_state.save_fields(spec.state_key, _collect_fields())
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
                   "rev_en_sf": "0.12", "rev_en_s": "0.12", "rev_ex_sf": "0.23",
                   "fx": fx_caption(1349.6, "현물")}

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
        text = run_caption(side, bool(w.get(f"credit_{side}")))  # 주식 신용이면 신용/상환
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

    def _leg_detail(i: int, side: str, leg: dict[str, Any], dtag: str = "fwd") -> str | None:
        """상태줄용 진행 상세 — 감시/대기 외 상태만 한 줄. 역방향은 '역' 표시."""
        st = str(leg.get("status") or "idle")
        reject = str(leg.get("reject") or "")
        if st in ("idle", "armed") and not reject:
            return None
        name = "진입" if side == "en" else "청산"
        head = f"{'역 ' if dtag == 'rev' else ''}{i + 1}세트 {name}"
        parts = [f"{head}: {_STATUS_TEXT.get(st, st)}"]
        if reject:  # 마지막 선주문 거부 — 시각·횟수·LS 사유(사용자 2026-09-15). 다음 접수 때 사라짐
            parts.append(f"{leg.get('reject_at') or ''} {reject}".strip())
        if leg.get("pre_order_id"):
            price = leg.get("pre_price")
            px = f"{float(price):,.0f}" if isinstance(price, int | float) else "-"
            unit = spec.qty_unit
            parts.append(f"선주문 #{leg['pre_order_id']} {leg.get('pre_qty', 0)}{unit} @{px}"
                         f" (체결 {leg.get('pre_filled', 0)}/{leg.get('pre_qty', 0)})")
        if leg.get("cancel_failed"):  # 취소 재전송 한도 초과(exec ㅂ3) — 수동 취소 유도
            parts.append(f"취소실패 {leg.get('cancel_tries', 0)}회 — 수동 취소 확인")
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
        if month_label and spec.has_month:
            cb_month.set(month_label)
        ref = book.get("ref_qty")
        if isinstance(ref, int):
            ent_refqty.delete(0, "end")
            ent_refqty.insert(0, str(ref))
            ref_sent["qty"] = ref
        state_box["_sets_sig"] = set_inputs_sig(book)
        _load_set_inputs(rows, "fwd")
        _load_set_inputs(book.get("rev_sets") or [], "rev")

    def _load_set_inputs(rows: list[Any], dtag: str, skip_focused: bool = False) -> None:
        """코어 책의 세트 입력값(목표·1회·전환초·진입SF·진입S·청산)을 그 방향 3세트 칸에 채운다.
        skip_focused: 지금 인라인 칸을 편집 중인 세트는 건너뛴다(입력 중 값이 튀지 않게)."""
        focused = root.focus_get() if skip_focused else None
        for i, raw in enumerate(rows[:SET_ROWS[dtag]]):
            if not isinstance(raw, dict):
                continue
            w = sets[(dtag, i)]
            if focused is not None and focused in (w["e_en_sf"], w["e_en_s"], w["e_ex_sf"]):
                continue
            w["target"] = int(raw.get("target_qty") or 0)
            w["per"] = int(raw.get("per_qty") or 0)
            w["delay"] = int(raw.get("switch_delay_s") or 0)
            w["offset"] = int(raw.get("price_offset") or 0)
            for key in ("en_sf", "en_s", "ex_sf"):
                v = raw.get(key)
                w[key] = float(v) * 100.0 if isinstance(v, int | float) else None
            apply_set_display(dtag, i)

    def _sync_set_inputs_from_core(data: dict[str, Any]) -> None:
        """다른 창에서 같은 종목의 세트설정을 바꿨으면(코어 값 변경) 이 창도 따라간다.

        실측 2026-09-09: 두 창을 열고 한 창에서 세트설정을 저장했는데 다른 창은 옛 값 그대로 —
        그 창에서 다시 저장하면 옛 값으로 덮어쓴다. 코어가 원본이므로 바뀔 때만 다시 채운다
        (세트설정이 코어에 가는 시점 = 설정창 '확인'·실행 켬, 사용자 확정 2026-09-09).
        """
        book = ((data.get("autom") or {}).get("books") or {}).get(cur_under()) or {}
        sig = set_inputs_sig(book)
        if not sig or sig == state_box.get("_sets_sig"):
            return
        state_box["_sets_sig"] = sig
        _load_set_inputs(book.get("sets") or [], "fwd", skip_focused=True)
        _load_set_inputs(book.get("rev_sets") or [], "rev", skip_focused=True)

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
            [am.get(k) for k in ("risk_fwd_en", "risk_fwd_ex", "risk_fwd_gap",
                                 "risk_rev_en", "risk_rev_ex", "risk_rev_gap")])
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
                         ("risk_fwd_gap", "fwd_gap"), ("risk_rev_en", "rev_en"),
                         ("risk_rev_ex", "rev_ex"), ("risk_rev_gap", "rev_gap")):
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
        else:
            _sync_set_inputs_from_core(data)  # 다른 창이 바꾼 세트설정 반영(같은 종목)
        details: list[str] = []
        for dtag in dirs:  # 방향별 세트(역방향 2026-09-14, 주식 화면은 정방향만)
            for i, row in enumerate(_live_sets(dtag)[:SET_ROWS[dtag]]):
                w = sets[(dtag, i)]
                en, ex = row.get("entry") or {}, row.get("exit") or {}
                _paint_leg(w, "en", en)
                _paint_leg(w, "ex", ex)
                _paint_halt(w, en.get("status") == "halted" or ex.get("status") == "halted")
                for side, leg in (("en", en), ("ex", ex)):
                    detail = _leg_detail(i, side, leg, dtag)
                    if detail:
                        details.append(detail)
                # RT는 부호 그대로 — 역방향은 0 또는 음수(사용자 확정 2026-09-14)
                w["rt"].config(text=_fmt_num(row.get("rt")))
                w["diff"].config(text=_fmt_num(row.get("fill_diff")))
        # 상단 모니터 3칸 — 코어가 기준수량으로 계산한 est 괴리(%), 정/역 각각.
        # '적'을 누른 뒤부터 표시(종목·호가단위·기준수량이 코어에 적용된 값이라야 뜻이 있음).
        monitor = _live_book().get("monitor") or {}
        fx = _live_book().get("fx") or {}
        mon["fx"].config(text=fx_caption(fx.get("used"), fx.get("src")))
        # 표시 여부는 화면 입력칸이 아니라 **코어가 실제로 쓰는 기준수량**으로 판단 —
        # 입력칸을 지우는 중에도 수치가 사라지지 않게(사용자 2026-09-07).
        core_ref = int(_live_book().get("ref_qty") or 0)
        applied = bool(state_box.get("_applied")) and core_ref > 0
        for dtag in dirs:
            vals = monitor.get(dtag) or {}
            for skey in ("en_sf", "en_s", "ex_sf"):
                if f"{dtag}_{skey}" not in mon:  # 주식 화면엔 진입SF 칸이 없다
                    continue
                v = vals.get(skey)
                text = (f"{float(v) * 100:.2f}"
                        if applied and isinstance(v, int | float) else "-")
                mon[f"{dtag}_{skey}"].config(text=text)
        # 매매결과 누적(정·역 각각) — 진입 = entry 누적, 청산 = exit 누적(세트 합산). 칸 이름은
        # 방향별 배치표(_ACC_ROWS_*)에서: 정방향 진입 -HP/+SF/-환, 역방향 진입 +HP/-SF/+환 …
        for dtag, acc_rows in spec.acc_rows.items():
            accs = sets[(dtag, 0)].get("_acc", {})
            rows = _live_sets(dtag)
            for glabel, leg in (("진입", "entry"), ("청산", "exit")):
                labels = accs.get(glabel, {})
                if not labels or not rows:
                    continue
                agg = sum_acc(rows, leg)
                if acc_clear_pending(state_box.get("_acc_clear") or {}, (dtag, glabel),
                                     float(agg["hl_qty"] or 0), time.monotonic()):
                    continue  # clear 눌렀는데 아직 옛 합계 — "-" 그대로 둔다
                hp_key, s_key, fx_key = dict(acc_rows)[glabel]
                # 누적체결량 칸 = 짝이 맞은 HL 체결량(사용자 2026-09-09) — 소수면 자릿수를 붙인다
                hl_q = float(agg["hl_qty"] or 0)
                labels["누적"].config(text=_fmt_num(hl_q, 3 if hl_q % 1 else 0))
                # HP/SF/환 = Sprd 식의 세 입력값(HL 평균 체결가·SF 평균 체결가·환진입가, 엑셀 메인
                # I28/I29/I27). 수량을 보여주던 것을 바로잡음(사용자 2026-09-11).
                labels[hp_key].config(text=_fmt_num(agg["hl_avg"], 2))
                labels[s_key].config(text=_fmt_num(agg["sf_avg"], 0))
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
        except Exception:  # noqa: BLE001 - 갱신 한 번의 오류로 화면이 죽은 것처럼 멈추면 안 됨
            # 예외가 새면 after 사슬이 끊겨 값이 멈추고 버튼을 눌러도 반응이 없어 보인다
            # (실측 2026-09-09 "컨트롤이 안 눌린다"). 원인은 화면 로그에 남기고 계속 돈다.
            now = time.time()
            if now - float(state_box.get("_err_logged", 0.0)) > 5.0:
                state_box["_err_logged"] = now
                screen_log().exception("자동M 화면 갱신 오류 — 계속")
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
