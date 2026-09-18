"""자동M 순수 상태변화 — DESIGN-auto-m-exec.md 정방향 진입·청산 (①단계)."""
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
                sf_asks=[(201_500.0, 5), (204_500.0, 3)], sf_bids=[(198_500.0, 4)],
                fx=1349.6, hl_bid1=191.9, hl_ask1=191.95, hl_est_bid=191.88, hl_est_ask=191.97)
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
    assert pre_order_price(Side.BUY, 200_000, 0.01, 0.005, 3000) == 201_000
    assert round(limit_price(Side.BUY, 201_500, 3000, 0.004)) == 197_706
    assert pre_order_price(Side.SELL, 200_000, 0.01, 0.005, 3000) == 201_000  # 매도는 올림
    assert pre_order_price(Side.SELL, 200_000, 0.011, 0.005, 3000) == 204_000
    assert rel_quote([(100.0, 1), (102.0, 1), (105.0, 1)], 2) == 102 and rel_quote([], 1) is None
    assert order_qty(Block.ENTRY, 10, 100, 95) == 5 and order_qty(Block.ENTRY, 10, 100, 100) == 0
    assert order_qty(Block.EXIT, 10, 100, 3) == 3
    # 역방향(§7A·§7B): RT는 0 또는 음수, 진입 Min(1회, 목표−(RT×−1)) / 청산 Min(1회, RT×−1)
    assert order_qty(Block.ENTRY, 10, 100, -95, reverse=True) == 5
    assert order_qty(Block.ENTRY, 10, 100, -100, reverse=True) == 0
    assert order_qty(Block.EXIT, 10, 100, -3, reverse=True) == 3
    assert order_qty(Block.EXIT, 10, 100, 0, reverse=True) == 0
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


def test_signal_gate_uses_only_s_for_entry_and_nothing_for_exit() -> None:
    # 정정 2026-09-07(exec §11.3): 진입은 S괴리만 비교(SF괴리 미달이어도 통과), 청산은 비교 없음.
    s = _set()
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(sf_spread_entry=-0.05), SETTINGS, U)  # SF 크게 미달
    assert [a.kind for a in acts] == ["place_pre"]  # 그래도 발주 — SF 기준값은 역산가에만
    s2 = _set()
    s2.rt = 10  # 청산할 RT가 있어야 G4 통과
    set_running(s2, Block.EXIT, True)
    # 옛 규칙이면 SF괴리 0.05 > 청산 −0.001로 미달. hl_disp_ask −1.2% → 역산가 197,800 → 주문단위
    # 3,000 올림 198,000 ≤ 매도 한계 (198,500 + 500) × 1.004 = 199,796 (G6 통과)
    acts2 = evaluate(s2, Block.EXIT, _sig(sf_spread_exit=0.05, hl_disp_ask=-0.012), SETTINGS, U)
    assert [a.kind for a in acts2] == ["place_pre"]  # 청산은 조건 비교 없음
    s3 = _set()
    s3.ex_sf = None
    s3.rt = 10
    set_running(s3, Block.EXIT, True)
    assert evaluate(s3, Block.EXIT, _sig(), SETTINGS, U) == []  # 기준값 없으면 역산 불가 → 안 냄


def test_three_consecutive_pre_rejects_halt_set_with_alarm() -> None:
    # 결정 29(사용자 확정 2026-09-11) + 정정 2026-09-17: 선주문이 연속 3회 거부되면 원인이 남아
    # 있는 것(증거금 부족 등) → 세트 진입·청산 둘 다 **중지(검정)** + 알람(체결차 중지와 같은
    # 경로, 사람이 해제). 접수 뒤 체결이나 취소가 한 번이라도 있으면 연속은 끊긴다.
    from kp_arb.auto_m import PRE_REJECT_LIMIT

    s = _set()
    set_running(s, Block.ENTRY, True)
    set_running(s, Block.EXIT, True)
    s.rt = 10
    for i in range(1, PRE_REJECT_LIMIT):
        evaluate(s, Block.ENTRY, _sig(mono=100.0 + i), SETTINGS, U)
        acts = on_pre_reject(s, Block.ENTRY, mono=100.5 + i, settings=SETTINGS,
                             reason="증거금 부족")
        assert [a.kind for a in acts] == ["notify"] and s.entry.running
        assert f"({i}/{PRE_REJECT_LIMIT})" in acts[0].reason and "증거금 부족" in acts[0].reason
        assert s.entry.status is LegStatus.SETTLE_DELAY
    # 그 사이 청산 선주문이 걸려 있음 → 세트가 꺼질 때 같이 취소
    evaluate(s, Block.EXIT, _sig(mono=105.0), SETTINGS, U)
    on_pre_ack(s, Block.EXIT, "X1")
    evaluate(s, Block.ENTRY, _sig(mono=106.0), SETTINGS, U)
    acts = on_pre_reject(s, Block.ENTRY, mono=106.5, settings=SETTINGS, reason="증거금 부족")
    kinds = [a.kind for a in acts]
    assert "halt" in kinds and "cancel_pre" in kinds           # 중지(알람은 halt 행동) + 청산 취소
    assert not s.entry.running and not s.exit.running          # 세트 양쪽 실행 끔
    assert s.entry.status is LegStatus.HALTED and s.exit.status is LegStatus.HALTED  # 검정
    assert "연속 거부" in s.exit.halt_reason and "연속 3회" in s.entry.halt_reason
    assert s.entry.reject_streak == 0 and s.exit.reject_streak == 0
    assert "연속 3회" in next(a.reason for a in acts if a.kind == "halt")
    # 중지 뒤 또 거부가 와도 중지 유지·딜레이 없음
    assert on_pre_reject(s, Block.ENTRY, mono=107.0, settings=SETTINGS, reason="x") == []
    release_halt(s, Block.ENTRY)
    assert s.entry.status is LegStatus.IDLE and s.exit.status is LegStatus.IDLE
    # 거부 2회 뒤 체결이 있으면 연속이 끊겨 다시 1부터
    s2 = _set()
    set_running(s2, Block.ENTRY, True)
    for i in range(2):
        evaluate(s2, Block.ENTRY, _sig(mono=200.0 + i), SETTINGS, U)
        on_pre_reject(s2, Block.ENTRY, mono=200.5 + i, settings=SETTINGS)
    evaluate(s2, Block.ENTRY, _sig(mono=203.0), SETTINGS, U)
    on_pre_ack(s2, Block.ENTRY, "3")
    on_pre_fill(s2, Block.ENTRY, 10, 201_000.0, mono=204.0)
    assert s2.entry.reject_streak == 0


