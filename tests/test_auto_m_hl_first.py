"""자동M 주식선물 HL선 버전(exec §7D, 시험 2026-09-22) — 선주문 HL ALO, 후주문 LS. 순수 로직."""
from datetime import datetime

from kp_arb.auto_m import (
    PRODUCT_SF_HL_FIRST,
    AutoMBook,
    AutoMSet,
    AutoMSettings,
    LegStatus,
    Signals,
    evaluate,
    hl_first_pre_price,
    on_hl_pre_fill,
    on_hl_pre_reject,
    on_ls_post_fill,
    on_ls_post_reject,
    on_pre_ack,
    on_pre_cancelled,
    set_running,
)
from kp_arb.domain.enums import Side, Underlying
from kp_arb.hl_price import hl_price_step, hl_round_price
from kp_arb.strategy_core import Block

U = Underlying.SK_HYNIX
SETTINGS = AutoMSettings(windows=(("09:00:00", "15:20:00"),), pre_delay_ms=1000)
NOW = datetime(2026, 9, 22, 10, 0, 0)
# 시세: S현재가 1,775,000 · SF이론가 1,778,000 · SF 매도1 1,779,000(괴리 +0.0562%) · 환율 1,376
# (S/환율 = 1,290.0 USD) · HL 매수1 1,297.0 매도1 1,297.3(HL이 S보다 0.54% 높음 — 격자 0.1,
# szDecimals 3). 진입 기준값 0.5% → 역산가 1,297.2(매수1호가 위, 매도1호가 아래)
FX = 1376.0


def _sig(mono: float = 100.0, **kw: object) -> Signals:
    # S괴리(G5, 결정 54): 정방향 진입은 매수호가창 est 기준 0.01 > 진입S 0.005 통과, 역방향 진입은
    # 매도호가창 est 기준 0.0 < 진입S 0.004 통과
    base = dict(now=NOW, mono=mono, sf_spread_entry=None, s_spread_entry=0.01,
                sf_spread_exit=None, s_spread_exit=0.0, hl_disp_bid=None, hl_disp_ask=None,
                sf_theory=1_778_000.0, stock_last=1_775_000.0,
                sf_asks=[(1_779_000.0, 5), (1_780_000.0, 3)],
                sf_bids=[(1_778_000.0, 4), (1_777_000.0, 2)],
                fx=FX, hl_bid1=1297.0, hl_ask1=1297.3,
                hl_bids=[(1297.0, 5.0), (1296.9, 8.0)], hl_asks=[(1297.3, 4.0), (1297.4, 6.0)],
                hl_sz_decimals=3, product=PRODUCT_SF_HL_FIRST)
    base.update(kw)
    return Signals(**base)  # type: ignore[arg-type]


def _set(**kw: object) -> AutoMSet:
    s = AutoMSet(target_qty=10, per_qty=1, switch_delay_s=0, en_sf=0.005, en_s=0.005,
                 ex_sf=-0.001, product=PRODUCT_SF_HL_FIRST)
    s.__post_init__()  # product를 생성자에서 줬으니 줄에도 전파
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_hl_grid_helpers_maker_rounding_and_step() -> None:
    # 메이커 맞춤은 taker와 반대: 매도 올림 / 매수 내림. 격자 한 칸 = 그 가격대 최소 자릿수
    assert hl_round_price(1283.7371, Side.SELL, 3) == 1283.7          # taker 매도 내림
    assert hl_round_price(1283.7371, Side.SELL, 3, maker=True) == 1283.8  # maker 매도 올림
    assert hl_round_price(1283.7371, Side.BUY, 3, maker=True) == 1283.7   # maker 매수 내림
    assert hl_price_step(1283.7, 3) == 0.1 and hl_price_step(185.6, 3) == 0.01
    assert hl_price_step(0, 3) == 0.0


