"""화면 공통 스타일 토큰 — **일반주문창(order_hl)을 기준**으로 굳힌 단일 진실.

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

# --- 역할별 폰트 (일반주문창 기준) ---
FONT_BASE = (FAMILY, 11)            # 창 기본(라벨·일반 텍스트)
FONT_NUM = (FAMILY, 11, "bold")     # 숫자·입력값 — 규칙: **숫자는 볼드**
FONT_GRID = (FAMILY, 10, "bold")    # 표(Treeview: 호가창·주문목록)
FONT_LABEL = (FAMILY, 9)            # 표 제목·상태바·콤보
FONT_SMALL = (FAMILY, 8)            # 보조 캡션(Cross·Unified 등)
FONT_STRONG = (FAMILY, 14, "bold")  # 강조 버튼(주문)
ROWHEIGHT = 22                      # Treeview 행 높이

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