def test_restore_drops_extra_saved_sets_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    # 세트 수 3·3 → 4·2(2026-09-15) 뒤 첫 재시동: 저장본의 역방향 3번째 세트는 버리되, 거기에
    # RT·장부가 남아 있으면 경고(사람이 포지션 확인). 정방향은 3개만 복원되고 4번째는 기본값.
    import logging

    from kp_arb.auto_m import AutoMBook, _book_from_dict

    # (2026-09-16 세트 수 8·4로 늘림 — 저장본이 더 긴 경우는 역방향 5개로 재현)
    book = AutoMBook()
    raw = {"sets": [{"target_qty": 5}, {"target_qty": 6}, {"target_qty": 7}],
           "rev_sets": [{"target_qty": 1}, {"target_qty": 2}, {"target_qty": 3},
                        {"target_qty": 4},
                        {"target_qty": 9, "rt": -1, "sf_net": -1, "hl_net": 10.0}]}
    with caplog.at_level(logging.WARNING, logger="kp_arb.autom"):
        _book_from_dict(book, raw)
    assert [s.target_qty for s in book.sets] == [5, 6, 7, 0, 0, 0, 0, 0]
    assert [s.target_qty for s in book.rev_sets] == [1, 2, 3, 4]
    assert any("세트 수 축소" in r.getMessage() and "RT -1" in r.getMessage()
               for r in caplog.records)


def test_pre_reject_is_shown_with_reason_until_next_ack() -> None:
    # 사용자 2026-09-15: 선주문이 거부됐는지·사유가 뭔지 화면에서 알 수 없었다 → 마지막 거부를
    # 다리에 들고 있다가 상태줄에("거부(n/3): 사유"), 다음 접수가 오면 지운다. 3회째는
    # "실행 끔" 문구.
    s = _set()
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_reject(s, Block.ENTRY, mono=100.5, settings=SETTINGS, reason="LS 02752 증거금부족")
    assert s.entry.last_reject == "선주문 거부(1/3): LS 02752 증거금부족"
    evaluate(s, Block.ENTRY, _sig(mono=200.0), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "9")
    assert s.entry.last_reject == ""  # 접수됐으면 거부 표시 끝
    s.entry.reject_streak = 2
    on_pre_reject(s, Block.ENTRY, mono=201.0, settings=SETTINGS, reason="LS 02752 증거금부족")
    assert s.entry.last_reject.startswith("선주문 거부 연속 3회 → 세트 중지: LS 02752")
    assert not s.entry.running and not s.exit.running and s.entry.status is LegStatus.HALTED


def test_place_pre_keeps_hl_est_of_post_side_for_fill_comparison() -> None:
    # 사용자 2026-09-14: 선주문 발주 시점 HL est(후주문 방향)를 들고 있다가 후주문 체결가와
    # 비교한다. 정방향 진입 = 후주문 HL 매도 → 매수호가창 est(hl_est_bid). 재발주면 새 값으로.
    s = _set()
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    assert [a.kind for a in acts] == ["place_pre"] and s.entry.pre_est == 191.88
    on_pre_ack(s, Block.ENTRY, "1")
    evaluate(s, Block.ENTRY, _sig(hl_disp_bid=0.03), SETTINGS, U)  # 역산가 변경 → 취소
    on_pre_cancelled(s, Block.ENTRY, mono=101.0, settings=SETTINGS)
    acts = evaluate(s, Block.ENTRY, _sig(mono=102.0, hl_disp_bid=0.03, hl_est_bid=195.5),
                    SETTINGS, U)
    assert [a.kind for a in acts] == ["place_pre"] and s.entry.pre_est == 195.5


def test_fill_before_cancel_confirmation_does_not_freeze_in_delay() -> None:
    # 실측 2026-09-11 오후: 역산가 변경으로 취소를 보냈는데 취소보다 체결이 먼저(LS는 취소를 01433
    # 거부) → 후주문까지 잡혀 판이 끝났는데 '취소 확인 대기' 표시가 남아 딜레이대기('쉼')에서 영영
    # 못 나옴. 선주문이 끝나면(체결·취소·거부) 그 표시도 지워야 한다.
    s = _set()
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    assert [a.kind for a in acts] == ["place_pre"] and s.entry.pre_price == 201_000.0
    on_pre_ack(s, Block.ENTRY, "1")
    acts = evaluate(s, Block.ENTRY, _sig(hl_disp_bid=0.03), SETTINGS, U)  # 역산가 204,000
    assert [a.kind for a in acts] == ["cancel_pre"] and s.entry.replace_pending
    on_pre_fill(s, Block.ENTRY, 10, 201_000.0, mono=101.0)                # 취소 전에 전량 체결
    assert s.entry.status is LegStatus.POST_PENDING
    acts = on_post_fill(s, Block.ENTRY, 100.0, 1184.0, 1356.0, 102.0, SETTINGS)
    assert acts == [] and s.entry.status is LegStatus.SETTLE_DELAY
    assert not s.entry.replace_pending                                    # 판 끝 → 표시 정리
    acts = evaluate(s, Block.ENTRY, _sig(mono=104.0), SETTINGS, U)        # 딜레이 지남
    assert s.entry.block_reason != "후주문/취소 확인 대기"
    assert [a.kind for a in acts] == ["place_pre"]                        # 다음 판 진행