def test_hl_first_pre_price_guarantees_threshold() -> None:
    # 역산가 = S×(1+SF괴리+기준값)/환율, 매도 올림. 그 가격에 HL이 잡히고 LS를 매도1호가에 사면
    # Sprd = (환×HL − S)/S − (SF − 이론가)/이론가 = 기준값(올림분만큼 그 이상)
    s, theory, ask1, thr = 1_775_000.0, 1_778_000.0, 1_779_000.0, 0.005
    p = hl_first_pre_price(Side.SELL, s, ask1, theory, thr, FX, 3)
    sf_disp = (ask1 - theory) / theory
    raw = s * (1 + sf_disp + thr) / FX
    assert p >= raw and p - raw < 0.1  # 격자(0.1) 한 칸 안에서 올림
    sprd = (FX * p - s) / s - sf_disp
    assert sprd >= thr and sprd - thr < 1e-4
    # 청산(HL 매수)은 매수1호가·내림 — 그 가격이면 Sprd(청산) ≤ 기준값
    bid1, thr_x = 1_778_000.0, -0.001
    px = hl_first_pre_price(Side.BUY, s, bid1, theory, thr_x, FX, 3)
    raw_x = s * (1 + (bid1 - theory) / theory + thr_x) / FX
    assert px <= raw_x and raw_x - px < 0.1
    assert (FX * px - s) / s - (bid1 - theory) / theory <= thr_x + 1e-12


