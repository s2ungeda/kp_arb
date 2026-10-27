"""LS 실시간 구독 계획(DESIGN-ls-xing.md §9, 사용자 2026-10-23 "전략이 쓰는 시세만") — 순수 로직.

설정(구독 종목 체크·선물 월물·원달러 월물)에서 등록해야 할 (TR, 키) 목록을 만들고, 지금 등록된
것과의 차이(추가·해제)를 낸다. 종목·월물에 따라 바뀌는 것만 다룬다 — 종목과 무관한 CUR·JIF·계좌
통보(SC*/O01/C01/H01)는 게이트웨이가 시동 때 고정으로 단다.

종목당 3건: UYS 통합 예상체결 · UH1 통합 호가 · VI_. 주식 호가는 통합(UH1) 하나(사용자 2026-10-27
오후 "통합호가 하나로 다 해결") — 단계마다 KRX·NXT·통합 잔량이 따로 와 거래소별 1호가가 다 나오므로
체결쏴 주식의 거래소 선택·동시호가 예약과 무관하게 늘 1건(읽는 칸은 xing_com.read_unified_quote).
주식 체결(US3)은 안 받는다(2026-10-27 — 현재가는 1호가 가중값). NXT 체결 NS3는 그것을 판정에
쓰는 화면(NXT 개장)이 실행 중인 종목만. 주식선물은 쓰는 월물의 JH0·YJC. 원달러선물은 공통설정에서
고른 월물의 FC9·YF9.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .domain.enums import Underlying

Sub = tuple[str, str]  # (tr_cd, tr_key)

NXT_TRADE_TR = "NS3"        # NXT 체결 — NXT 개장 매매가 실행 중인 종목만(사용자 2026-10-27)
STOCK_EXPECTED_TR = "UYS"   # KOSPI+KOSDAQ 예상체결(통합)
# 통합 호가 — KRX·NXT·통합 1호가가 한 건에서 나온다(2026-10-27 오후). 10-16 운영 실측 분당 건수는
# H1_ 하나와 비슷(H1_+NH1의 절반쯤).
STOCK_QUOTE_TR = "UH1"
STOCK_VI_TR = "VI_"
FUTURES_QUOTE_TR = "JH0"    # 주식선물 호가(1호가 넷만 읽는다 — xing_com.REAL_FIELD_LIMIT)
FUTURES_EXPECTED_TR = "YJC"
FX_TRADE_TR = "FC9"         # 원달러선물 체결(1호가 포함 — 선물역산 환율), 공통설정이 고른 월물만
FX_EXPECTED_TR = "YF9"      # 원달러선물 예상체결(동시호가 주문)


def unified_key(code: str) -> str:
    """통합 TR(UH1/UYS) 키 — "U"+6자리코드+공백3(RTD 실측 10자)."""
    return f"U{code}   "


def nxt_key(code: str) -> str:
    """NXT 전용 TR(NS3) 키 — "N"+6자리코드+공백3."""
    return f"N{code}   "


@dataclass(frozen=True)
class QuotePlanInput:
    """구독 계획 재료 — 전부 설정·종목 메타에서 온다(시세 아님)."""

    symbols_on: frozenset[Underlying] = frozenset()           # 공통설정 quote_symbols 체크
    futures_codes: Mapping[tuple[Underlying, str], str] = field(default_factory=dict)
    # (종목, "near"|"next") → 주식선물 코드
    futures_months: Mapping[Underlying, frozenset[str]] = field(default_factory=dict)
    # 종목이 쓰는 월물 집합(주식선물·HL선 종목 상태의 future_month 합집합; 비면 근월물)
    fx_code: str | None = None                   # 원달러선물 코드(공통설정이 고른 월물)
    # NXT 체결(NS3)을 판정에 쓰는 화면이 실행 중인 종목 — NXT 개장(진입·청산 실행)
    nxt_trade_symbols: frozenset[Underlying] = frozenset()


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
        add(STOCK_EXPECTED_TR, unified_key(code))
        if u in inp.nxt_trade_symbols:
            add(NXT_TRADE_TR, nxt_key(code))
        add(STOCK_QUOTE_TR, unified_key(code))
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
