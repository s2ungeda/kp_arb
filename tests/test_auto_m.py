"""자동M 순수 상태기계 — DESIGN-auto-m-exec.md 정방향 진입·청산 (①단계)."""
from datetime import datetime

import pytest

from kp_arb.auto_m import (
    Accum,
    AutoMSet,
    AutoMSettings,
    LegStatus,
    Signals,
    evaluate,
    fill_diff,
    halt_if_unhedged,
    limit_price,
    on_post_fill,
    on_post_reject,
    on_pre_ack,
    on_pre_cancelled,
    on_pre_fill,
    on_pre_reject,
    order_qty,
    parse_hms,
    pre_order_price,
    rel_quote,
    release_halt,
    set_running,
)
from kp_arb.domain.enums import Side, Underlying
from kp_arb.strategy_core import Block

U = Underlying.SK_HYNIX
SETTINGS = AutoMSettings(windows=(("09:00:00", "15:20:00"),), pre_delay_ms=1000)
NOW = datetime(2026, 9, 4, 10, 0, 0)


def _sig(mono: float = 100.0, **kw: object) -> Signals:
    # exec §4 실예: 이론가 200,000 · HL est 괴리 1.0% · 매도1호가 201,500
    base = dict(now=NOW, mono=mono, sf_spread_entry=0.01, s_spread_entry=0.01,
                sf_spread_exit=-0.01, hl_disp_bid=0.01, hl_disp_ask=0.01,
                sf_theory=200_000.0, stock_last=199_000.0,
                sf_asks=[(201_500.0, 5), (204_500.0, 3)], sf_bids=[(198_500.0, 4)])
    base.update(kw)
    return Signals(**base)  # type: ignore[arg-type]


def _set(**kw: object) -> AutoMSet:
    s = AutoMSet(target_qty=100, per_qty=10, switch_delay_s=30, en_sf=0.005, en_s=0.005,
                 ex_sf=-0.001)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_pure_pieces_match_exec_example() -> None:
    # exec §4 실예: 역산가 201,000(틱 3,000 내림), 한계 (201,500−3,000)×0.996 ≈ 197,706 → 발주
    assert pre_order_price(Block.ENTRY, 200_000, 0.01, 0.005, 3000) == 201_000
    assert round(limit_price(Side.BUY, 201_500, 3000, 0.004)) == 197_706
    assert pre_order_price(Block.EXIT, 200_000, 0.01, 0.005, 3000) == 201_000  # 매도는 올림
    assert pre_order_price(Block.EXIT, 200_000, 0.011, 0.005, 3000) == 204_000
    assert rel_quote([(100.0, 1), (102.0, 1), (105.0, 1)], 2) == 102 and rel_quote([], 1) is None
    assert order_qty(Block.ENTRY, 10, 100, 95) == 5 and order_qty(Block.ENTRY, 10, 100, 100) == 0
    assert order_qty(Block.EXIT, 10, 100, 3) == 3
    assert fill_diff(4, -40) == 0 and fill_diff(4, -30) == 10
    assert parse_hms("08:30:10").second == 10 and parse_hms("09:00").hour == 9
    with pytest.raises(ValueError):
        parse_hms("9")


def test_entry_round_trip_partial_fill_and_rt() -> None:
    # 감시 → 선주문 10계약 @201,000 → 4계약 체결 → HL 40 즉시 → 후주문 체결 → RT 4 (exec 실예)
    s = _set()
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    assert [(a.kind, a.side, a.qty, a.price) for a in acts] == [
        ("place_pre", Side.BUY, 10, 201_000.0)]
    assert s.entry.status is LegStatus.PRE_RESTING
    on_pre_ack(s, Block.ENTRY, "2801")
    assert evaluate(s, Block.ENTRY, _sig(mono=101), SETTINGS, U) == []  # 같은 역산가 → 유지

    acts = on_pre_fill(s, Block.ENTRY, 4, 201_000.0, mono=102)
    assert [(a.kind, a.side, a.qty) for a in acts] == [("place_post", Side.SELL, 40)]
    assert s.entry.status is LegStatus.PRE_PARTIAL and s.entry.post_pending == 40
    on_post_fill(s, Block.ENTRY, 40, 1184.0, 1356.1, mono=103, settings=SETTINGS)
    assert s.rt == 4 and s.entry.post_pending == 0
    assert s.entry.status is LegStatus.PRE_PARTIAL  # 남은 6계약은 호가에 그대로
    assert s.last_entry_fill_mono == 103

    # 남은 수량 전부 체결 → 후주문 60 → 체결 → 딜레이 → 감시 복귀
    on_pre_fill(s, Block.ENTRY, 6, 201_000.0, mono=104)
    assert s.entry.status is LegStatus.POST_PENDING
    on_post_fill(s, Block.ENTRY, 60, 1184.0, 1356.1, mono=105, settings=SETTINGS)
    assert s.rt == 10 and s.entry.status is LegStatus.SETTLE_DELAY
    assert evaluate(s, Block.ENTRY, _sig(mono=105.5), SETTINGS, U) == []  # 1초 딜레이 중
    acts = evaluate(s, Block.ENTRY, _sig(mono=106.1), SETTINGS, U)
    assert acts and acts[0].kind == "place_pre" and acts[0].qty == 10  # Min(10, 100−10)


