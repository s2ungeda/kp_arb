"""화면 공통 스타일 토큰 — 단일 진실. 색·역할은 일반주문창(order_hl) 기준,
기본 폰트는 **일반 창(base 9)** 기준(order_hl 등 큰 화면은 예외적으로 _LG 사용).

모든 창은 raw 폰트 튜플·색 문자열을 직접 쓰지 말고 이 모듈의 **'역할' 토큰**만 참조한다.
크기·색을 바꾸려면 여기 한 곳만 고치면 전 화면이 따라온다. 규칙·표는 DESIGN-ui.md(계약).

  from . import ui_theme as T
  T.apply_base(root)
  tk.Label(f, text="수량", font=T.FONT_LABEL)
  val.config(text=..., font=T.FONT_NUM, fg=T.sign_color(v))
"""
from __future__ import annotations

from typing import Any

FAMILY = "Malgun Gothic"

# --- 역할별 폰트 ---
# 기본은 **일반 창 기준(base 9)** — 대부분 창(order_list·monitor·main 등)이 이 크기.
FONT_BASE = (FAMILY, 9)             # 창 기본(라벨·버튼·체크박스·일반 텍스트)
FONT_NUM = (FAMILY, 9, "bold")      # 숫자·입력값 — 규칙: **숫자는 볼드**
FONT_LABEL = (FAMILY, 9)            # 표 제목·상태바·콤보 (= FONT_BASE 크기, 의미 구분용)
FONT_SMALL = (FAMILY, 8)            # 보조 캡션(Cross·Unified 등)
FONT_GRID = (FAMILY, 10, "bold")    # 표(Treeview: 호가창 등)
FONT_STRONG = (FAMILY, 14, "bold")  # 강조 버튼(주문)
ROWHEIGHT = 22                      # Treeview 행 높이
# 큰 화면(정보밀도 높은 일반주문창 order_hl 등) — 예외적으로 큼(base 11).
FONT_BASE_LG = (FAMILY, 11)         # 큰 화면 기본
FONT_NUM_LG = (FAMILY, 11, "bold")  # 큰 화면 숫자·입력값

# --- 의미별 색 ---
C_BUY = "#c00000"           # 매수·이익 (빨강)
C_SELL = "#0000c0"          # 매도·손실 (파랑)
C_ZERO = "black"            # 0·중립
C_BUY_ACTIVE = "#a00000"    # 매수 버튼 눌림
C_SELL_ACTIVE = "#000090"   # 매도 버튼 눌림
C_HILITE_BG = "#ffe680"     # 강조 바탕(잔고·PNL)
C_QUERY_LBL = "gray45"      # 조회(REST) 항목 라벨 — 회색 구분
C_ERR = "#8b0000"           # 거부·실패
C_MUTED = "gray30"          # 보조·안내(상태바 일반)
C_BORDER = "gray50"         # 셀 테두리


def sign_color(v: Any) -> str:
    """부호별 글자색 — 양수(매수/이익)=빨강, 음수(매도/손실)=파랑, 0·None·이상=검정."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return C_ZERO
    if f > 0:
        return C_BUY
    if f < 0:
        return C_SELL
    return C_ZERO


def apply_base(root: Any) -> None:
    """창 기본 스타일 적용(기본 폰트 + Treeview) — 각 창 생성 직후 한 번 호출."""
    from tkinter import ttk

    root.option_add("*Font", FONT_BASE)
    ttk.Style().configure("Treeview", font=FONT_GRID, rowheight=ROWHEIGHT)