def test_reverse_entry_is_sf_sell_hl_buy_with_flipped_gates() -> None:
    # exec §7A(2026-09-14): 역방향 진입 = SF 매도 → HL 매수. G5는 매도호가창 S괴리 < +HP/-S,
    # G6는 HL 매도호가창 est·주문단위 올림·한계 (상대매수N호가+1틱)(1+범위) 이하. RT는 −쪽으로,
    # SF 순잔고 −, HL 순잔고 +. 화면 RT는 −값 그대로.
    s = AutoMSet(target_qty=100, per_qty=10, switch_delay_s=30, en_sf=0.005, en_s=0.005,
                 ex_sf=-0.001, reverse=True)
    assert s.entry.pre_side is Side.SELL and s.entry.post_side is Side.BUY
    assert s.exit.pre_side is Side.BUY and s.exit.post_side is Side.SELL
    set_running(s, Block.ENTRY, True)
    # G5: 매도호가창 S괴리(s_spread_exit)를 본다 — 기준(0.5%) 이상이면 미달
    acts = evaluate(s, Block.ENTRY, _sig(s_spread_exit=0.02, hl_disp_ask=-0.02), SETTINGS, U)
    assert acts == [] and "G5 미달" in s.entry.block_reason
    # 통과: 역산가 = 200,000×(1 − 2.0% − 0.5%) = 195,000 → 3,000 올림 195,000; 한계(매도) =
    # (매수1호가 198,500 + 500) × 1.004 = 199,796 → 195,000 ≤ 한계 → SF 매도 선주문
    acts = evaluate(s, Block.ENTRY, _sig(s_spread_exit=-0.01, hl_disp_ask=-0.02), SETTINGS, U)
    assert [a.kind for a in acts] == ["place_pre"]
    assert acts[0].side is Side.SELL and acts[0].qty == 10 and acts[0].price == 195_000
    assert "범위 199,000~199,796" in s.entry.block_reason
    # 역산가가 한계보다 높으면(너무 비싸게 팔려고 물러남) 범위 밖
    on_pre_ack(s, Block.ENTRY, "R1")
    evaluate(s, Block.ENTRY, _sig(s_spread_exit=-0.01, hl_disp_ask=0.03), SETTINGS, U)
    assert "G6 범위 밖" in s.entry.block_reason
    # 체결: SF 매도 4계약 → 후주문 HL 매수 40, RT −4, SF 순잔고 −4
    evaluate(s, Block.ENTRY, _sig(s_spread_exit=-0.01, hl_disp_ask=-0.02), SETTINGS, U)
    acts = on_pre_fill(s, Block.ENTRY, 4, 195_000.0, mono=101.0)
    assert [a.kind for a in acts] == ["place_post"] and acts[0].side is Side.BUY
    assert acts[0].qty == 40 and s.rt == -4 and s.sf_net == -4 and s.held == 4
    assert s.fill_diff == -40  # 후주문 대기 중 — SF −4×10 + HL 0
    acts = on_post_fill(s, Block.ENTRY, 40.0, 1190.0, 1356.1, 102.0, SETTINGS)
    assert s.hl_net == 40 and s.fill_diff == 0 and s.entry.post_pending == 0
    assert acts == [] and s.entry.status is LegStatus.PRE_PARTIAL  # 선주문 6계약 아직 걸림
    # 역방향 청산 = SF 매수 → HL 매도, 수량 Min(1회, RT×−1) = 4, RT는 0 쪽으로
    set_running(s, Block.EXIT, True)
    acts = evaluate(s, Block.EXIT, _sig(mono=140.0), SETTINGS, U)  # 전환대기 30초 지난 뒤
    assert [a.kind for a in acts] == ["place_pre"] and acts[0].side is Side.BUY
    assert acts[0].qty == 4 and acts[0].price == 201_000  # 정방향 진입과 같은 계산(내림·하한)
    on_pre_ack(s, Block.EXIT, "R2")
    on_pre_fill(s, Block.EXIT, 4, 201_000.0, mono=141.0)
    assert s.rt == 0 and s.sf_net == 0
    on_post_fill(s, Block.EXIT, 40.0, 1184.0, 1355.9, 142.0, SETTINGS)
    assert s.hl_net == 0 and s.fill_diff == 0


def test_reverse_sets_and_risk_round_trip_through_dict() -> None:
    # 책의 역방향 3세트(rev_sets)·리스크방지 역방향 값이 저장·복원되고, 복원된 세트는 reverse 유지
    from dataclasses import asdict

    from kp_arb.auto_m import AutoMScreen, autom_from_dict

    screen = AutoMScreen()
    book = screen.book(U)
    # 세트 수: 정방향 8·역방향 4(사용자 확정 2026-09-16, 전엔 4·2)
    assert len(book.sets) == 8 and len(book.rev_sets) == 4
    assert all(s.reverse and s.entry.reverse for s in book.rev_sets)
    assert len(book.all_sets()) == 12 and book.sets_of(True) is book.rev_sets
    book.rev_sets[1].target_qty, book.rev_sets[1].rt, book.rev_sets[1].sf_net = 7, -3, -3
    book.rev_sets[1].en_sf = -0.015
    screen.risk_rev_en, screen.risk_rev_gap = 0.004, 0.002
    raw = asdict(screen)
    restored = AutoMScreen()
    autom_from_dict(restored, raw)
    r = restored.book(U).rev_sets[1]
    assert (r.target_qty, r.rt, r.sf_net, r.en_sf) == (7, -3, -3, -0.015) and r.reverse
    assert r.held == 3 and restored.book(U).sets[1].target_qty == 0
    assert (restored.risk_rev_en, restored.risk_rev_ex, restored.risk_rev_gap) == (
        0.004, 0.0, 0.002)


def test_limit_uses_market_tick_not_order_unit() -> None:
    # 사용자 확정 2026-09-08: 한계의 "상대1호가 − 1틱"에서 1틱은 시세 호가단위(20만 원대 500),
    # 선주문 주문단위(설정 3,000)는 역산가를 주문 단위로 맞출 때만. 매도1호가 201,500 →
    # 한계 (201,500 − 500) × 0.996 = 200,196 (옛 계산은 3,000을 빼 197,706).
    s = _set()
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    assert [a.kind for a in acts] == ["place_pre"]
    # 로그엔 범위의 시작호가(상대1호가 − 1틱 = 201,000)와 한계를 함께(사용자 2026-09-11)
    assert "범위 201,000~200,196" in s.entry.block_reason
    assert "호가단위 500" in s.entry.block_reason
    # 발주 근거에 그때의 SF 1호가·환율(사용자 2026-09-11) + HL 1호가·est(후주문 방향, 2026-09-14)
    assert "매수1 198,500 매도1 201,500 환율 1,349.60" in s.entry.block_reason
    # 진입 후주문 = HL 매도 → 매수호가창 est(191.88)
    assert "HL 매수1 191.9 매도1 191.95 est 191.88" in s.entry.block_reason
    # 청산 후주문 = HL 매수 → 매도호가창 est(191.97) — 같은 근거 줄 형식
    s2 = _set()
    s2.rt = 10
    set_running(s2, Block.EXIT, True)
    evaluate(s2, Block.EXIT, _sig(hl_disp_ask=-0.02), SETTINGS, U)  # 역산가 198,000 ≤ 한계
    assert s2.exit.block_reason.startswith("통과")
    assert "HL 매수1 191.9 매도1 191.95 est 191.97" in s2.exit.block_reason
    assert "주문단위 3000" in s.entry.block_reason  # 역산가 반올림 단위는 그대로 설정값