def test_replace_rule_cancel_then_wait_post_then_delay_then_fresh_qty() -> None:
    # 부분체결 4/10 상태에서 역산가 바뀜 → 남은 6 취소 → 병행 후주문 체결 확인(RT 4) → 딜레이 →
    # 신규 Min(10, 100−4) = 10 (exec §6 공통 규칙, 2026-09-03)
    s = _set()
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "2801")
    on_pre_fill(s, Block.ENTRY, 4, 201_000.0, mono=101)  # 후주문 40 대기 중
    # HL est 괴리 1%→2%면 역산가 203,000이지만 틱 3,000 내림으로 201,000 그대로 → 재발주 없음
    assert evaluate(s, Block.ENTRY, _sig(mono=101.5, hl_disp_bid=0.02), SETTINGS, U) == []
    # 3%면 205,000 → 내림 204,000 ≠ 201,000 → 재발주 규칙 발동
    acts = evaluate(s, Block.ENTRY, _sig(mono=102, hl_disp_bid=0.03), SETTINGS, U)
    assert [(a.kind, a.order_id) for a in acts] == [("cancel_pre", "2801")]
    assert evaluate(s, Block.ENTRY, _sig(mono=102.5, hl_disp_bid=0.03), SETTINGS, U) == []
    on_pre_cancelled(s, Block.ENTRY, mono=103, settings=SETTINGS)
    assert s.entry.await_post_then_delay and s.entry.status is LegStatus.PRE_PARTIAL
    assert evaluate(s, Block.ENTRY, _sig(mono=103.5, hl_disp_bid=0.03), SETTINGS, U) == []
    on_post_fill(s, Block.ENTRY, 40, 1184.0, 1356.1, mono=104, settings=SETTINGS)
    assert s.rt == 4 and s.entry.status is LegStatus.SETTLE_DELAY
    acts = evaluate(s, Block.ENTRY, _sig(mono=105.1, hl_disp_bid=0.03), SETTINGS, U)
    assert acts[0].kind == "place_pre" and acts[0].qty == 10 and acts[0].price == 204_000.0


def test_gates_cancel_or_hold_resting_order() -> None:
    s = _set()
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "1")
    # G5 미달 → 취소
    acts = evaluate(s, Block.ENTRY, _sig(mono=101, s_spread_entry=0.001), SETTINGS, U)
    assert [a.kind for a in acts] == ["cancel_pre"]
    on_pre_cancelled(s, Block.ENTRY, mono=102, settings=SETTINGS)
    assert s.entry.status is LegStatus.ARMED and s.entry.pre_order_id is None
    # G6 한계 미달(역산가 197,000 < 197,706) → 안 냄
    assert evaluate(s, Block.ENTRY, _sig(mono=103, hl_disp_bid=-0.01), SETTINGS, U) == []
    # G2 시간 밖 → 안 냄
    off = _sig(mono=104, now=datetime(2026, 9, 4, 16, 0))
    assert evaluate(s, Block.ENTRY, off, SETTINGS, U) == []
    # 시장 정지 → 걸어둔 것 취소, 재개 딜레이 동안 안 냄
    evaluate(s, Block.ENTRY, _sig(mono=105), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "2")
    assert [a.kind for a in evaluate(s, Block.ENTRY, _sig(mono=106, market_halted=True),
                                     SETTINGS, U)] == ["cancel_pre"]
    on_pre_cancelled(s, Block.ENTRY, mono=107, settings=SETTINGS)
    assert evaluate(s, Block.ENTRY, _sig(mono=108, resumed_mono=107.0), SETTINGS, U) == []
    assert evaluate(s, Block.ENTRY, _sig(mono=120, resumed_mono=107.0), SETTINGS, U)[0].kind \
        == "place_pre"