def test_g5_s_spread_filter_applies_to_both_directions() -> None:
    # 결정 54(사용자 2026-09-30): HL선도 주식선물과 같은 G5 S괴리 필터 — 정방향 진입 S괴리 > 진입S,
    # 역방향 진입 S괴리 < 진입S. 미달이면 안 내고 걸어둔 선주문은 취소. 청산은 비교 없음.
    s = _set()
    set_running(s, Block.ENTRY, True)
    assert evaluate(s, Block.ENTRY, _sig(s_spread_entry=0.004), SETTINGS, U) == []  # 0.4% < 0.5%
    assert s.entry.block_reason.startswith("G5 미달 S괴리 < 진입S 0.500%")
    assert evaluate(s, Block.ENTRY, _sig(s_spread_entry=None), SETTINGS, U) == []   # 값 없음
    acts = evaluate(s, Block.ENTRY, _sig(s_spread_entry=0.006), SETTINGS, U)
    assert [a.kind for a in acts] == ["place_pre"]
    on_pre_ack(s, Block.ENTRY, "hl-1")
    acts = evaluate(s, Block.ENTRY, _sig(mono=101.0, s_spread_entry=0.004), SETTINGS, U)
    assert [a.kind for a in acts] == ["cancel_pre"]                                 # 미달 → 취소
    # 역방향 진입: 매도호가창 est 기준 S괴리가 진입S(0.4%)보다 작아야 낸다
    r = _rev_set()
    set_running(r, Block.ENTRY, True)
    assert evaluate(r, Block.ENTRY, _sig(s_spread_exit=0.005), SETTINGS, U) == []
    assert r.entry.block_reason.startswith("G5 미달 S괴리 > 진입S 0.400%")
    assert evaluate(r, Block.ENTRY, _sig(s_spread_exit=0.003), SETTINGS, U)[0].kind == "place_pre"
    # 진입S 기준값이 없으면 안 낸다(코어 실행 검사와 같은 뜻)
    s2 = _set(en_s=None)
    set_running(s2, Block.ENTRY, True)
    assert evaluate(s2, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert s2.entry.block_reason == "G5 진입S 기준값 없음"
    # 청산은 S괴리를 보지 않는다
    x = _set(rt=1, sf_net=1, hl_net=-10.0, ex_sf=0.002)  # 청산 역산가 1292.5(범위 안)
    set_running(x, Block.EXIT, True)
    acts = evaluate(x, Block.EXIT, _sig(s_spread_entry=None, s_spread_exit=None), SETTINGS, U)
    assert [a.kind for a in acts] == ["place_pre"]


def test_entry_places_hl_sell_alo_qty_10_at_reverse_price() -> None:
    # 감시 → G5 통과(S괴리 0.01 > 0.005) → HL 매도 선주문 10(=1계약×10) @역산가.
    # 선주문 방향 = HL 다리
    s = _set()
    assert s.hl_first and s.entry.pre_side is Side.SELL and s.entry.post_side is Side.BUY
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    assert [a.kind for a in acts] == ["place_pre"]
    a = acts[0]
    expected = hl_first_pre_price(Side.SELL, 1_775_000.0, 1_779_000.0, 1_778_000.0, 0.005, FX, 3)
    assert a.side is Side.SELL and a.qty == 10 and a.price == expected == 1297.2
    assert 1297.0 < expected < 1297.3  # 매수1호가 위 → ALO로 걸린다(매도1호가 아래라 호가 맨 앞)
    assert s.entry.status is LegStatus.PRE_RESTING and s.entry.pre_qty == 10
    assert "역산가" in s.entry.block_reason and "SF괴리" in s.entry.block_reason


def test_alo_overlap_moves_one_step_inside_best_bid() -> None:
    # 역산가가 HL 매수1호가 이하(기준값이 이미 테이커로도 나오는 상황) → 매수1호가 + 1칸에 건다
    s = _set(en_sf=-0.02)  # 기준값을 낮춰 역산가가 매수1호가 아래로
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    assert acts[0].kind == "place_pre" and acts[0].price == 1297.1  # 1297.0 + 0.1


def test_g6_range_blocks_when_too_far_from_hl_book() -> None:
    # 매도 한계 = (HL 매수N호가 + 1칸) × (1 + 범위). 기준값이 커서 역산가가 그 위면 안 낸다
    s = _set(en_sf=0.05)
    set_running(s, Block.ENTRY, True)
    assert evaluate(s, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert s.entry.block_reason.startswith("G6 범위 밖")


def test_missing_inputs_hold_without_order() -> None:
    s = _set()
    set_running(s, Block.ENTRY, True)
    assert evaluate(s, Block.ENTRY, _sig(hl_bid1=None), SETTINGS, U) == []
    assert s.entry.block_reason.startswith("G6 입력 없음")
    assert evaluate(s, Block.ENTRY, _sig(hl_bids=[]), SETTINGS, U) == []
    assert s.entry.block_reason == "G6 HL 호가창 없음"
    s2 = _set(en_sf=None)
    set_running(s2, Block.ENTRY, True)
    assert evaluate(s2, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert s2.entry.block_reason == "G5 기준값 없음"


def test_fractional_hl_fills_bundle_into_ls_contract_and_round_closes() -> None:
    # HL 체결 0.008 + 3.385 + 6.607 = 10 → 10에 찬 순간 LS 1계약. LS 체결로 판 끝 → RT 1, 체결차 0,
    # 기준값(환·S·이론가)은 LS 체결 시점 값(결정 C)
    s = _set()
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "hl-1")
    assert on_hl_pre_fill(s, Block.ENTRY, 0.008, 1283.9, 101.0) == []
    assert s.entry.status is LegStatus.PRE_PARTIAL and s.entry.hl_unhedged == 0.008
    assert on_hl_pre_fill(s, Block.ENTRY, 3.385, 1283.9, 101.1) == []
    assert s.fill_diff == -3.393  # 조각은 장부에 그대로 보인다
    acts = on_hl_pre_fill(s, Block.ENTRY, 6.607, 1283.9, 101.2)
    assert [(a.kind, a.side, a.qty) for a in acts] == [("place_post", Side.BUY, 1)]
    assert s.entry.status is LegStatus.POST_PENDING and s.entry.post_pending == 1
    assert abs(s.entry.hl_unhedged) < 1e-9 and s.entry.pre_filled == 10
    assert s.rt == 0  # RT는 LS(SF) 체결 때
    # 후주문 대기 중엔 시세가 와도 손대지 않는다
    assert evaluate(s, Block.ENTRY, _sig(mono=102.0), SETTINGS, U) == []
    assert s.entry.block_reason == "후주문/취소 확인 대기"
    acts = on_ls_post_fill(s, Block.ENTRY, 1, 1_779_000.0, 1376.3, 102.5, SETTINGS,
                           stock_last=1_775_000.0, sf_theory=1_778_000.0)
    assert [a.kind for a in acts] == []  # 체결차 0 — 조용히 딜레이
    assert s.rt == 1 and s.sf_net == 1 and abs(s.hl_net + 10) < 1e-9 and s.fill_diff == 0
    assert s.entry.status is LegStatus.SETTLE_DELAY and s.entry.pre_order_id is None
    acc = s.entry.acc
    assert acc.hl_qty == 10 and acc.sf_qty == 1 and acc.fx_avg() == 1376.3
    assert acc.s_avg() == 1_775_000.0 and acc.theory_avg() == 1_778_000.0
    sprd = acc.sprd()
    assert sprd is not None and abs(sprd - ((1376.3 * 1283.9 - 1_775_000) / 1_775_000
                                            - (1_779_000 - 1_778_000) / 1_778_000)) < 1e-12
    assert s.entry.last_round is not None and s.entry.last_round.hl_qty == 10
    assert s.entry.pending.hl_qty == 0  # 이월 조각 없음


def test_leftover_piece_carries_to_next_round() -> None:
    # 선주문 10 중 3.4만 잡히고 취소 → 조각 3.4는 장부·판 버퍼에 남는다(결정 A). 다음 선주문에서
    # 6.6 + 3.4 = 10이 되면 LS 1계약 → 판 끝 때 짝 맞은 10만 누적, 남는 조각 없음
    s = _set()
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "hl-1")
    assert on_hl_pre_fill(s, Block.ENTRY, 3.4, 1283.9, 101.0) == []
    on_pre_cancelled(s, Block.ENTRY, 101.5, SETTINGS)  # 실행 끔·G6 등으로 취소됨
    assert s.entry.status is LegStatus.ARMED and s.entry.hl_unhedged == 3.4
    assert s.fill_diff == -3.4 and s.entry.pending.hl_qty == 3.4
    acts = evaluate(s, Block.ENTRY, _sig(mono=103.0), SETTINGS, U)
    assert acts[0].kind == "place_pre" and acts[0].qty == 10  # 새 선주문도 10(조각과 별개)
    on_pre_ack(s, Block.ENTRY, "hl-2")
    acts = on_hl_pre_fill(s, Block.ENTRY, 6.6, 1284.0, 103.5)
    assert [(a.kind, a.qty) for a in acts] == [("place_post", 1)]  # 6.6 + 3.4 = 10
    assert abs(s.entry.hl_unhedged) < 1e-9 and s.entry.status is LegStatus.PRE_PARTIAL
    # LS가 잡혀도 선주문(잔량 3.4)이 걸려 있으니 판은 안 닫힌다
    assert on_ls_post_fill(s, Block.ENTRY, 1, 1_779_000.0, 1376.0, 104.0, SETTINGS,
                           stock_last=1_775_000.0, sf_theory=1_778_000.0) == []
    assert s.entry.status is LegStatus.PRE_PARTIAL and s.fill_diff == 0
    # 잔량 3.4 체결 → 선주문 전량 → 조각 3.4 이월 상태로 판 끝(선주문 끝 + LS 대기 0은 이미)
    acts = on_hl_pre_fill(s, Block.ENTRY, 3.4, 1284.0, 104.5)
    assert acts == [] and s.entry.status is LegStatus.POST_PENDING
    assert s.entry.post_pending == 0 and s.entry.hl_unhedged == 3.4
    # 판을 닫는 건 LS 후주문 완료 시점 — 여기서는 LS 대기가 0이라 다음 LS 체결이 없다. 실행을 끄면
    # 선주문이 없고 대기도 0이라 대기(idle)로, 조각 3.4는 체결차로 남는다(사람이 정리)
    set_running(s, Block.ENTRY, False)
    assert s.fill_diff == -3.4


def test_ls_post_reject_halts_set_without_resend() -> None:
    # 결정 B: LS 후주문 거부·응답 없음 → 재전송 없이 세트 중지(진입·청산 둘 다), 사유에 체결차
    s = _set()
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "hl-1")
    on_hl_pre_fill(s, Block.ENTRY, 10.0, 1283.9, 101.0)
    acts = on_ls_post_reject(s, Block.ENTRY, "응답 없음 30초(결과 모름)")
    assert [a.kind for a in acts][:2] == ["halt", "notify"]
    assert s.entry.status is LegStatus.HALTED and s.exit.status is LegStatus.HALTED
    assert "재전송 없이" in s.entry.halt_reason and "응답 없음" in s.entry.halt_reason
    assert s.entry.post_pending == 0 and s.fill_diff == -10


