"""캐리 이론가·만기 계산 — 순수 로직 (DESIGN §6.1 전략 v0.1).

- 주식선물 이론가 = 기초 주식 현재가 × (1 + 연 3.0% × 잔존일/365) — 배당 무시 (공통설정)
- 환율이론가   = 원달러선물 현재가 × (1 + 연 1.0% × 잔존일/365) — 현물환율 환산 (공통설정)
- 만기(최종거래일): 주식/지수선물 = 둘째 목요일, 미국달러선물 = 셋째 월요일
- 롤오버 시점: 최종거래일 15:45(정규장 마감) 이후는 다음 월물로
  (만기 규칙·15:45 롤 모두 사용자 확인 2026-09-14 — KRX 파생상품 시장 규정)
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

EQ_CARRY_RATE = 0.030   # 주식선물 캐리 연이자율 기본(공통설정에서 사용자 조정)
FX_CARRY_RATE = 0.010   # 환율(통화선물→현물) 환산 연이자율 기본(공통설정에서 사용자 조정)


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


def business_days_until(today: date, target: date) -> int:
    """오늘 다음 날부터 target까지의 영업일 수(월~금). target이 오늘이면 0, 지났으면 음수.
    공휴일은 빼지 않는다 — 휴장일 달력이 코어에 없다([OPEN], 결정 75)."""
    if target <= today:
        return -sum(1 for k in range((today - target).days)
                    if (target + timedelta(days=k)).weekday() < 5)
    return sum(1 for k in range(1, (target - today).days + 1)
               if (today + timedelta(days=k)).weekday() < 5)


# 차근월물 선택 가능 구간 — 최근월물 만기 5영업일 전부터(사용자 2026-10-14, 결정 75;
# 5일 → 3일 → 같은 날 다시 5일로 정정)
NEXT_MONTH_WINDOW_DAYS = 5


def next_month_window(yyyymm: int, product: str, today: date,
                      window: int = NEXT_MONTH_WINDOW_DAYS) -> tuple[bool, str]:
    """차근월물을 고를 수 있는가(결정 75): 최근월물 만기(최종거래일) **5영업일 전부터** 만기일까지.
    (허용 여부, 설명) — 설명은 화면·거부 사유용. product: "EQ" 주식선물 / "USD" 원달러선물."""
    exp = expiry_date(yyyymm, product)
    left = business_days_until(today, exp)
    if left < 0:
        return True, f"최근월물 {yyyymm % 100}월 만기 {exp.month}-{exp.day} 지남"
    if left <= window:
        return True, f"최근월물 만기 {exp.month}-{exp.day}까지 {left}영업일 — 차근 선택 가능"
    return False, (f"차근월물은 최근월물 만기({exp.month}-{exp.day}) {window}영업일 전부터 — "
                   f"지금 {left}영업일 남음")


def is_rolled(yyyymm: int, product: str, now: datetime) -> bool:
    """해당 월물이 롤오버 시점(최종거래일 15:45)을 지났는가."""
    exp = expiry_date(yyyymm, product)
    if now.date() > exp:
        return True
    return now.date() == exp and (now.hour * 60 + now.minute) >= 15 * 60 + 45


def parse_hhmm(text: str) -> time:
    """"07:50" / "08:45:00" 같은 시:분[:초] 문자열 → time. 형식이 틀리면 ValueError.
    (초는 외환선물 우선시간 입력용 — 사용자 2026-09-28.)"""
    parts = text.split(":")
    if len(parts) == 2:
        return time(int(parts[0]), int(parts[1]))
    if len(parts) == 3:
        return time(int(parts[0]), int(parts[1]), int(parts[2]))
    raise ValueError(f"시:분[:초] 형식이 아님: {text!r}")


def in_time_window(now: time, start: time, end: time) -> bool:
    """now가 [start, end) 창 안인가. start > end면 자정을 넘는 창으로 해석."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def weighted_quote_price(bid: float, ask: float, bid_qty: float | None,
                         ask_qty: float | None) -> float | None:
    """1호가 가중값(사용자 2026-10-27 식, 주식 "현재가" 대용 — DESIGN-ls-xing §9) =
    매수1 + (매도1 − 매수1) × 매수1잔량/(매도1잔량 + 매수1잔량).
    매수 잔량이 많을수록 매도1 쪽으로 붙는다. 잔량 합이 0(또는 없음)이면 중간값. 호가가 0 이하면
    None. 순수."""
    if bid <= 0 or ask <= 0:
        return None
    bq, aq = float(bid_qty or 0.0), float(ask_qty or 0.0)
    if bq + aq <= 0:
        return (bid + ask) / 2.0
    return bid + (ask - bid) * bq / (aq + bq)