def test_block_reason_records_gate_and_basis() -> None:
    # 판정 근거 한 줄 — 어느 게이트에서 막혔는지·통과 시 역산가 계산 근거(로그는 바뀔 때만).
    s = _set()
    assert evaluate(s, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert s.entry.block_reason.startswith("G1")
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(now=datetime(2026, 9, 4, 16, 0)), SETTINGS, U)
    assert s.entry.block_reason.startswith("G2")
    evaluate(s, Block.ENTRY, _sig(s_spread_entry=0.001), SETTINGS, U)
    assert "G5" in s.entry.block_reason and "0.100%" in s.entry.block_reason
    evaluate(s, Block.ENTRY, _sig(hl_disp_bid=-0.01), SETTINGS, U)
    assert "G6 한계 밖" in s.entry.block_reason
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    assert s.entry.block_reason.startswith("통과") and "201,000" in s.entry.block_reason
    on_pre_ack(s, Block.ENTRY, "1")
    evaluate(s, Block.ENTRY, _sig(mono=101), SETTINGS, U)
    assert s.entry.block_reason.startswith("유지")


def test_switch_delay_and_exit_leg() -> None:
    # 청산: SF 매도(올림) — 직전 진입 체결 뒤 전환딜레이 30초 동안 안 냄, 수량 Min(1회, RT)
    s = _set(rt=3, last_entry_fill_mono=100.0)
    set_running(s, Block.EXIT, True)
    assert evaluate(s, Block.EXIT, _sig(mono=110), SETTINGS, U) == []
    # 매도 한계 = (상대매수1호가 + 1틱)×(1+범위): 매수1호가 198,500이면 202,306 < 204,000 → 안 냄
    assert evaluate(s, Block.EXIT, _sig(mono=131), SETTINGS, U) == []
    acts = evaluate(s, Block.EXIT, _sig(mono=132, sf_bids=[(203_000.0, 4)]), SETTINGS, U)
    assert [(a.kind, a.side, a.qty, a.price) for a in acts] == [
        ("place_pre", Side.SELL, 3, 204_000.0)]  # 200,000×(1+0.01+0.001)=202,200 → 올림 204,000
    on_pre_ack(s, Block.EXIT, "9")
    on_pre_fill(s, Block.EXIT, 3, 204_000.0, mono=132)
    on_post_fill(s, Block.EXIT, 30, 1190.0, 1357.0, mono=133, settings=SETTINGS)
    assert s.rt == 0 and s.last_exit_fill_mono == 133
    # 진입은 이제 직전 청산 뒤 30초 전환대기
    set_running(s, Block.ENTRY, True)
    assert evaluate(s, Block.ENTRY, _sig(mono=140), SETTINGS, U) == []


def test_halts_and_running_off() -> None:
    s = _set()
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "1")
    on_pre_fill(s, Block.ENTRY, 10, 201_000.0, mono=101)
    acts = on_post_reject(s, Block.ENTRY, "insufficient margin")
    assert s.entry.status is LegStatus.HALTED and [a.kind for a in acts] == ["halt", "notify"]
    assert evaluate(s, Block.ENTRY, _sig(mono=102), SETTINGS, U) == []  # 중지는 사람이 풀어야
    release_halt(s, Block.ENTRY)
    assert s.entry.status is LegStatus.IDLE and not s.entry.running

    # 실행 끔 → 미체결 취소, 포지션 유지 / 선주문 거부 → 딜레이 뒤 재시도
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(mono=103), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "2")
    assert [a.kind for a in set_running(s, Block.ENTRY, False)] == ["cancel_pre"]
    on_pre_cancelled(s, Block.ENTRY, mono=104, settings=SETTINGS)
    assert s.entry.status is LegStatus.IDLE
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(mono=105), SETTINGS, U)
    on_pre_reject(s, Block.ENTRY, mono=106, settings=SETTINGS)
    assert s.entry.status is LegStatus.SETTLE_DELAY
    # 체결차 감지(후주문 대기 없음) → 중지
    s2 = _set()
    set_running(s2, Block.ENTRY, True)
    assert [a.kind for a in halt_if_unhedged(s2, Block.ENTRY, 10)] == ["halt", "notify"]
    assert halt_if_unhedged(_set(), Block.ENTRY, 0) == []


def test_accum_sprd_matches_excel_i25() -> None:
    # 엑셀 메인 I25: (환×HL − S현재가)/S현재가 − (SF − SF이론가)/SF이론가
    acc = Accum()
    acc.hl_qty, acc.hl_px_sum, acc.fx_sum = 40, 1083.5 * 40, 1413.0 * 40
    acc.sf_qty, acc.sf_px_sum = 4, 1_529_000 * 4
    stock, theory = 1_520_000.0, 1_525_000.0
    want = (1413.0 * 1083.5 - stock) / stock - (1_529_000 - theory) / theory
    assert acc.sprd(stock, theory) == pytest.approx(want)
    assert Accum().sprd(stock, theory) is None
    acc.clear()
    assert acc.hl_qty == 0 and acc.sf_qty == 0