def test_alo_cross_reject_not_counted_in_streak() -> None:
    # 사용자 2026-09-22: ALO 겹침 거부는 연속 거부에 세지 않는다 — 딜레이 뒤 재역산. 다른 거부는 셈
    s = _set()
    set_running(s, Block.ENTRY, True)
    for _ in range(5):
        evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
        acts = on_hl_pre_reject(s, Block.ENTRY, 101.0, SETTINGS,
                                "Post only order would have immediately matched", alo_cross=True)
        assert acts[0].kind == "notify" and s.entry.reject_streak == 0
        assert s.entry.status is LegStatus.SETTLE_DELAY
        s.entry.delay_until = None
        s.entry.status = LegStatus.ARMED
    # 그 밖의 거부는 결정 29대로 3회면 중지
    for i in range(3):
        evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
        acts = on_hl_pre_reject(s, Block.ENTRY, 101.0, SETTINGS, "Insufficient margin")
        if i < 2:
            assert s.entry.reject_streak == i + 1
            s.entry.delay_until = None
            s.entry.status = LegStatus.ARMED
    assert s.entry.status is LegStatus.HALTED and acts[0].kind == "halt"


def test_replace_on_price_change_and_exit_direction() -> None:
    # 역산가가 바뀌면 취소 → 재발주(§6 공통 규칙). 청산은 HL 매수·SF 매수1호가·내림·겹침은 매도1−1칸
    s = _set(rt=1, sf_net=1, hl_net=-10.0, ex_sf=0.002)
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "hl-1")
    acts = evaluate(s, Block.ENTRY, _sig(mono=101.0, stock_last=1_776_000.0), SETTINGS, U)
    assert [a.kind for a in acts] == ["cancel_pre"] and s.entry.replace_pending
    assert "역산가 변경 1297.2→" in acts[0].reason
    # 청산: SF 매수1호가 1,778,000(괴리 0) → 역산가 1290.0×1.002 = 1292.58 → 내림 1292.5
    set_running(s, Block.EXIT, True)
    acts = evaluate(s, Block.EXIT, _sig(), SETTINGS, U)
    assert acts[0].kind == "place_pre" and acts[0].side is Side.BUY and acts[0].qty == 10
    expected = hl_first_pre_price(Side.BUY, 1_775_000.0, 1_778_000.0, 1_778_000.0, 0.002, FX, 3)
    assert acts[0].price == expected == 1292.5
    # 청산 겹침: 기준값이 커서 역산가가 매도1호가(1297.3) 이상이면 한 칸 아래 1297.2
    s2 = _set(rt=1, sf_net=1, hl_net=-10.0, ex_sf=0.01)
    set_running(s2, Block.EXIT, True)
    acts = evaluate(s2, Block.EXIT, _sig(), SETTINGS, U)
    assert acts[0].kind == "place_pre" and acts[0].price == 1297.2


