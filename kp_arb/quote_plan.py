"""LS 실시간 구독 계획(DESIGN-ls-xing.md §9, 사용자 2026-10-23 "전략이 쓰는 시세만") — 순수 로직.

설정(구독 종목 체크·체결쏴 주식 거래소·선물 월물·원달러 월물)에서 등록해야 할 (TR, 키) 목록을
만들고, 지금 등록된 것과의 차이(추가·해제)를 낸다. 종목·월물에 따라 바뀌는 것만 다룬다 — 종목과
무관한 CUR·JIF·계좌 통보(SC*/O01/C01/H01)는 게이트웨이가 시동 때 고정으로 단다.

종목당 4건: US3 통합 체결(거래소명으로 KRX·NXT·통틀어 마지막을 가름) · UYS 통합 예상체결 · 호가
(체결쏴 주식이 고른 거래소 하나 — KRX H1_ | NXT NH1, 읽는 칸은 1호가 여섯 —
xing_com.REAL_FIELD_LIMIT) · VI_. 주식선물은 쓰는 월물의 JH0·YJC.
원달러선물은 공통설정에서 고른 월물의 FC9·YF9.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .domain.enums import Underlying

Sub = tuple[str, str]  # (tr_cd, tr_key)

STOCK_TRADE_TR = "US3"      # KRX+NXT 통합 체결
STOCK_EXPECTED_TR = "UYS"   # KOSPI+KOSDAQ 예상체결(통합)
# 호가 — 고른 거래소 하나(사용자 2026-10-23 "3": 우선호가 S2_/NS2는 1호가 가격이 바뀔 때만 와서
# (분당 1~12건) 등록 직후 1~2분 값이 없었음 → 잔량 변화마다 오는 H1_/NH1로 되돌리되 읽는 칸은
# 1호가 여섯만)
STOCK_BEST_TR = {"krx": "H1_", "nxt": "NH1"}
STOCK_VI_TR = "VI_"
FUTURES_QUOTE_TR = "JH0"    # 주식선물 호가(1호가 넷만 읽는다 — xing_com.REAL_FIELD_LIMIT)
FUTURES_EXPECTED_TR = "YJC"
FX_TRADE_TR = "FC9"         # 원달러선물 체결(1호가 포함 — 선물역산 환율), 공통설정이 고른 월물만
FX_EXPECTED_TR = "YF9"      # 원달러선물 예상체결(동시호가 주문)


def unified_key(code: str) -> str:
    """통합 TR(US3/UYS) 키 — "U"+6자리코드+공백3(RTD 실측 10자)."""
    return f"U{code}   "


def nxt_key(code: str) -> str:
    """NXT 전용 TR(NH1) 키 — "N"+6자리코드+공백3."""
    return f"N{code}   "


@dataclass(frozen=True)
class QuotePlanInput:
    """구독 계획 재료 — 전부 설정·종목 메타에서 온다(시세 아님)."""

    symbols_on: frozenset[Underlying] = frozenset()           # 공통설정 quote_symbols 체크
    stock_market: Mapping[Underlying, str] = field(default_factory=dict)  # 체결쏴 주식 거래소
    futures_codes: Mapping[tuple[Underlying, str], str] = field(default_factory=dict)
    # (종목, "near"|"next") → 주식선물 코드
    futures_months: Mapping[Underlying, frozenset[str]] = field(default_factory=dict)
    # 종목이 쓰는 월물 집합(주식선물·HL선 종목 상태의 future_month 합집합; 비면 근월물)
    fx_code: str | None = None                   # 원달러선물 코드(공통설정이 고른 월물)


def plan(inp: QuotePlanInput) -> list[Sub]:
    """등록해야 할 (TR, 키) — 종목 순, 중복 없음."""
    out: list[Sub] = []

    def add(tr: str, key: str) -> None:
        if (tr, key) not in out:
            out.append((tr, key))

    for u in Underlying:
        if u not in inp.symbols_on:
            continue
        code = u.krx_code
        add(STOCK_TRADE_TR, unified_key(code))
        add(STOCK_EXPECTED_TR, unified_key(code))
        market = inp.stock_market.get(u, "krx")
        best_tr = STOCK_BEST_TR.get(market, STOCK_BEST_TR["krx"])
        add(best_tr, nxt_key(code) if market == "nxt" else code)
        add(STOCK_VI_TR, code)
        months = inp.futures_months.get(u) or frozenset({"near"})
        for month in ("near", "next"):
            if month not in months:
                continue
            fcode = inp.futures_codes.get((u, month))
            if fcode:
                add(FUTURES_QUOTE_TR, fcode)
                add(FUTURES_EXPECTED_TR, fcode)
    if inp.fx_code and inp.symbols_on:
        add(FX_TRADE_TR, inp.fx_code)
        add(FX_EXPECTED_TR, inp.fx_code)
    return out


def plan_diff(current: Iterable[Sub], wanted: Iterable[Sub]) -> tuple[list[Sub], list[Sub]]:
    """(추가할 것, 해제할 것) — 순서는 wanted/current 순."""
    cur, want = list(current), list(wanted)
    cur_set, want_set = set(cur), set(want)
    return [s for s in want if s not in cur_set], [s for s in cur if s not in want_set]
