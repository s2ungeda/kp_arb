"""캐리 이론가·만기 계산 — 순수 로직 (DESIGN §6.1 전략 v0.1).

- 주식선물 이론가 = 기초 주식 현재가 × (1 + 연 3.5% × 잔존일/365) — 배당 무시
- 환율이론가   = 원달러선물 현재가 × (1 + 연 1.5% × 잔존일/365) — 현물환율 환산
- 만기(최종거래일): 주식/지수선물 = 둘째 목요일, 미국달러선물 = 셋째 월요일
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

EQ_CARRY_RATE = 0.035   # 주식선물 캐리 연이자율 (config로 이동 가능)
FX_CARRY_RATE = 0.015   # 환율(통화선물→현물) 환산 연이자율


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """해당 월의 n번째 weekday(월=0). 예: 셋째 월요일 = nth_weekday(y, m, 0, 3)."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def expiry_date(yyyymm: int, product: str) -> date:
    """최종거래일. product: "EQ"=주식/지수선물(둘째 목요일) / "USD"=미국달러선물(셋째 월요일)."""
    year, month = yyyymm // 100, yyyymm % 100
    if product == "USD":
        return nth_weekday(year, month, 0, 3)
    return nth_weekday(year, month, 3, 2)


def days_to_expiry(yyyymm: int, product: str, today: date) -> int:
    """잔존일(최소 1) — 캐리 이론가 계산용."""
    remain = (expiry_date(yyyymm, product) - today).days
    return remain if remain > 0 else 1


def is_rolled(yyyymm: int, product: str, now: datetime) -> bool:
    """해당 월물이 롤오버 시점(최종거래일 15:45)을 지났는가."""
    exp = expiry_date(yyyymm, product)
    if now.date() > exp:
        return True
    return now.date() == exp and (now.hour * 60 + now.minute) >= 15 * 60 + 45


def carry_theory(base_price: float, days: int, annual_rate: float) -> float:
    """비용캐리 이론가 = 가격 × (1 + 연이자율 × 잔존일/365)."""
    return base_price * (1.0 + annual_rate * days / 365.0)


def parse_ym(hname: str) -> int | None:
    """선물 hname에서 만기 YYYYMM 추출 (t8401/t8426 = 6자리, 지수 t9943 = YYMM 4자리)."""
    m6 = re.findall(r"\d{6}", hname or "")
    if m6:
        return int(m6[-1])
    m4 = re.findall(r"\d{4}", hname or "")
    if m4:
        return (2000 + int(m4[-1][:2])) * 100 + int(m4[-1][2:])
    return None


def select_usd_futures(
    rows: list[dict[str, object]], now: datetime
) -> tuple[str, int] | None:
    """t8426(상품선물 마스터) 행에서 미국달러선물 **최근월물** (shcode, yyyymm).

    "미국달러 ... F ..." 월물만(스프레드 SP 제외), 롤오버 지난 월물 제외.
    """
    candidates: list[tuple[int, str]] = []
    for row in rows:
        hname = str(row.get("hname", ""))
        shcode = str(row.get("shcode", "")).strip()
        if "미국달러" not in hname or " F " not in hname or " SP " in hname:
            continue
        if not shcode:
            continue
        ym = parse_ym(hname)
        if ym is not None:
            candidates.append((ym, shcode))
    for ym, shcode in sorted(candidates):
        if not is_rolled(ym, "USD", now):
            return shcode, ym
    return None