def test_order_unit_from_set_snaps_price_and_damps_replace() -> None:
    # 결정 55(사용자 2026-09-30): HL 선주문 주문단위·시작호가 = 세트설정(USD). 역산가 1297.2 →
    # 단위 0.5면 매도 올림 1297.5, 매수(청산)는 내림. 범위의 1틱도 그 단위. (09-22의 화면 호가단위
    # 콤보는 다른 창과 공유돼 주문에 안 씀)
    from kp_arb.auto_m import hl_snap_to_unit

    assert hl_snap_to_unit(1297.2, Side.SELL, 0.5, 0.1) == 1297.5
    assert hl_snap_to_unit(1297.5, Side.SELL, 0.5, 0.1) == 1297.5  # 이미 배수면 그대로
    assert hl_snap_to_unit(1292.58, Side.BUY, 0.5, 0.1) == 1292.5
    assert hl_snap_to_unit(207.31, Side.SELL, 0.05, 0.01) == 207.35  # 삼성 200달러대 격자 0.01
    # 시작호가: 0.5 단위에 시작 0.2 → 격자 …1297.2·1297.7… (매도 올림 1297.7, 매수 내림 1297.2)
    assert hl_snap_to_unit(1297.3, Side.SELL, 0.5, 0.1, 0.2) == 1297.7
    assert hl_snap_to_unit(1297.3, Side.BUY, 0.5, 0.1, 0.2) == 1297.2

    s = _set(hl_unit=0.5)
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    assert acts[0].kind == "place_pre" and acts[0].price == 1297.5
    assert "주문단위 0.5" in s.entry.block_reason and "호가단위 0.5" in s.entry.block_reason
    on_pre_ack(s, Block.ENTRY, "hl-1")
    # S현재가가 조금 움직여 원값이 1297.3 → 같은 단위 칸(1297.5) → 재발주 없음
    acts = evaluate(s, Block.ENTRY, _sig(mono=101.0, stock_last=1_775_100.0), SETTINGS, U)
    assert acts == [] and s.entry.block_reason.startswith("유지 역산가 1297.5")
    # 단위 칸을 넘으면(원값 1297.9 → 1298.0) 취소 → 재발주
    acts = evaluate(s, Block.ENTRY, _sig(mono=102.0, stock_last=1_776_000.0), SETTINGS, U)
    assert [a.kind for a in acts] == ["cancel_pre"]
    # 단위 0(또는 격자 이하)이면 격자 그대로(1297.2)
    s2 = _set(hl_unit=0.0)
    set_running(s2, Block.ENTRY, True)
    assert evaluate(s2, Block.ENTRY, _sig(), SETTINGS, U)[0].price == 1297.2
    s3 = _set(hl_unit=0.01)
    set_running(s3, Block.ENTRY, True)
    assert evaluate(s3, Block.ENTRY, _sig(), SETTINGS, U)[0].price == 1297.2
    # 시작호가 0.2 + 단위 0.5 → 1297.2 그대로(격자 위), 1297.13이면 올림 1297.2
    s4 = _set(hl_unit=0.5, hl_offset=0.2)
    set_running(s4, Block.ENTRY, True)
    assert evaluate(s4, Block.ENTRY, _sig(), SETTINGS, U)[0].price == 1297.2
    # 격자에 안 맞는 단위(0.25 — 격자 0.1)면 안 낸다: HL이 격자 밖 가격을 거부하므로
    s5 = _set(hl_unit=0.25)
    set_running(s5, Block.ENTRY, True)
    assert evaluate(s5, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert s5.entry.block_reason.startswith("G6 주문단위 0.25·시작호가 0이 HL 격자 0.1에 안 맞음")


def test_hl_unit_errors_for_set_dialog() -> None:
    # 세트설정 경고창(사용자 2026-09-30): 주문단위는 0(격자) 또는 격자 배수, 시작호가는 0 이상·
    # 주문단위 미만·격자 배수. 격자를 모르면(시세 없음) 배수 검사는 건너뛴다.
    from kp_arb.auto_m import hl_unit_errors

    assert hl_unit_errors(0.0, 0.0, 0.1) == []
    assert hl_unit_errors(0.5, 0.0, 0.1) == []
    assert hl_unit_errors(0.5, 0.2, 0.1) == []
    assert hl_unit_errors(1.0, 0.9, 0.1) == []
    assert any("배수" in e for e in hl_unit_errors(0.25, 0.0, 0.1))
    assert any("배수" in e for e in hl_unit_errors(0.5, 0.15, 0.1))
    assert any("미만" in e for e in hl_unit_errors(0.5, 0.5, 0.1))
    assert any("미만" in e for e in hl_unit_errors(0.0, 0.1, 0.1))  # 단위 0 = 격자 0.1 → 시작 < 0.1
    assert any("0 이상" in e for e in hl_unit_errors(-0.1, 0.0, 0.1))
    assert hl_unit_errors(0.25, 0.0, None) == []  # 격자 모름 → 배수 검사 생략(G6이 잡음)
    assert hl_unit_errors(0.05, 0.0, 0.01) == []  # 삼성 200달러대


def _rev_set(**kw: object) -> AutoMSet:
    """역방향 세트(exec §7D 역방향) — 진입 = HL 매수 ALO → LS SF 매도, 청산 = HL 매도 ALO → LS SF
    매수. 기준값: 진입 +HP/-SF 0.4%(그 아래로 HL을 산다), 청산 -HP/+SF 0.7%(그 위로 HL을 판다)."""
    s = _set(en_sf=0.004, en_s=0.004, ex_sf=0.007, **kw)
    s.reverse = True
    s.__post_init__()
    return s


def test_reverse_entry_places_hl_buy_alo_and_sprd_stays_under_threshold() -> None:
    # 역방향 진입 = 정방향 청산과 같은 주문(HL 매수 → LS SF 매도): SF 매수1호가(1,778,000, 괴리 0)
    # 기준, 역산가 = 1,775,000×(1+0+0.004)/1376 = 1295.13 → 메이커 매수라 내림 1295.1.
    # 그 값 이하로 HL을 사고 LS를 매수1호가에 팔면 Sprd ≤ 기준값.
    s = _rev_set()
    assert s.entry.pre_side is Side.BUY and s.entry.post_side is Side.SELL
    assert s.exit.pre_side is Side.SELL and s.exit.post_side is Side.BUY
    set_running(s, Block.ENTRY, True)
    acts = evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    assert [(a.kind, a.side, a.qty, a.price) for a in acts] == [
        ("place_pre", Side.BUY, 10, 1295.1)]
    assert s.entry.pre_est == 1_778_000.0  # 발주 때 본 SF 호가 = 매수1호가(LS를 팔 값)
    assert (FX * 1295.1 - 1_775_000) / 1_775_000 <= 0.004
    # 겹침: 기준값이 커서 역산가가 HL 매도1호가(1297.3) 이상이면 한 칸 아래
    s2 = _rev_set()
    s2.en_sf = 0.01
    set_running(s2, Block.ENTRY, True)
    assert evaluate(s2, Block.ENTRY, _sig(), SETTINGS, U)[0].price == 1297.2
    # 범위 밖: 너무 싸게 사려고 물러나 있으면(한계 = (매도1 − 1칸)×(1−범위) 아래) 안 낸다
    s3 = _rev_set()
    s3.en_sf = -0.02
    set_running(s3, Block.ENTRY, True)
    assert evaluate(s3, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert s3.entry.block_reason.startswith("G6 범위 밖")


def test_reverse_round_trip_rt_goes_negative_and_back() -> None:
    # 역방향 진입: HL 매수 조각 4 + 6 → 10에 찬 순간 LS SF **매도** 1계약 → LS 체결로 RT −1,
    # 장부 SF −1·HL +10, 체결차 0. 청산: HL 매도 10 → LS SF 매수 1 → RT 0, 장부 0.
    s = _rev_set()
    set_running(s, Block.ENTRY, True)
    evaluate(s, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s, Block.ENTRY, "hl-1")
    assert on_hl_pre_fill(s, Block.ENTRY, 4.0, 1295.1, 101.0) == []
    assert s.fill_diff == 4.0 and s.hl_net == 4.0  # 역방향은 HL이 + 쪽으로 쌓인다
    acts = on_hl_pre_fill(s, Block.ENTRY, 6.0, 1295.1, 101.1)
    assert [(a.kind, a.side, a.qty) for a in acts] == [("place_post", Side.SELL, 1)]
    assert s.rt == 0 and s.entry.status is LegStatus.POST_PENDING
    assert on_ls_post_fill(s, Block.ENTRY, 1, 1_778_000.0, FX, 102.0, SETTINGS,
                           stock_last=1_775_000.0, sf_theory=1_778_000.0) == []
    assert s.rt == -1 and s.sf_net == -1 and s.hl_net == 10.0 and s.fill_diff == 0
    assert s.entry.status is LegStatus.SETTLE_DELAY
    sprd = s.entry.acc.sprd()
    assert sprd is not None and sprd <= 0.004
    # 청산할 물량 = RT×−1 = 1계약 → HL 매도 10. SF 매도1호가(1,779,000, 괴리 +0.056%) 기준,
    # 1,775,000×(1+0.000562+0.007)/1376 = 1299.75 → 메이커 매도라 올림 1299.8
    set_running(s, Block.EXIT, True)
    acts = evaluate(s, Block.EXIT, _sig(mono=110.0), SETTINGS, U)
    assert [(a.kind, a.side, a.qty, a.price) for a in acts] == [
        ("place_pre", Side.SELL, 10, 1299.8)]
    on_pre_ack(s, Block.EXIT, "hl-2")
    acts = on_hl_pre_fill(s, Block.EXIT, 10.0, 1299.8, 111.0)
    assert [(a.kind, a.side, a.qty) for a in acts] == [("place_post", Side.BUY, 1)]
    assert s.fill_diff == -10.0  # SF −1(×10) + HL 0
    assert on_ls_post_fill(s, Block.EXIT, 1, 1_779_000.0, FX, 112.0, SETTINGS,
                           stock_last=1_775_000.0, sf_theory=1_778_000.0) == []
    assert s.rt == 0 and s.sf_net == 0 and s.hl_net == 0 and s.fill_diff == 0
    sprd_x = s.exit.acc.sprd()
    assert sprd_x is not None and sprd_x >= 0.007
    # 들고 있는 것이 없으면 청산은 안 낸다
    s.exit.delay_until = None
    s.exit.status = LegStatus.ARMED
    assert evaluate(s, Block.EXIT, _sig(mono=120.0), SETTINGS, U) == []
    assert s.exit.block_reason.startswith("G4 여유 없음")


def test_reverse_entry_stops_at_target_and_ls_reject_halts() -> None:
    # 목표 1계약을 채우면(RT −1) 진입은 더 안 낸다. LS 후주문 실패는 정방향과 같이 재전송 없이 중지
    s = _rev_set(target_qty=1, rt=-1, sf_net=-1, hl_net=10.0)
    set_running(s, Block.ENTRY, True)
    assert evaluate(s, Block.ENTRY, _sig(), SETTINGS, U) == []
    assert s.entry.block_reason.startswith("G4 여유 없음")
    s2 = _rev_set()
    set_running(s2, Block.ENTRY, True)
    evaluate(s2, Block.ENTRY, _sig(), SETTINGS, U)
    on_pre_ack(s2, Block.ENTRY, "hl-1")
    on_hl_pre_fill(s2, Block.ENTRY, 10.0, 1295.1, 101.0)
    acts = on_ls_post_reject(s2, Block.ENTRY, "증거금 부족")
    assert acts[0].kind == "halt" and s2.entry.status is LegStatus.HALTED
    assert s2.exit.status is LegStatus.HALTED and s2.fill_diff == 10.0  # HL만 잡힌 채(+10)


def test_book_propagates_product_to_legs_and_restore() -> None:
    from kp_arb.auto_m import book_key, parse_book_key

    book = AutoMBook(product=PRODUCT_SF_HL_FIRST)
    assert book.hl_ratio == 10 and all(st.hl_first for _r, _i, st in book.all_sets())
    assert all(st.entry.hl_first and st.exit.hl_first for _r, _i, st in book.all_sets())
    assert book.sets[0].entry.pre_side is Side.SELL and book.rev_sets[0].entry.pre_side is Side.BUY
    assert parse_book_key(book_key(U, PRODUCT_SF_HL_FIRST)) == (U, PRODUCT_SF_HL_FIRST)


def test_stock_auction_skips_s_spread_filter_in_hl_first() -> None:
    # 결정 57: HL선도 같은 S괴리를 쓰므로 주식 동시호가엔 필터를 건너뛴다(정·역방향)
    s = _set()
    set_running(s, Block.ENTRY, True)
    assert evaluate(s, Block.ENTRY, _sig(s_spread_entry=0.004), SETTINGS, U) == []
    acts = evaluate(s, Block.ENTRY, _sig(mono=101.0, s_spread_entry=0.004, stock_auction=True),
                    SETTINGS, U)
    assert [a.kind for a in acts] == ["place_pre"]
    assert "동시호가: S괴리 필터 생략" in s.entry.block_reason
    r = _rev_set()
    set_running(r, Block.ENTRY, True)
    assert evaluate(r, Block.ENTRY, _sig(s_spread_exit=0.005), SETTINGS, U) == []
    assert evaluate(r, Block.ENTRY, _sig(mono=101.0, s_spread_exit=0.005, stock_auction=True),
                    SETTINGS, U)[0].kind == "place_pre"
