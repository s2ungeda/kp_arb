"""자동M 리스크방지 입력 검증(check_risk) 단위 테스트 (DESIGN-auto-m-exec §11.9).

검사 대상은 진입SF·청산뿐 — 진입S는 검사하지 않는다(사용자 2026-09-09).
"""
from __future__ import annotations

from kp_arb.order_autom import check_risk

# 공통설정 기본값 (목업): 진입 0, 청산 0.5, gap 0.1
EN, EX, GAP = 0.0, 0.5, 0.1


def test_fwd_valid() -> None:
    # 진입SF 0.5(>0), 청산 -0.1(<0.5), 진입−청산 0.6(>0.1)
    assert check_risk("fwd", 0.5, -0.1, EN, EX, GAP) == []


def test_fwd_entry_not_above_zero() -> None:
    errs = check_risk("fwd", 0.0, -0.1, EN, EX, GAP)
    assert any("진입SF" in e for e in errs)
    assert not any("진입S는" in e for e in errs)  # 진입S는 검사 대상 아님


def test_fwd_exit_not_below_half() -> None:
    errs = check_risk("fwd", 0.5, 0.5, EN, EX, GAP)
    assert any("청산" in e for e in errs)


def test_dividend_is_added_to_inputs_and_limits_alike() -> None:
    # exec §11.9 배당율(사용자 2026-11-13, 정정 "설정에 입력된 수치에도 배당율 더해서"): 진입SF·청산
    # 입력값과 설정 한도 **양쪽**에 q(%)를 더해 비교 → 판정은 배당 없는 달과 같고, 경고 글에만
    # 더한 값이 보인다. gap은 그대로.
    assert check_risk("fwd", 0.5, -0.1, EN, EX, GAP, dividend=0.0) == []
    # q=0이면 경고 글도 옛 그대로
    assert check_risk("fwd", 0.0, -0.5, EN, EX, GAP) == ["정방향 진입SF는 0 초과여야 합니다"]
    # 정방향 청산 0.3(< 0.5 통과)은 q 0.3을 더해도 통과(0.6 < 0.8) — 한도도 같이 오르므로
    assert check_risk("fwd", 0.5, 0.3, EN, EX, GAP, dividend=0.3) == []
    # 정방향 청산 0.5(위반)는 q를 더해도 위반(0.8 ≥ 0.8) — 글에 양쪽 더한 값
    errs = check_risk("fwd", 0.9, 0.5, EN, EX, GAP, dividend=0.3)
    assert errs == ["정방향 청산(0.5+배당 0.3=0.8)은 0.8(0.5+배당 0.3) 미만이어야 합니다"]
    # 정방향 진입 −0.2(위반)는 q를 더해도 위반(0.1 ≤ 0.3)
    errs = check_risk("fwd", -0.2, -0.5, EN, EX, GAP, dividend=0.3)
    assert errs == ["정방향 진입SF(-0.2+배당 0.3=0.1)는 0.3(0+배당 0.3) 초과여야 합니다"]
    # 역방향(진입 < 0.5, 청산 > 0): 진입 0.3·청산 0.5는 q를 더해도 통과, 진입 0.5는 위반
    assert check_risk("rev", 0.3, 0.5, 0.5, 0.0, GAP, dividend=0.3) == []
    errs = check_risk("rev", 0.5, 0.7, 0.5, 0.0, GAP, dividend=0.3)
    assert errs == ["역방향 진입SF(0.5+배당 0.3=0.8)는 0.8(0.5+배당 0.3) 미만이어야 합니다"]
    # 미입력(None)은 q가 있어도 건너뛴다
    assert check_risk("fwd", None, None, EN, EX, GAP, dividend=0.3) == []


def test_fwd_gap_too_small() -> None:
    # 진입 0.15, 청산 0.1 → 차이 0.05 ≤ 0.1
    errs = check_risk("fwd", 0.15, 0.1, EN, EX, GAP)
    assert any("진입SF−청산" in e for e in errs)


def test_rev_valid() -> None:
    # 진입SF -1.5(<0), 청산 0.9(>0.5), 청산−진입 2.4(>0.1)
    assert check_risk("rev", -1.5, 0.9, EN, EX, GAP) == []