def quote_price(bid: float, ask: float, bid_qty: float | None,
                ask_qty: float | None) -> float | None:
    """호가로 정하는 주식 "현재가"(사용자 2026-10-28 유의사항 — DESIGN-ls-xing §9):
    ① 매도·매수 1호가가 둘 다 없으면 현재가 없음(None) ② 한쪽만 있으면 있는 쪽 1호가
    ③ 둘 다 있으면 가중값(weighted_quote_price)을 **1원 반올림**. 순수."""
    has_bid, has_ask = bid > 0, ask > 0
    if not has_bid and not has_ask:
        return None
    if has_bid != has_ask:
        return bid if has_bid else ask
    w = weighted_quote_price(bid, ask, bid_qty, ask_qty)
    return None if w is None else float(round(w))


def carry_theory(base_price: float, days: int, annual_rate: float) -> float:
    """비용캐리 이론가 = 가격 × (1 + 연이자율 × 잔존일/365)."""
    return base_price * (1.0 + annual_rate * days / 365.0)


def fx_theory_base_and_carry(
    last: float | None, quote: tuple[float, float] | None, days: int, annual_rate: float,
) -> float | None:
    """환율이론가(판정용) = 원달러선물 최근월물 (현재가 + 매수1호가 + 매도1호가)/3 × (1 + 연이자율 ×
    잔존일/365) — 사용자 확정 2026-09-15(옛 식은 현재가만). 현재가·호가 중 하나라도 없거나 0이면
    **계산불가(None)**."""
    if not last or quote is None:
        return None
    bid, ask = quote
    if not bid or not ask:
        return None
    return carry_theory((last + bid + ask) / 3.0, days, annual_rate)


def parse_ym(hname: str) -> int | None:
    """선물 hname에서 만기 YYYYMM 추출 (t8401/t8426 = 6자리, 지수 t9943 = YYMM 4자리)."""
    m6 = re.findall(r"\d{6}", hname or "")
    if m6:
        return int(m6[-1])
    m4 = re.findall(r"\d{4}", hname or "")
    if m4:
        return (2000 + int(m4[-1][:2])) * 100 + int(m4[-1][2:])
    return None


def select_usd_futures_months(
    rows: list[dict[str, object]], now: datetime, count: int = 2
) -> list[tuple[str, int]]:
    """t8426(상품선물 마스터) 행에서 미국달러선물 **가까운 월물부터 count개** [(shcode, yyyymm)].

    "미국달러 ... F ..." 월물만(스프레드 SP 제외), 롤오버 지난 월물 제외, 만기 오름차순.
    최근월물=[0], 차근월물=[1] (환헤지 월물 선택용 — DESIGN §9.1).
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
    out: list[tuple[str, int]] = []
    for ym, shcode in sorted(candidates):
        if not is_rolled(ym, "USD", now):
            out.append((shcode, ym))
            if len(out) >= count:
                break
    return out


def pick_fx_futures(
    months: list[tuple[str, int]], code: str
) -> tuple[str, int] | None:
    """환율 계산에 쓸 원달러선물 월물 — 공통설정에서 고른 코드(DESIGN-settings §3, 2026-09-28).

    고른 코드가 목록에 있으면 그 월물, 없거나(만기로 빠짐) 빈 문자열이면 최근월물([0]).
    목록이 비면 None. 순수.
    """
    for month in months:
        if month[0] == code:
            return month
    return months[0] if months else None


def select_usd_futures(
    rows: list[dict[str, object]], now: datetime
) -> tuple[str, int] | None:
    """미국달러선물 **최근월물** (shcode, yyyymm). = ``select_usd_futures_months(..)[0]``."""
    months = select_usd_futures_months(rows, now, count=1)
    return months[0] if months else None