def test_gate_cancel_is_sent_once_until_confirmed() -> None:
    # 실측 2026-09-08: G2로 막힌 채 매 틱 취소를 다시 보내 0.2초에 3번 나감 → 확인 올 때까지 1번만.
    s = _set()
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "7244")
    off = _sig(mono=101, now=datetime(2026, 9, 4, 16, 0))
    assert [a.kind for a in evaluate(s, Block.ENTRY, off, SETTINGS, U)] == ["cancel_pre"]
    assert evaluate(s, Block.ENTRY, _sig(mono=101.1, now=off.now), SETTINGS, U) == []  # 재전송 없음
    assert evaluate(s, Block.ENTRY, _sig(mono=101.2, now=off.now), SETTINGS, U) == []
    on_pre_cancelled(s, Block.ENTRY, mono=102, settings=SETTINGS)  # 확인 → 표시 정리
    assert not s.entry.cancel_sent and s.entry.pre_order_id is None
    evaluate(s, Block.ENTRY, _sig(mono=103), SETTINGS, U)  # 다시 시간 안 → 새 선주문
    on_pre_ack(s, Block.ENTRY, "7245")
    assert [a.kind for a in evaluate(s, Block.ENTRY, _sig(mono=104, now=off.now), SETTINGS, U)] \
        == ["cancel_pre"]  # 새 주문은 다시 1번 취소 가능


def test_cancel_resent_after_confirm_timeout_and_alarm_after_limit() -> None:
    # exec ㅂ3(2026-09-09): 취소를 보내고 3초 안에 확인이 없으면 "보냄" 표시를 풀고 다시 보낸다.
    # 재전송이 3회를 넘으면 한 번 알람(alarm) — 상태줄 "취소실패", 사람이 수동 취소.
    from kp_arb.auto_m import CANCEL_ALARM_TRIES, CANCEL_CONFIRM_S

    s = _set()
    set_running(s, Block.ENTRY, True)
    s.entry.pre_qty = 1
    on_pre_ack(s, Block.ENTRY, "7001")
    acts = set_running(s, Block.ENTRY, False, mono=100.0)     # 끔 → 취소 1회
    assert [a.kind for a in acts] == ["cancel_pre"] and s.entry.cancel_tries == 1
    assert evaluate(s, Block.ENTRY, _sig(mono=101.0), SETTINGS, U) == []  # 아직 확인 대기
    late = _sig(mono=100.0 + CANCEL_CONFIRM_S)
    acts = evaluate(s, Block.ENTRY, late, SETTINGS, U)         # 3초 지남 → 재전송
    assert [a.kind for a in acts] == ["cancel_pre"] and "재전송 2회" in acts[0].reason
    assert s.entry.cancel_tries == 2 and not s.entry.cancel_alarmed
    mono = late.mono
    kinds: list[str] = []
    while s.entry.cancel_tries <= CANCEL_ALARM_TRIES:
        mono += CANCEL_CONFIRM_S
        kinds += [a.kind for a in evaluate(s, Block.ENTRY, _sig(mono=mono), SETTINGS, U)]
    assert kinds == ["cancel_pre", "cancel_pre", "alarm"]     # 4회째에 알람 한 번
    assert s.entry.cancel_alarmed and s.entry.pre_order_id == "7001"
    mono += CANCEL_CONFIRM_S                                   # 그 뒤로도 재전송 계속, 알람 없음
    again = evaluate(s, Block.ENTRY, _sig(mono=mono), SETTINGS, U)
    assert [a.kind for a in again] == ["cancel_pre"]
    on_pre_cancelled(s, Block.ENTRY, mono=mono, settings=SETTINGS)  # 확인 → 표시 전부 정리
    assert s.entry.cancel_tries == 0 and not s.entry.cancel_alarmed and not s.entry.cancel_sent
    # 시각 없이 보낸 취소(접수 때 등)는 다음 판정 시각부터 확인을 기다린다
    s2 = _set()
    set_running(s2, Block.ENTRY, True)
    s2.entry.status, s2.entry.pre_qty = LegStatus.PRE_RESTING, 1
    set_running(s2, Block.ENTRY, False)
    assert [a.kind for a in on_pre_ack(s2, Block.ENTRY, "7002")] == ["cancel_pre"]
    assert s2.entry.cancel_sent_mono is None
    assert evaluate(s2, Block.ENTRY, _sig(mono=200.0), SETTINGS, U) == []
    assert s2.entry.cancel_sent_mono == 200.0
    resend = evaluate(s2, Block.ENTRY, _sig(mono=203.0), SETTINGS, U)
    assert [a.kind for a in resend] == ["cancel_pre"]


def test_accum_matched_uses_smaller_side() -> None:
    # 사용자 확정 2026-09-08: 매매결과 수량은 LS·HL 누적 체결량 중 적은 쪽(SF 1 = HL 10).
    acc = Accum(hl_qty=0.588, hl_px_sum=0.588 * 1313.1, fx_sum=0.588 * 1340.0,
                sf_qty=1, sf_px_sum=1_836_000.0)
    assert acc.matched_hl() == pytest.approx(0.588) and acc.matched_sf() == pytest.approx(0.0588)
    full = Accum(hl_qty=40.0, hl_px_sum=40 * 1184.0, fx_sum=40 * 1356.0, sf_qty=4,
                 sf_px_sum=4 * 1_602_000.0)
    assert full.matched_hl() == 40 and full.matched_sf() == 4
    more_hl = Accum(hl_qty=45.0, hl_px_sum=45 * 1184.0, fx_sum=45 * 1356.0, sf_qty=4,
                    sf_px_sum=4 * 1_602_000.0)
    assert more_hl.matched_hl() == 40  # HL이 더 많아도 SF 4계약(=40) 기준