def test_rev_entry_not_below_zero() -> None:
    errs = check_risk("rev", 0.0, 0.9, EN, EX, GAP)
    assert any("진입SF" in e for e in errs)


def test_rev_exit_not_above_half() -> None:
    errs = check_risk("rev", -1.5, 0.5, EN, EX, GAP)
    assert any("청산" in e for e in errs)


def test_rev_gap_too_small() -> None:
    # 청산 -0.9, 진입 -0.95 → 차이 0.05 ≤ 0.1
    errs = check_risk("rev", -0.95, -0.9, EN, EX, GAP)
    assert any("청산−진입SF" in e for e in errs)


def test_none_values_skipped() -> None:
    # 미입력(None)은 검증 건너뜀 — 빈 화면은 통과
    assert check_risk("fwd", None, None, EN, EX, GAP) == []
    # 청산만 있고 진입 없으면 gap 검증 안 함
    assert check_risk("fwd", None, -0.1, EN, EX, GAP) == []


def test_s_auction_bg_green_only_during_stock_auction() -> None:
    # 사용자 2026-09-30(exec 결정 57): 진입S 모니터 칸은 주식 동시호가 중 초록, 끝나면 다시 빨강.
    # 코어 /state의 phase 'pre_open'(장전 동시호가)만 지금 인식 — 장마감 동시호가 코드는 실측 뒤
    # 추가.
    from kp_arb.order_autom import C_AUCTION, C_S_NORMAL, s_auction_bg

    assert s_auction_bg("pre_open") == C_AUCTION
    assert s_auction_bg("regular") == C_S_NORMAL
    assert s_auction_bg("dead") == C_S_NORMAL
    assert s_auction_bg(None) == C_S_NORMAL


def test_slot_bar_text_shows_current_or_next_slot_only() -> None:
    # 결정 80 + 사용자 2026-10-15 "1번": 상단 시간 표시는 지금 든 구간 하나
    # 'HH:MM:SS~HH:MM:SS KRX (i/n)',
    # 구간 밖이면 '다음 …', 사용 구간 없으면 '주문가능시간 없음'. 전체 목록은 마우스 올리면 팝업.
    from datetime import time as _t

    from kp_arb.order_autom import slot_bar_text

    slots = [{"enabled": True, "start": "09:00:00", "end": "10:20:00", "market": "krx"},
             {"enabled": False, "start": "10:30:00", "end": "11:00:00", "market": "nxt"},
             {"enabled": True, "start": "12:00:05", "end": "13:50:00", "market": "nxt"},
             {"enabled": True, "start": "15:40:00", "end": "20:00:00", "market": "nxt"}]
    short, full = slot_bar_text(slots, _t(9, 30))
    assert short == "09:00:00~10:20:00 KRX (1/3)"
    assert full == "1: 09:00:00~10:20:00 KRX\n2: 12:00:05~13:50:00 NXT\n3: 15:40:00~20:00:00 NXT"
    assert slot_bar_text(slots, _t(11, 0))[0] == "다음 12:00:05~13:50:00 NXT (2/3)"
    assert slot_bar_text(slots, _t(13, 0))[0] == "12:00:05~13:50:00 NXT (2/3)"
    assert slot_bar_text(slots, _t(21, 0))[0] == "다음 09:00:00~10:20:00 KRX (1/3)"  # 오늘 끝 → 첫
    off = [dict(s, enabled=False) for s in slots]
    assert slot_bar_text(off, _t(9, 0)) == ("주문가능시간 없음", "")
    # 주식선물: 거래소 없이(사용자 2026-10-15 "2구간, 거래소 콤보 제외")
    sf = [{"enabled": True, "start": "08:30:10", "end": "08:46:20", "market": "krx"},
          {"enabled": True, "start": "15:35:30", "end": "15:46:55", "market": "krx"}]
    assert slot_bar_text(sf, _t(8, 40), with_market=False) == (
        "08:30:10~08:46:20 (1/2)", "1: 08:30:10~08:46:20\n2: 15:35:30~15:46:55")


