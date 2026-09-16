"""주문 화면 공용 입력 헬퍼·상수 — 종목 표시명 매핑, 입력칸 필터, 수량·기준값 파싱(순수).

원래 자동T 화면(order_autot)에 있던 것을 2026-09-16 자동T 화면 삭제(체결쏴 T 모드로 대체 예정,
사용자 결정) 때 여기로 옮겼다. 체결쏴(order_autom)·HL 일반주문(order_hl)이 쓴다.
"""
from __future__ import annotations

import re

UNDERLYINGS = ("하이닉스", "삼성", "현대차")
UNDER_MAP = {"하이닉스": "sk_hynix", "삼성": "samsung", "현대차": "hyundai"}

# 국내 신용거래 주문유형 코드 (DESIGN-auto-t §9) — 체결쏴 주식 신용 주문에서 쓸 참고표
ORDER_TYPES: dict[str, str] = {
    "00": "보통", "003": "유통/자기융자신규", "005": "유통대주신규",
    "007": "자기대주신규", "101": "유통융자상환", "103": "자기융자상환",
    "105": "유통대주상환", "107": "자기대주상환", "180": "예탁담보대출상환(신용)",
}


def is_int_text(text: str) -> bool:
    """정수 입력칸 허용 — 빈칸 또는 숫자만."""
    return text == "" or text.isdigit()


def is_qty_text(text: str) -> bool:
    """수량 입력칸 — 숫자와 천 단위 쉼표만(빈칸 허용). 체결쏴 주식 세트설정(사용자 2026-09-16:
    목표수량·1회주문수량 천 단위 쉼표 표시)."""
    return re.fullmatch(r"[\d,]*", text) is not None


def format_qty(value: int) -> str:
    """수량 표시 — 천 단위 쉼표(10000 → '10,000')."""
    return f"{int(value):,}"


def is_signed_int_text(text: str) -> bool:
    """부호 있는 정수 입력칸 — 빈칸·'-'(입력 중)·-?숫자. 역방향 RT 수동 입력용(사용자 2026-09-15:
    역방향 RT는 0 또는 음수라 '-'를 칠 수 있어야 한다)."""
    return re.fullmatch(r"-?\d*", text) is not None


def is_decimal_text(text: str) -> bool:
    """소수 입력칸 허용 — 부호·소수점 포함 숫자 형태(입력 중간 상태 허용)."""
    return re.fullmatch(r"-?\d*\.?\d*", text) is not None


def is_time_text(text: str) -> bool:
    """시:분:초 입력 허용 — 숫자와 콜론만(입력 중간 상태 허용)."""
    return re.fullmatch(r"[\d:]*", text) is not None


def parse_qty(text: str) -> int:
    """수량 → int. 빈칸/오타는 0. 천 단위 쉼표는 무시('10,000' → 10000)."""
    try:
        return int(text.strip().replace(",", ""))
    except ValueError:
        return 0


def parse_threshold(text: str) -> float | None:
    """기준값(%) → float(% 단위 그대로). 빈칸/오타는 None."""
    try:
        return float(text.strip())
    except ValueError:
        return None