def test_block_reason_records_gate_and_basis() -> None:
    # 판정 근거 한 줄 — 어느 게이트에서 막혔는지·통과 시 역산가 계산 근거(로그는 바뀔 때만).
    s = _set()
    assert evaluate(s, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert s.entry.block_reason.startswith("G1")
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(now=datetime(2026, 9, 4, 16, 0)), SETTINGS, U)
    assert s.entry.block_reason.startswith("G2")
    evaluate(s, Block.ENTRY, _sig(s_spread_entry=0.001), SETTINGS, U)
    # G5 미달 근거엔 실시간 괴리값을 안 넣는다(틱마다 바뀌어 로그 도배, 2026-09-10) — 기준값만
    assert "G5 미달" in s.entry.block_reason and "0.500%" in s.entry.block_reason
    assert "0.100%" not in s.entry.block_reason
    evaluate(s, Block.ENTRY, _sig(hl_disp_bid=-0.01), SETTINGS, U)
    assert "G6 범위 밖" in s.entry.block_reason and "범위 201,000~200,196" in s.entry.block_reason
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
    # 체결차 판정(후주문 대기 없음) — 한도 = 1회주문수량×10 = 100(사용자 확정 2026-09-10)
    s2 = _set()
    set_running(s2, Block.ENTRY, True)
    assert halt_if_unhedged(s2, Block.ENTRY, 10) == [] and s2.fill_diff == 10  # 한도 미만 → 계속
    assert [a.kind for a in halt_if_unhedged(s2, Block.ENTRY, 100)] == ["halt", "notify"]
    assert halt_if_unhedged(_set(), Block.ENTRY, 0) == []


def test_accum_sprd_matches_excel_i25() -> None:
    # 엑셀 메인 I25: (환×HL − S현재가)/S현재가 − (SF − SF이론가)/SF이론가
    # S현재가·SF이론가도 후주문 체결 시점 값의 HL 수량 가중평균(사용자 확정 2026-09-10) — 고정값.
    acc = Accum()
    acc.hl_qty, acc.hl_px_sum, acc.fx_sum = 40, 1083.5 * 40, 1413.0 * 40
    acc.sf_qty, acc.sf_px_sum = 4, 1_529_000 * 4
    stock, theory = 1_520_000.0, 1_525_000.0
    acc.s_px_sum, acc.theory_sum, acc.ref_qty = stock * 40, theory * 40, 40
    want = (1413.0 * 1083.5 - stock) / stock - (1_529_000 - theory) / theory
    assert acc.sprd() == pytest.approx(want)
    assert Accum().sprd() is None
    no_ref = Accum(hl_qty=40, hl_px_sum=1083.5 * 40, fx_sum=1413.0 * 40,
                   sf_qty=4, sf_px_sum=1_529_000 * 4)
    assert no_ref.sprd() is None  # 체결 시점 시세가 없었으면 계산 불가(실시간으로 대체 안 함)
    # 옛 누적(기준값 없음, ref_qty 0)에 새 판이 붙으면 분모는 기록된 수량(ref_qty)만 — 안 섞임
    pend = Accum(hl_qty=10, hl_px_sum=1083.5 * 10, fx_sum=1413.0 * 10, sf_qty=1,
                 sf_px_sum=1_529_000, s_px_sum=stock * 10, theory_sum=theory * 10, ref_qty=10)
    no_ref.add_round(pend)
    assert no_ref.hl_qty == 50 and no_ref.ref_qty == 10
    assert no_ref.s_avg() == stock and no_ref.theory_avg() == theory
    acc.clear()
    assert acc.hl_qty == 0 and acc.sf_qty == 0 and acc.s_px_sum == 0 and acc.ref_qty == 0


def test_sprd_is_fixed_at_post_fill_time_not_live() -> None:
    # 사용자 2026-09-10: "매매결과 Sprd가 계속 바뀐다" → 체결 시점 S현재가·SF이론가로 고정.
    # 두 판의 체결 시점 시세가 달라도 각 판 값의 HL 수량 가중평균으로 굳고, 뒤의 시세와 무관.
    s = _set()
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "1")
    on_pre_fill(s, Block.ENTRY, 10, 201_000.0, mono=101)
    on_post_fill(s, Block.ENTRY, 100, 1184.0, 1356.0, mono=102, settings=SETTINGS,
                 stock_last=199_000.0, sf_theory=200_000.0)
    acc = s.entry.acc
    assert acc.s_avg() == 199_000.0 and acc.theory_avg() == 200_000.0
    first = acc.sprd()
    assert first == pytest.approx((1356.0 * 1184.0 - 199_000) / 199_000
                                  - (201_000 - 200_000) / 200_000)
    # 다음 판(딜레이 뒤): 체결 시점 시세가 다름 → 가중평균으로 섞임
    evaluate(s, Block.ENTRY, _sig(mono=104), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "2")
    on_pre_fill(s, Block.ENTRY, 10, 201_000.0, mono=105)
    on_post_fill(s, Block.ENTRY, 100, 1184.0, 1356.0, mono=106, settings=SETTINGS,
                 stock_last=201_000.0, sf_theory=202_000.0)
    assert acc.s_avg() == 200_000.0 and acc.theory_avg() == 201_000.0
    assert acc.sprd() != first and acc.sprd() == acc.sprd()  # 이후 시세와 무관하게 같은 값


def test_price_offset_shifts_order_unit_grid() -> None:
    # 사용자 2026-09-15: 주문단위 3,000의 기준점을 세트마다 둔다. 0이면 0·3,000·6,000…, 1,000이면
    # 1,000·4,000·7,000…. 매수 내림·매도 올림은 그대로.
    from kp_arb.auto_m import price_offset_errors, snap_to_unit

    assert snap_to_unit(Side.BUY, 1_797_500, 3000, 0) == 1_797_000
    assert snap_to_unit(Side.SELL, 1_797_500, 3000, 0) == 1_800_000
    assert snap_to_unit(Side.BUY, 1_797_500, 3000, 1000) == 1_795_000
    assert snap_to_unit(Side.SELL, 1_797_500, 3000, 1000) == 1_798_000
    assert snap_to_unit(Side.BUY, 1_798_000, 3000, 1000) == 1_798_000  # 격자 위면 그대로
    assert pre_order_price(Side.BUY, 200_000, 0.01, 0.005, 3000, 1000) == 199_000
    assert pre_order_price(Side.BUY, 200_000, 0.01, 0.005, 3000) == 201_000  # 기본 0 = 옛 결과
    assert price_offset_errors(0, 3000) == [] and price_offset_errors(2999, 3000) == []
    assert "0 이상" in price_offset_errors(-1, 3000)[0]
    assert "미만" in price_offset_errors(3000, 3000)[0]


