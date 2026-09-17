"""주문 리스트 화면 순수 부분 — 필터(거래소·종목·매매·출처)와 출처 표시(사용자 2026-09-11)."""
from __future__ import annotations

from kp_arb.order_list import _src_label, row_visible, window_title


def test_row_visible_all_passes_everything() -> None:
    flt = {"venue": "전체", "under": "전체", "side": "전체", "source": "전체"}
    assert row_visible(flt, "LS", "samsung", "buy", "자동M")
    assert row_visible(flt, "HL", "sk_hynix", "sell", "")
    assert row_visible({}, "HL", "hyundai", "sell", "따라가기")  # 키 없음 = 전체


def test_row_visible_each_filter() -> None:
    assert row_visible({"venue": "HL"}, "HL", "samsung", "buy", "")
    assert not row_visible({"venue": "HL"}, "LS", "samsung", "buy", "")
    assert row_visible({"under": "하이닉스"}, "LS", "sk_hynix", "buy", "")
    assert not row_visible({"under": "하이닉스"}, "LS", "samsung", "buy", "")
    assert row_visible({"side": "매도"}, "LS", "samsung", "sell", "")
    assert not row_visible({"side": "매도"}, "LS", "samsung", "buy", "")
    # 출처: 자동M / 일반주문(= 자동M 아닌 전부 — 일반주문창·따라가기·미상)
    assert row_visible({"source": "자동M"}, "LS", "samsung", "buy", "자동M")
    assert not row_visible({"source": "자동M"}, "LS", "samsung", "buy", "일반주문창")
    assert row_visible({"source": "일반주문"}, "LS", "samsung", "buy", "일반주문창")
    assert row_visible({"source": "일반주문"}, "LS", "samsung", "buy", "따라가기")
    assert row_visible({"source": "일반주문"}, "LS", "samsung", "buy", "")
    assert not row_visible({"source": "일반주문"}, "LS", "samsung", "buy", "자동M")
    # 여러 필터는 AND
    flt = {"venue": "LS", "under": "삼성", "side": "매수", "source": "자동M"}
    assert row_visible(flt, "LS", "samsung", "buy", "자동M")
    assert not row_visible(flt, "LS", "samsung", "sell", "자동M")


def test_window_title_shows_hidden_count_and_staleness() -> None:
    # 필터로 숨긴 건수가 제목에 — 안 보이는 미체결이 "없는 것"으로 오해되지 않게(실측 2026-09-11)
    assert window_title(0.5, 0, 0) == "주문 리스트 (미체결·취소·정정)"
    assert window_title(0.5, 0, 8) == "주문 리스트 (미체결·취소·정정) — 필터로 8건 숨김"
    assert window_title(7.2, 0, 0) == "주문 리스트 (미체결·취소·정정) — 갱신 지연 7초"
    assert window_title(None, 3, 2) == (
        "주문 리스트 (미체결·취소·정정) — 코어 미접속 — 필터로 2건 숨김")
    assert window_title(None, 0, 0) == "주문 리스트 (미체결·취소·정정)"  # 아직 데이터 전


def test_src_label() -> None:
    assert _src_label("자동M") == "자동M"
    assert _src_label("일반주문창") == "일반"
    assert _src_label("따라가기") == "따라가기"
    assert _src_label("") == "-" and _src_label(None) == "-"


def test_set_filter_and_choices() -> None:
    # 2026-09-16: 주문 리스트 '세트' 칸·콤보 — 자동M 꼬리표("정3진입")의 세트 부분("정3")으로 거른다
    from kp_arb.order_list import row_visible, set_choices

    # 형식(사용자 2026-09-17): 주식선물 선정3진/선역4청, 주식 주정3진/주역4청
    assert set_choices(["선정3진", "선정3청", "선역1진", "주정2진", "", "일반"]) == [
        "전체", "선역1", "선정3", "일반", "주정2"]
    assert set_choices([]) == ["전체"]
    f = {"set": "선정3"}
    assert row_visible(f, "LS", "sk_hynix", "sell", "자동M", "선정3진")
    assert row_visible(f, "HL", "sk_hynix", "buy", "자동M", "선정3청")
    assert not row_visible(f, "LS", "sk_hynix", "sell", "자동M", "선정1진")
    assert not row_visible(f, "LS", "sk_hynix", "sell", "일반주문창", "")
    assert row_visible({"set": "전체"}, "LS", "sk_hynix", "sell", "일반주문창", "")
