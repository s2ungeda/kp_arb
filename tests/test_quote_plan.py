"""LS 실시간 구독 계획(DESIGN-ls-xing §9) — 설정 → (TR, 키) 목록, 현재 등록과의 차이. 순수 로직."""
from kp_arb.domain.enums import Underlying
from kp_arb.quote_plan import QuotePlanInput, plan, plan_diff

S, H, M = Underlying.SAMSUNG, Underlying.SK_HYNIX, Underlying.HYUNDAI
CODES = {(S, "near"): "A116B000", (S, "next"): "A116C000",
         (H, "near"): "A506B000", (H, "next"): "A506C000",
         (M, "near"): "A166B000", (M, "next"): "A166C000"}


def test_plan_subscribes_only_checked_symbols_with_four_stock_trs() -> None:
    # 삼성·하이닉스만 체크, 현대차 제외(어느 전략도 안 씀) — 종목당 US3·UYS·호가 1(고른 거래소)·
    # VI_ +
    # 근월물 JH0·YJC
    inp = QuotePlanInput(symbols_on=frozenset({S, H}), stock_market={S: "nxt", H: "krx"},
                         futures_codes=CODES, fx_code="A756B000")
    got = plan(inp)
    assert ("US3", "U005930   ") in got and ("UYS", "U000660   ") in got
    assert ("NH1", "N005930   ") in got and ("H1_", "000660") in got   # 고른 거래소의 호가만
    assert ("H1_", "005930") not in got and ("NH1", "N000660   ") not in got
    assert ("VI_", "005930") in got and ("VI_", "000660") in got
    assert ("JH0", "A116B000") in got and ("YJC", "A506B000") in got  # 근월물만
    assert ("JH0", "A116C000") not in got and ("JH0", "A166B000") not in got
    assert ("FC9", "A756B000") in got and ("YF9", "A756B000") in got
    assert not any(k.endswith("005380") or "005380" in k for _t, k in got)  # 현대차 없음
    assert all(t not in ("S3_", "NS3", "YS3", "UH1", "S2_", "NS2", "US2", "JC0", "DC0")
               for t, _k in got)
    assert len(got) == 2 * 4 + 2 * 2 + 2                             # 14건(통보·CUR·JIF는 밖)


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
    before = plan(QuotePlanInput(symbols_on=frozenset({S, H}), stock_market={S: "krx"},
                                 futures_codes=CODES, fx_code="A756B000"))
    after = plan(QuotePlanInput(symbols_on=frozenset({S}), stock_market={S: "nxt"},
                                futures_codes=CODES, fx_code="A756B000"))
    add, remove = plan_diff(before, after)
    assert add == [("NH1", "N005930   ")]                                 # 거래소 KRX→NXT
    assert ("H1_", "005930") in remove                                    # 옛 거래소 호가 해제
    assert all("000660" in k or "A506" in k or t == "H1_" for t, k in remove)  # 하이닉스 전부 해제
    assert ("FC9", "A756B000") not in remove and ("US3", "U005930   ") not in remove
    assert plan_diff(after, after) == ([], [])