def test_gate_applies_set_price_offset_and_rejects_off_tick_price() -> None:
    # 시작호가 1,000 → 역산가 201,000이 199,000으로(범위 밖이라 미발주), 근거 줄에 시작호가 표기.
    s = _set(price_offset=1000)
    set_running(s, Block.ENTRY, True)
    assert evaluate(s, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert s.entry.block_reason.startswith("G6 범위 밖 역산가 199,000")
    # 시작호가 250 → 198,250은 시세 호가단위 500에 안 맞아 LS가 거부 → 내지 않고 사유 표시
    s2 = _set(price_offset=250)
    set_running(s2, Block.ENTRY, True)
    assert evaluate(s2, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert "호가단위 500에 안 맞음" in s2.entry.block_reason
    assert "시작호가 250" in s2.entry.block_reason
    # 통과 줄에도 시작호가가 남는다
    s3 = _set()
    set_running(s3, Block.ENTRY, True)
    evaluate(s3, Block.ENTRY, _sig(), SETTINGS, U)
    assert "주문단위 3000 시작호가 0 " in s3.entry.block_reason


def test_price_offset_survives_restore() -> None:
    from kp_arb.auto_m import AutoMBook, _book_from_dict

    book = AutoMBook()
    _book_from_dict(book, {"sets": [{"target_qty": 5, "price_offset": 1000}, {"target_qty": 6}]})
    assert book.sets[0].price_offset == 1000 and book.sets[1].price_offset == 0


def test_post_fill_without_fx_is_excluded_from_fx_average() -> None:
    # 사용자 확정 2026-09-15: 환진입가는 원달러선물 1호가 → LS 현물환 → 없음. 없으면 그 체결은
    # 환 없이 쌓여 환평균·Sprd 분모에서 빠진다(0으로 희석하지 않는다).
    from kp_arb.auto_m import Accum

    s = _set()
    s.rt = 1
    on_pre_fill(s, Block.ENTRY, 1, 201_000.0, mono=100)
    on_post_fill(s, Block.ENTRY, 4, 1184.0, None, mono=101, settings=SETTINGS,
                 stock_last=199_000.0, sf_theory=200_000.0)
    assert s.entry.pending.fx_avg() is None and s.entry.pending.fx_qty == 0
    on_post_fill(s, Block.ENTRY, 6, 1184.0, 1356.0, mono=102, settings=SETTINGS,
                 stock_last=199_000.0, sf_theory=200_000.0)
    # 10 = 1 SF × 10이라 판이 끝나 누적으로 합쳐짐 — 분모(fx_qty)는 환이 있던 6만
    acc = s.entry.acc
    assert acc.hl_qty == 10 and acc.fx_qty == 6 and acc.fx_avg() == 1356.0
    # 짝맞춤(중지)으로 합칠 때도 환이 있던 판만 분모에 들어간다
    pend = Accum(hl_qty=10, hl_px_sum=11840.0, fx_sum=1356.0 * 6, fx_qty=6, sf_qty=1,
                 sf_px_sum=201_000.0)
    part = Accum()
    part.add_round(pend, matched_only=True)
    assert part.fx_avg() == 1356.0 and part.fx_qty == part.matched_hl() == 10
    no_fx = Accum(hl_qty=10, hl_px_sum=11840.0, sf_qty=1, sf_px_sum=201_000.0)
    part2 = Accum()
    part2.add_round(no_fx, matched_only=True)
    assert part2.fx_qty == 0 and part2.fx_avg() is None
    none_at_all = Accum(hl_qty=10, hl_px_sum=11840.0)
    assert none_at_all.fx_avg() is None and none_at_all.sprd() is None
    old_saved = Accum(hl_qty=10, hl_px_sum=11840.0, fx_sum=13560.0)  # fx_qty 없는 옛 저장본
    assert old_saved.fx_avg() == 1356.0


def test_missing_judgment_fx_halts_the_set() -> None:
    # 사용자 확정 2026-09-15: 판정 환율(현물환 → 원달러선물 이론가)을 못 구하면 세트를 중지한다
    # (G0). 값 없이 판정하지 않고, 사람이 해제해야 재개. 실행이 꺼진 다리는 건드리지 않는다.
    s = _set()
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(fx=None), SETTINGS, U)
    assert [a.kind for a in acts][:2] == ["halt", "notify"]
    assert s.entry.status is LegStatus.HALTED and s.exit.status is LegStatus.HALTED
    assert not s.entry.running and "환율 계산불가" in s.entry.halt_reason
    # 실행이 꺼져 있으면(G1) 환율이 없어도 그냥 대기
    idle = _set()
    assert evaluate(idle, Block.EXIT, _sig(fx=None), SETTINGS, U) == []
    assert idle.exit.status is not LegStatus.HALTED


def test_price_offset_must_be_multiple_of_market_tick() -> None:
    # 사용자 2026-09-15: 잘못 넣으면(삼성 호가단위 500에 시작호가 100) 저장 때 바로 경고창.
    # 코어 스냅샷 sf_tick(지금 가격대 호가단위)이 있을 때만 검사, 없으면 G6이 잡는다.
    from kp_arb.auto_m import price_offset_errors

    samsung = price_offset_errors(100, 500, 500)[0]  # 실측 15:17 — 삼성 500 호가에 100
    assert "호가단위(500)의 배수" in samsung and "가능한 값: 0" in samsung
    msg = price_offset_errors(500, 3000, 1000)[0]
    assert "호가단위(1,000)의 배수" in msg and "가능한 값: 0, 1,000, 2,000" in msg
    assert price_offset_errors(1000, 3000, 1000) == []
    assert price_offset_errors(500, 1000, 500) == []
    assert price_offset_errors(500, 3000, None) == []  # 시세 없음 → 건너뜀


def test_last_round_snapshot_is_kept_per_leg() -> None:
    # 사용자 목업 2026-09-16: 오른쪽 아래 블록 = 그 방향에서 마지막으로 끝난 한 판의 매매내역.
    # 판이 누적에 합쳐질 때 그 판(pending)을 복사해 두고 순번을 매긴다(세트 간 최근 비교용).
    s = _set()
    assert s.entry.last_round is None
    on_pre_fill(s, Block.ENTRY, 1, 201_000.0, mono=100)
    on_post_fill(s, Block.ENTRY, 10, 1184.0, 1356.0, mono=101, settings=SETTINGS,
                 stock_last=199_000.0, sf_theory=200_000.0)
    lr = s.entry.last_round
    assert lr is not None and lr.hl_qty == 10 and lr.sf_avg() == 201_000.0
    assert lr.fx_avg() == 1356.0 and lr.sprd() == s.entry.acc.sprd()
    seq1 = s.entry.last_round_seq
    on_pre_fill(s, Block.ENTRY, 1, 202_000.0, mono=102)
    on_post_fill(s, Block.ENTRY, 10, 1190.0, 1356.0, mono=103, settings=SETTINGS,
                 stock_last=199_000.0, sf_theory=200_000.0)
    assert s.entry.last_round is not None and s.entry.last_round.sf_avg() == 202_000.0
    assert s.entry.last_round_seq > seq1  # 최근 판이 큰 순번
    assert s.entry.acc.sf_qty == 2  # 누적은 두 판 합


def test_set_pre_tick_overrides_common_unit() -> None:
    # 2026-09-16: 선주문 주문단위를 공통설정(종목별)에서 세트설정으로 — 세트값이 있으면 그것,
    # 0이면 공통설정 종목값(옛 저장본 호환). 역산가 201,000은 3,000 배수·1,000 배수 모두 그대로,
    # HL 괴리 2%면 원값 203,000 → 3,000 내림 201,000 / 1,000 내림 203,000.
    s = _set()
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(hl_disp_bid=0.02), SETTINGS, U)
    assert acts[0].price == 201_000.0 and "주문단위 3000" in s.entry.block_reason  # 공통 3,000
    s2 = _set(pre_tick=1000)
    set_running(s2, Block.ENTRY, True)
    acts = evaluate(s2, Block.ENTRY, _sig(hl_disp_bid=0.02), SETTINGS, U)
    assert acts[0].price == 203_000.0 and "주문단위 1000" in s2.entry.block_reason
    from kp_arb.auto_m import AutoMBook, _book_from_dict

    book = AutoMBook()
    _book_from_dict(book, {"sets": [{"pre_tick": 1000}, {"target_qty": 1}]})
    assert book.sets[0].pre_tick == 1000 and book.sets[1].pre_tick == 0


