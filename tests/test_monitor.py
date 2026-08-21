"""모니터 렌더부 테스트 — 코어 /monitor 스냅샷(dict)을 표 행으로 그리는 순수 함수.

창(tkinter)은 수동 확인. 값 계산은 코어(monitor_snapshot)가 하므로 여기선 포맷만 검증.
"""
from kp_arb.monitor import board_rows, funding_countdown, hl_rows, ls_rows


def test_ls_rows_format_and_name_dedup() -> None:
    snap = {"ls": [
        {"underlying": "samsung", "instrument": "kr_stock",
         "ask": 293_000, "ask_qty": 50, "bid": 292_500, "bid_qty": 100,
         "last": 292_800, "expected": 292_700, "theory": None, "disp": None},
        {"underlying": "samsung", "instrument": "kr_stock_future",
         "ask": None, "ask_qty": None, "bid": None, "bid_qty": None,
         "last": 296_435, "expected": None, "theory": 293_500.0, "disp": 1.0},
    ]}
    rows = ls_rows(snap)
    # (종목, 매도잔량, 매도가, 현재가, 매수가, 매수잔량, 예상가, 이론가, 괴리율%)
    assert rows[0] == ("삼성전자 주식", "50", "293,000", "292,800",
                       "292,500", "100", "292,700", "-", "-")
    assert rows[1][0] == "선물"          # 같은 종목 → 이름 생략, 종류만
    assert rows[1][2] == "-"             # 미수신은 '-'
    assert rows[1][7] == "293,500.00"    # 선물 이론가 (소수 2자리 — 엑셀과 동일)
    assert rows[1][8] == "+1.00"         # 괴리율%(코어 계산)


def test_hl_rows_format_funding_and_countdown() -> None:
    lvo = (184.60 - 184.70) / 184.70 * 100  # 현-오라클%
    mvo = (184.62 - 184.70) / 184.70 * 100  # 마크-오라클%
    snap = {"hl": [
        {"underlying": "samsung", "ask": 184.65, "bid": 184.55, "last": 184.60,
         "oracle": 184.70, "mark": 184.62, "last_vs_oracle": lvo, "mark_vs_oracle": mvo,
         "funding_prev": 0.0001595, "funding_next": 0.0001841},
    ]}
    rows = hl_rows(snap, now_epoch=3600 * 10 + 3540)  # 정각 60초 전
    s = rows[0]
    # (종목,매도가,현재가,오라클,매수가,마크,현-오라클%,마크-오라클%,펀딩전,펀딩피,남은시간)
    assert s[0] == "삼성전자"
    assert s[1] == "184.65" and s[4] == "184.55"  # 매도/매수
    assert s[2] == "184.60"                       # 현재가 = 체결가
    assert s[3] == "184.70" and s[5] == "184.62"  # 오라클 / 마크
    assert s[6] == "-0.054" and s[7] == "-0.043"  # 오라클 대비 %(코어 계산)
    assert s[8] == "0.0159%" and s[9] == "0.0184%"  # 펀딩 직전/예정
    assert s[10] == "01:00"                       # 남은시간


def test_hl_rows_missing_values_dash() -> None:
    snap = {"hl": [
        {"underlying": "hyundai", "ask": None, "bid": None, "last": None,
         "oracle": None, "mark": 184.62, "last_vs_oracle": None,
         "mark_vs_oracle": None, "funding_prev": None, "funding_next": None},
    ]}
    s = hl_rows(snap, now_epoch=0)[0]
    assert s[2] == "-" and s[5] == "184.62"       # 현재가 '-', 마크는 표시
    assert s[6] == "-" and s[8] == "-"            # 괴리·펀딩 미수신 '-'


def test_board_rows_pct_and_est() -> None:
    snap = {"board": [
        {"underlying": "samsung", "instrument": "kr_stock_future",
         "entry": 0.005, "exit": 0.010, "hl_last_d": 0.0, "kr_last_d": 0.0,
         "est_bid": 184.1234, "est_ask": 184.5678, "px_entry": 301_500, "px_exit": 303_000},
    ]}
    r = board_rows(snap)[0]
    assert r[0] == "삼성전자-선물"
    assert r[1] == "0.500" and r[2] == "1.000"    # 소수→% (×100, 소수 3자리)
    assert r[3] == "184.1234" and r[4] == "184.5678"  # est (USD, 소수 4자리)
    assert r[5] == "301,500" and r[6] == "303,000"    # 주문가 (원)


def test_funding_countdown_wraps_hourly() -> None:
    assert funding_countdown(0) == "60:00"
    assert funding_countdown(3599) == "00:01"
    assert funding_countdown(3600) == "60:00"