def test_rt_sum_per_direction_keeps_sign_and_skips_missing() -> None:
    # 사용자 2026-11-13: RT선진입 머리 위에 그 방향 세트들의 RT 합 — 역방향은 음수 그대로,
    # 값 없는 행은 건너뛰고, 하나도 없으면 None("-")
    from kp_arb.order_autom import rt_sum

    assert rt_sum([{"rt": 5}, {"rt": 3.0}, {"rt": None}, {}]) == 8
    assert rt_sum([{"rt": -4}, {"rt": -1}]) == -5
    assert rt_sum([{"rt": None}, {}]) is None and rt_sum([]) is None
    assert rt_sum([{"rt": True}]) is None                     # bool은 숫자로 안 센다


def test_fx_caption_shows_spot_source() -> None:
    # 사용자 2026-10-16: 괴리 모니터는 '현물CUR(하나고시)'인데 체결쏴는 '(현물)'뿐이라 CUR
    # 미수신으로 오해 → 현물이면 원천(하나고시/LS, 결정 70)까지. 선물역산·값 없음은 종전 그대로.
    from kp_arb.order_autom import fx_caption

    # 2026-11-13 사용자 "최대 줄이기" — 값 + 출처 한 글자(현/선/하), 값 없으면 '-'
    assert fx_caption(None, "현물", "LS") == "-"
    assert fx_caption(0, "현물") == "-"
    assert fx_caption(1340.0, "현물", "하나고시") == "1,340.00(하)"
    assert fx_caption(1339.5, "현물", "LS") == "1,339.50(현)"
    assert fx_caption(1339.5, "현물") == "1,339.50(현)"               # 출처 미상(옛 코어)
    assert fx_caption(1338.29, "선물역산", "LS") == "1,338.29(선)"
    assert fx_caption(1338.29, None) == "1,338.29"
    assert fx_caption(1338.29, "기타") == "1,338.29(기타)"              # 모르는 출처는 그대로



def test_save_payload_rt_only_when_row_edited_and_never_clear() -> None:
    # 결정 82(2026-10-16, 사용자): 닫을 때 저장은 실행 중인 세트도 보내므로 세트설정 창의 RT 수동
    # 입력값·체결차 Clear는 안 실린다("실행 중에는 RT가 바뀔 수 있는데 저장 때 덮어쓸 수 있다").
    # 행 RT 칸을 고쳐 둔 값(실행 중엔 잠겨 못 고침 → 실행 중 아닌 세트뿐)만 rt_manual로 실린다
    # ("RT도 저장 안 되는 듯"). 나머지는 그대로.
    from kp_arb.order_autom import save_payload, set_payload

    w = {"target": 10, "per": 2, "delay": 30, "en_sf": 0.5, "en_s": 0.4, "ex_sf": -0.1,
         "rt_manual": 7, "clear_diff": True, "credit": True, "tick": 500}
    raw = set_payload(2, w, "samsung", "fwd")
    assert raw["rt_manual"] == 7 and raw["clear_diff"] is True
    out = save_payload(raw)                      # 행 RT 안 고침 → RT 안 실림
    assert out["rt_manual"] is None and out["clear_diff"] is False
    assert save_payload(raw, 3)["rt_manual"] == 3  # 행 RT를 3으로 고쳐 둠 → 그 값
    rest = {k: v for k, v in out.items() if k not in ("rt_manual", "clear_diff")}
    assert rest == {k: v for k, v in raw.items() if k not in ("rt_manual", "clear_diff")}
    assert raw["rt_manual"] == 7  # 원본은 안 건드림


def test_market_label_maps_core_value_to_screen_text() -> None:
    # 결정 80: 지금 거래소 라벨은 코어 종목 상태의 market을 따라간다(운영 실측 2026-10-19: 구간이
    # NXT로 바뀌어 주문은 NXT로 나가는데 라벨은 창 열 때의 'KRX'에 머묾 → 갱신 루프에서 따라가게).
    from kp_arb.order_autom import market_label

    assert market_label("krx") == "KRX"
    assert market_label("nxt") == "NXT"
    assert market_label(None) == "-" and market_label("uni") == "-"