def _stock_set() -> AutoMSet:
    # 주식 종목 상태(exec §7C): 세트·매매결과에 상품(비율 1)이 전파된 세트
    from kp_arb.auto_m import AutoMBook

    book = AutoMBook(product="stock")
    s = book.sets[0]
    s.target_qty, s.per_qty, s.switch_delay_s = 100, 10, 0
    s.en_s, s.ex_sf = 0.0025, -0.001  # 진입 0.25% / 청산 −0.1%
    s.pre_tick = 100
    return s


def _stock_sig(**kw: object) -> Signals:
    # 주식 100,000원, 매수1호가 100,000·매도1호가 100,100, HL 매수호가창 est 74.30 × 1,350 = 100,305
    base = dict(now=NOW, mono=100.0, sf_spread_entry=None, s_spread_entry=None,
                sf_spread_exit=None, hl_disp_bid=None, hl_disp_ask=None, sf_theory=None,
                stock_last=100_000.0, sf_asks=[(100_100.0, 50), (100_200.0, 30)],
                sf_bids=[(100_000.0, 40)], fx=1350.0, hl_bid1=74.3, hl_ask1=74.4,
                hl_est_bid=74.30, hl_est_ask=74.40, product="stock")
    base.update(kw)
    return Signals(**base)  # type: ignore[arg-type]


def test_stock_book_propagates_product_and_ratio() -> None:
    # 주식 종목 상태는 세트 비율 1(주 1 = HL 1), 상대 상품 KR_STOCK, 매매결과 Sprd 주식 식
    from kp_arb.auto_m import AutoMBook, _book_from_dict
    from kp_arb.domain.enums import Instrument

    book = AutoMBook(product="stock")
    assert book.counterpart is Instrument.KR_STOCK and book.hl_ratio == 1
    s = book.sets[0]
    assert s.product == "stock" and s.hl_ratio == 1 and s.entry.acc.stock and s.entry.acc.ratio == 1
    assert fill_diff(3, -3, 1) == 0 and fill_diff(3, -30) == 0  # 주식 1:1 / 주식선물 1:10
    restored = AutoMBook()
    _book_from_dict(restored, {"product": "stock", "sets": [{"target_qty": 5}]})
    assert restored.product == "stock" and restored.sets[0].hl_ratio == 1
    assert restored.sets[0].entry.pending.ratio == 1


def test_stock_book_credit_defaults_live_in_core() -> None:
    # 2026-09-17 실측: 화면만 아래 2세트를 기본 신용으로 그렸더니 코어에 붙자(코어 값이 원본)
    # 풀렸다. 기본값은 코어 종목 상태에 — 새 주식 종목 상태는 5세트 중 4·5번 세트 신용, 복원은
    # 저장값 우선, 저장본에 credit이 없는 옛 세트는 기본 규칙. 주식선물 종목 상태는 전부 False.
    from kp_arb.auto_m import AutoMBook, _book_from_dict

    book = AutoMBook(product="stock")
    assert [s.credit for s in book.sets[:5]] == [False, False, False, True, True]
    assert not any(s.credit for s in AutoMBook().sets)
    restored = AutoMBook()
    _book_from_dict(restored, {"product": "stock",
                               "sets": [{"credit": True}, {}, {}, {"credit": False}]})
    assert [s.credit for s in restored.sets[:5]] == [True, False, False, False, True]


