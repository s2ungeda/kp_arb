"""LS TR별 초당 한도 표(공식 TR 목록 transaction_per_sec) — 우리 한도 지킴이가 이 표를 쓴다."""
from __future__ import annotations

from kp_arb.gateways.ls_rest import LS_PER_SECOND, RateLimiter, RateLimitError


def test_official_per_tr_limits_present() -> None:
    # 실측 2026-09-08: 기본 2회를 전 TR에 적용해 선물 취소(공식 10회)를 우리 쪽에서 막았음.
    assert LS_PER_SECOND["CFOAT00300"] == 10 and LS_PER_SECOND["CFOAT00100"] == 10
    assert LS_PER_SECOND["CSPAT00601"] == 10 and LS_PER_SECOND["CSPAT00801"] == 3
    assert LS_PER_SECOND["CFOBQ10500"] == 1 and LS_PER_SECOND["t1901"] == 1
    # 재대조 2026-09-14(openapi.ls-sec.co.kr ThroughputQuotaRule): t2111은 표에 없어 기본 2로
    # 막히고 있었음(공식 10). t0441·t0434는 2로 적혀 있었으나 공식 1.
    assert LS_PER_SECOND["t2111"] == 10
    assert LS_PER_SECOND["t0441"] == 1 and LS_PER_SECOND["t0434"] == 1


def test_every_tr_the_gateway_uses_is_in_the_table() -> None:
    # 표에 없는 TR은 근거 없는 기본값(2)으로 막힌다 — 게이트웨이가 쓰는 REST TR은 전부 표에.
    from kp_arb.gateways.ls import LSApiGateway as G

    used = {G.SPOT_ORDER_TR, G.SPOT_AMEND_TR, G.SPOT_CANCEL_TR, G.FUTURE_ORDER_TR,
            G.FUTURE_AMEND_TR, G.FUTURE_CANCEL_TR, G.STOCK_DEPOSIT_TR, G.STOCK_POSITIONS_TR,
            G.STOCK_OPEN_ORDERS_TR, G.DERIV_DEPOSIT_TR, G.DERIV_POSITIONS_TR,
            G.DERIV_OPEN_ORDERS_TR, G.FUTURES_MASTER_TR, G.STOCK_PRICE_TR, G.FUTURES_PRICE_TR,
            G.ETF_INFO_TR, G.COMMODITY_MASTER_TR, G.FX_PRICE_TR}
    assert used <= set(LS_PER_SECOND), used - set(LS_PER_SECOND)


def test_limiter_uses_table_per_tr() -> None:
    clock = [1000.0]
    lim = RateLimiter(now=lambda: clock[0], per_tr_per_second=LS_PER_SECOND)
    for _ in range(10):  # 선물 취소 초당 10회까지 허용
        lim.check("CFOAT00300")
    try:
        lim.check("CFOAT00300")
    except RateLimitError:
        pass
    else:  # pragma: no cover - 실패 경로
        raise AssertionError("11번째 취소는 막혀야 함")
    for _ in range(10):  # t2111(원달러선물 현재가) 공식 10 — 시동 초기값(월물 2)+예비 조회 한 초 OK
        lim.check("t2111")
    try:
        lim.check("t2111")
    except RateLimitError:
        pass
    else:  # pragma: no cover
        raise AssertionError("11번째 t2111은 막혀야 함")
    lim.check("t0434")  # 선물 미체결 공식 1
    try:
        lim.check("t0434")
    except RateLimitError:
        pass
    else:  # pragma: no cover
        raise AssertionError("2번째 t0434는 막혀야 함")
    lim.check("zzz999")  # 표에 없는 TR은 기본값(2)
    lim.check("zzz999")
    try:
        lim.check("zzz999")
    except RateLimitError:
        pass
    else:  # pragma: no cover
        raise AssertionError("표에 없는 TR은 기본 2회")


def test_daily_cap_warns_but_never_blocks(caplog) -> None:  # type: ignore[no-untyped-def]
    # 운영 실측 2026-09-14 13:11: 자체 일 한도(5,000)가 선물 계좌의 취소까지 막아 걸린 선주문을 못
    # 지웠다. 일 호출수는 경고만 남기고 절대 막지 않는다(진짜 한도는 LS가 rsp_cd로 거부).
    import logging

    clock = [1000.0]
    lim = RateLimiter(now=lambda: clock[0], daily_cap=5, per_tr_per_second={"CFOAT00300": 100})
    with caplog.at_level(logging.WARNING, logger="kp_arb.gateways.ls_rest"):
        for _ in range(8):
            clock[0] += 1.0
            lim.check("CFOAT00300")  # 5를 넘겨도 예외 없음
    warns = [r for r in caplog.records if "일 호출수" in r.getMessage()]
    assert len(warns) == 1 and "참고 한도 5 초과(차단 안 함" in warns[0].getMessage()
