"""LS 실시간 구독 계획(DESIGN-ls-xing §9) — 설정 → (TR, 키) 목록, 현재 등록과의 차이. 순수 로직."""
from kp_arb.domain.enums import Underlying
from kp_arb.quote_plan import QuotePlanInput, plan, plan_diff

S, H, M = Underlying.SAMSUNG, Underlying.SK_HYNIX, Underlying.HYUNDAI
CODES = {(S, "near"): "A116B000", (S, "next"): "A116C000",
         (H, "near"): "A506B000", (H, "next"): "A506C000",
         (M, "near"): "A166B000", (M, "next"): "A166C000"}


def test_plan_subscribes_only_checked_symbols_with_three_stock_trs() -> None:
    # 삼성·하이닉스만 체크, 현대차 제외(어느 전략도 안 씀) — 종목당 UYS·UH1(통합 호가 하나 — 사용자
    # 2026-10-27 오후, 거래소별 1호가가 한 건에서 나온다)·VI_ + 근월물 JH0·YJC. 주식 체결 US3와
    # 거래소별 호가 H1_/NH1은 안 받는다.
    inp = QuotePlanInput(symbols_on=frozenset({S, H}), futures_codes=CODES, fx_code="A756B000")
    got = plan(inp)
    assert ("UYS", "U005930   ") in got and ("UYS", "U000660   ") in got
    assert ("UH1", "U005930   ") in got and ("UH1", "U000660   ") in got
    assert ("VI_", "005930") in got and ("VI_", "000660") in got
    assert ("JH0", "A116B000") in got and ("YJC", "A506B000") in got  # 근월물만
    assert ("JH0", "A116C000") not in got and ("JH0", "A166B000") not in got
    assert ("FC9", "A756B000") in got and ("YF9", "A756B000") in got
    assert not any(k.endswith("005380") or "005380" in k for _t, k in got)  # 현대차 없음
    assert all(t not in ("US3", "S3_", "NS3", "YS3", "H1_", "NH1", "S2_", "NS2", "US2", "JC0",
                         "DC0") for t, _k in got)
    assert len(got) == 2 * 3 + 2 * 2 + 2                             # 12건(통보·CUR·JIF는 밖)


def test_plan_follows_futures_month_choice_and_symbol_toggle() -> None:
    # 차근을 고른 종목은 차근 JH0·YJC도(근월물과 같이 쓰는 세트가 있을 수 있어 합집합), 체크 끄면
    # 전부 빠짐
    inp = QuotePlanInput(symbols_on=frozenset({H}), futures_codes=CODES,
                         futures_months={H: frozenset({"near", "next"})}, fx_code="A756B000")
    got = plan(inp)
    assert ("JH0", "A506B000") in got and ("JH0", "A506C000") in got
    assert ("YJC", "A506C000") in got
    assert plan(QuotePlanInput(symbols_on=frozenset(), futures_codes=CODES,
                               fx_code="A756B000")) == []  # 종목이 없으면 환도 안 받음


def test_plan_diff_adds_and_removes_only_changed() -> None:
    before = plan(QuotePlanInput(symbols_on=frozenset({S, H}), futures_codes=CODES,
                                 fx_code="A756B000"))
    after = plan(QuotePlanInput(symbols_on=frozenset({S}), futures_codes=CODES,
                                fx_code="A756C000"))
    add, remove = plan_diff(before, after)
    assert add == [("FC9", "A756C000"), ("YF9", "A756C000")]          # 원달러 월물 교체
    assert ("UH1", "U005930   ") not in remove                        # 삼성 호가는 그대로
    assert all("000660" in k or "A506" in k or k == "A756B000" for _t, k in remove)
    assert ("FC9", "A756B000") in remove
    assert plan_diff(after, after) == ([], [])
    # 옛 등록(H1_/NH1/US3)이 남아 있으면 해제 쪽으로 나온다(계획엔 더 없음)
    stale = [*before, ("H1_", "005930"), ("NH1", "N005930   ")]
    assert set(plan_diff(stale, before)[1]) == {("H1_", "005930"), ("NH1", "N005930   ")}