def test_credit_code_for_stock_sets() -> None:
    # 결정 40 + 2026-09-17 추측값: 신용 세트 진입 003(융자신규)·청산 101(융자상환), 아니면 000
    from kp_arb.auto_m import Block, credit_code_for

    assert credit_code_for(Block.ENTRY, False) == "000"
    assert credit_code_for(Block.EXIT, False) == "000"
    assert credit_code_for(Block.ENTRY, True) == "003"
    assert credit_code_for(Block.EXIT, True) == "101"


def test_stock_book_market_default_and_restore() -> None:
    # 2026-09-17: 주식 API는 통합 미지원 → 종목 상태에 거래소(krx/nxt) 보관. 주식 기본 NXT(사용자
    # 같은 날), 저장·복원은 저장값 우선, 잘못된 값은 기본으로. 주식선물 종목 상태는 krx(안 씀).
    from kp_arb.auto_m import AutoMBook, _book_from_dict

    assert AutoMBook(product="stock").market == "nxt" and AutoMBook().market == "krx"
    restored = AutoMBook()
    _book_from_dict(restored, {"product": "stock", "market": "krx"})
    assert restored.market == "krx"
    old = AutoMBook()
    _book_from_dict(old, {"product": "stock"})  # 옛 저장본(거래소 없음) → 기본 NXT
    assert old.market == "nxt"
    bad = AutoMBook()
    _book_from_dict(bad, {"product": "stock", "market": "uni"})
    assert bad.market == "nxt"


def test_stock_entry_uses_top_quote_formula_and_one_to_one_hedge() -> None:
    # exec §7C(사용자 확정 2026-09-16/17): 수치 = (H − 매수1호가)/매수1호가 = 0.305% > 0.25% 통과,
    # 주문가 = H/(1+0.25%) = 100,054.9 → 주문단위 100 내림 100,000, 허용범위(매도1호가 100,100 −
    # 1틱 100)×(1−0.4%) = 99,600 이상 → 발주. 체결되면 HL 후주문 = 주수 × 1.
    from kp_arb.auto_m import stock_monitor_value

    s = _stock_set()
    sig = _stock_sig()
    assert abs(stock_monitor_value(sig, Side.SELL) - 0.00305) < 1e-9
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, sig, SETTINGS, U)
    assert [(a.kind, a.side, a.qty, a.price) for a in acts] == [
        ("place_pre", Side.BUY, 10, 100_000.0)]
    assert "주문가 100,000 = H 100,305/(1+0.250%)" in s.entry.block_reason
    assert "호가단위 100" in s.entry.block_reason
    on_pre_ack(s, Block.ENTRY, "S1")
    acts = on_pre_fill(s, Block.ENTRY, 4, 100_000.0, mono=101)
    assert [(a.kind, a.side, a.qty) for a in acts] == [("place_post", Side.SELL, 4)]  # 1:1
    assert s.entry.post_pending == 4 and s.fill_diff == 4
    on_post_fill(s, Block.ENTRY, 4, 74.3, 1350.0, mono=102, settings=SETTINGS,
                 stock_last=100_000.0, sf_theory=None)
    assert s.rt == 4 and s.fill_diff == 0 and s.entry.post_pending == 0
    # 수치가 낮아도(0.05% < 0.25%) 주식은 **필터 없이** 역산가로 걸어 둔다(사용자 확정 2026-09-18,
    # 주식선물의 S괴리 필터와 다름): H = 74.11×1350 = 100,048 → /1.0025 = 99,799 → 단위 100 내림
    # 99,700 — 매수1호가(100,000) 뒤에 서지만 체결되면 기준값이 보장된다.
    s2 = _stock_set()
    set_running(s2, Block.ENTRY, True)
    acts = evaluate(s2, Block.ENTRY, _stock_sig(hl_est_bid=74.11), SETTINGS, U)
    assert [(a.kind, a.side, a.qty, a.price) for a in acts] == [
        ("place_pre", Side.BUY, 10, 99_700.0)]


def test_stock_sprd_and_halt_limit() -> None:
    # 주식 Sprd = (환×HL평균 − S현재가)/S현재가 한 항, 중지 한도 = 1회주문수량(주)
    from kp_arb.auto_m import Accum, diff_limit

    s = _stock_set()
    assert diff_limit(s) == 10  # 주식선물이면 100
    acc = Accum(hl_qty=4, hl_px_sum=4 * 74.3, fx_sum=4 * 1350.0, fx_qty=4, sf_qty=4,
                sf_px_sum=4 * 100_000.0, s_px_sum=4 * 100_000.0, ref_qty=4, ratio=1, stock=True)
    assert abs(acc.sprd() - (74.3 * 1350.0 - 100_000.0) / 100_000.0) < 1e-12
    assert acc.matched_hl() == 4 and acc.matched_sf() == 4


def test_stock_post_fill_records_sprd_base_without_sf_theory() -> None:
    # 실측 2026-09-17: 주식은 SF이론가가 없어(None) 후주문 체결 때 S현재가 기준값이 기록되지 않아
    # 누적·마지막 판 Sprd가 "-"였다. 주식은 S현재가만 있으면 기록해 Sprd = (환×HL평균 − S)/S.
    s = _stock_set()
    set_running(s, Block.ENTRY, True)
    on_pre_fill(s, Block.ENTRY, 1, 254_500.0, mono=100)  # 주식 1주 → HL 1계약 대기
    on_post_fill(s, Block.ENTRY, 1, 184.6, 1381.9, mono=101, settings=SETTINGS,
                 stock_last=255_000.0, sf_theory=None)
    acc = s.entry.acc
    assert acc.ref_qty == 1 and acc.s_avg() == 255_000.0
    assert abs(acc.sprd() - (1381.9 * 184.6 - 255_000.0) / 255_000.0) < 1e-12
    assert s.entry.last_round is not None and s.entry.last_round.sprd() == acc.sprd()
    # 주식선물은 종전대로 둘 다 있어야 기록
    sf = _set()
    set_running(sf, Block.ENTRY, True)
    on_pre_fill(sf, Block.ENTRY, 1, 201_000.0, mono=100)
    on_post_fill(sf, Block.ENTRY, 10, 1184.0, 1356.1, mono=101, settings=SETTINGS,
                 stock_last=199_000.0, sf_theory=None)
    assert sf.entry.acc.ref_qty == 0 and sf.entry.acc.sprd() is None
