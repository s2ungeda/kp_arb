"""SessionService 계약 테스트. 실측 JIF 프레임(시장 단위) → 세션 맵 검증.

[정합 v6.3] JIF는 시장 단위(tr_key="0", body={jangubun, jstatus})임이 라이브 실측으로
확인되어, 종전의 '종목 단위(jang_cd)' 가정 테스트를 실제 계약으로 정정했다.
"""
from kp_arb.domain.enums import Instrument, SessionPhase, Underlying
from kp_arb.gateways.ls_ws import MarketStatus
from kp_arb.session import reference_instrument, tradeable_instruments
from kp_arb.session_service import (
    SessionService,
    classify_jstatus,
    market_of_instrument,
    phase_from_jif,
)

SAMSUNG = Underlying.SAMSUNG


def jif(jstatus: str, *, jangubun: str = "1") -> MarketStatus:
    """실측 JIF 프레임(파싱된 MarketStatus). 시장 단위 — tr_key는 '0'."""
    return MarketStatus(tr_key="0", body={"jangubun": jangubun, "jstatus": jstatus})


# --- jstatus → phase 매핑 (순수 함수) ---


def test_phase_from_jif_mapping() -> None:
    assert phase_from_jif({"jangubun": "1", "jstatus": "11"}) is SessionPhase.PRE_OPEN
    assert phase_from_jif({"jangubun": "1", "jstatus": "24"}) is SessionPhase.PRE_OPEN  # 실측
    assert phase_from_jif({"jangubun": "1", "jstatus": "21"}) is SessionPhase.REGULAR
    assert phase_from_jif({"jangubun": "1", "jstatus": "41"}) is SessionPhase.DEAD


def test_phase_from_jif_unknown_is_dead() -> None:
    assert phase_from_jif({"jangubun": "1", "jstatus": "99"}) is SessionPhase.DEAD
    assert phase_from_jif({}) is SessionPhase.DEAD  # 누락도 보수적으로 DEAD


# --- jstatus 분류: 시간대 / 정지 / 변동성 (순수 함수, §8) ---


def test_classify_time_phase() -> None:
    assert classify_jstatus("1", "21") == ("phase", SessionPhase.REGULAR)
    assert classify_jstatus("1", "11") == ("phase", SessionPhase.PRE_OPEN)


def test_classify_stock_halt_and_resume() -> None:
    assert classify_jstatus("1", "64") == ("halt", "사이드카매도")
    assert classify_jstatus("1", "66") == ("halt", "사이드카매수")
    assert classify_jstatus("1", "61") == ("halt", "서킷1")
    assert classify_jstatus("1", "68") == ("halt", "서킷2")
    assert classify_jstatus("1", "65") == ("resume", None)  # 사이드카 해제
    assert classify_jstatus("1", "63") == ("resume", None)  # 서킷1 동시호가종료
    assert classify_jstatus("1", "69") == ("phase", SessionPhase.DEAD)  # 서킷3=당일종료


def test_classify_futures_halt_info_close() -> None:
    assert classify_jstatus("5", "63") == ("halt", "서킷")
    assert classify_jstatus("5", "62") == ("resume", None)
    assert classify_jstatus("5", "61") == ("phase", SessionPhase.DEAD)  # 당일 장종료
    assert classify_jstatus("5", "70")[0] == "info"  # 변동성 확대 → 거래 계속


def test_classify_same_code_differs_by_market() -> None:
    # 71: 주식 = 서킷2 동시호가종료(재개) / 선물 = 변동성 확대(거래 계속)
    assert classify_jstatus("1", "71") == ("resume", None)
    assert classify_jstatus("5", "71")[0] == "info"


def test_classify_unknown() -> None:
    assert classify_jstatus("1", "99") == ("unknown", None)


# --- 정지 오버레이 (SessionService, §8) — phase와 별개, 시장별 격리 ---


def test_halt_overlay_set_and_clear() -> None:
    svc = SessionService()
    svc.on_market_status(jif("21"))
    assert svc.halt_for("1") is None
    svc.on_market_status(jif("64"))  # 사이드카 매도발동
    assert svc.halt_for("1") == "사이드카매도"
    svc.on_market_status(jif("65"))  # 사이드카 매도해제
    assert svc.halt_for("1") is None


def test_futures_halt_isolated_from_stock() -> None:
    # 결정3 근거: 선물(5) 정지는 주식(1)과 격리 → 자동M(선물)은 주식 사이드카 무영향.
    svc = SessionService()
    svc.on_market_status(jif("63", jangubun="5"))  # 선물 서킷
    assert svc.halt_for("5") == "서킷"
    assert svc.halt_for("1") is None
    svc.on_market_status(jif("64", jangubun="1"))  # 주식 사이드카
    assert svc.halt_for("1") == "사이드카매도"
    assert svc.halt_for("5") == "서킷"  # 선물엔 그대로, 사이드카 안 옮음


# --- 2단계: 시장 라우팅 + is_tradeable (정지-인지 게이트, §8) ---


def test_market_of_instrument() -> None:
    assert market_of_instrument(Instrument.KR_STOCK) == "1"
    assert market_of_instrument(Instrument.KR_STOCK_FUTURE) == "5"


def test_is_tradeable_regular_and_halt() -> None:
    svc = SessionService()
    svc.on_market_status(jif("21"))       # 장시작(주식)
    assert svc.is_tradeable("1") is True
    svc.on_market_status(jif("64"))       # 사이드카 발동
    assert svc.is_tradeable("1") is False  # 정지 → 발주 불가


def test_stock_sidecar_does_not_block_futures() -> None:
    # 결정3: 주식 사이드카 중에도 선물(5)은 발주 가능(공용 phase=REGULAR, 선물 정지 없음).
    svc = SessionService()
    svc.on_market_status(jif("21"))       # 주식 REGULAR
    svc.on_market_status(jif("64"))       # 주식 사이드카
    assert svc.is_tradeable("1") is False
    assert svc.is_tradeable("5") is True


def test_halt_does_not_clobber_phase_and_resumes() -> None:
    # 버그 방지: 정지가 phase를 DEAD로 만들지 않아, 해제되면 바로 다시 거래 가능.
    svc = SessionService()
    svc.on_market_status(jif("21"))
    svc.on_market_status(jif("64"))       # 사이드카 발동
    assert svc.phase_for(SAMSUNG) is SessionPhase.REGULAR  # phase 유지
    svc.on_market_status(jif("65"))       # 사이드카 해제
    assert svc.is_tradeable("1") is True   # DEAD에 안 갇힘


def test_futures_phase_falls_back_to_stock() -> None:
    # 선물 phase 미수신 시 주식 phase 공용(기존 규칙).
    svc = SessionService()
    svc.on_market_status(jif("21"))       # 주식만 수신
    assert svc.phase_for_market("5") is SessionPhase.REGULAR


# --- SessionService: JIF → 세션 맵 ---


def test_regular_jif_yields_regular_session() -> None:
    svc = SessionService()
    svc.on_market_status(jif("21"))  # 장시작
    s = svc.session_for(SAMSUNG)
    assert svc.phase_for(SAMSUNG) is SessionPhase.REGULAR
    t = tradeable_instruments(s)
    assert Instrument.KR_STOCK in t
    assert Instrument.KR_ETF in t
    assert Instrument.KR_STOCK_FUTURE in t
    assert reference_instrument(s) is Instrument.KR_STOCK


def test_preopen_countdown_is_auction_no_reference() -> None:
    svc = SessionService()
    svc.on_market_status(jif("24"))  # 장개시 5분전 (실측)
    s = svc.session_for(SAMSUNG)
    assert s[Instrument.KR_STOCK].tradeable is True
    assert s[Instrument.KR_STOCK].is_auction is True
    assert reference_instrument(s) is None  # 동시호가는 레퍼런스 아님


def test_close_jif_yields_deadzone() -> None:
    svc = SessionService()
    svc.on_market_status(jif("21"))
    svc.on_market_status(jif("41"))  # 장마감 → 데드존
    s = svc.session_for(SAMSUNG)
    assert tradeable_instruments(s) == set()
    assert reference_instrument(s) is None


def test_no_jif_is_deadzone() -> None:
    svc = SessionService()
    assert svc.phase_for(SAMSUNG) is SessionPhase.DEAD
    s = svc.session_for(SAMSUNG)
    assert tradeable_instruments(s) == set()
    assert reference_instrument(s) is None


def test_holiday_overrides_regular() -> None:
    svc = SessionService()
    svc.on_market_status(jif("21"))
    svc.set_holiday(True)
    s = svc.session_for(SAMSUNG)
    assert tradeable_instruments(s) == set()
    assert reference_instrument(s) is None


def test_other_market_does_not_affect_stock_phase() -> None:
    # 다른 시장(예: jangubun "6")의 상태는 주식 시장 phase에 영향 없음.
    svc = SessionService()
    svc.on_market_status(jif("21"))
    svc.on_market_status(MarketStatus(tr_key="0", body={"jangubun": "6", "jstatus": "C3"}))
    assert svc.phase_for(SAMSUNG) is SessionPhase.REGULAR


def test_missing_jangubun_ignored() -> None:
    svc = SessionService()
    svc.on_market_status(MarketStatus(tr_key="0", body={"jstatus": "21"}))
    assert svc.phase_for(SAMSUNG) is SessionPhase.DEAD  # 갱신되지 않음


def test_seed_phase_initializes_but_jif_wins() -> None:
    # 장중 재시작: 운영자 시딩으로 시작하되, JIF 이벤트가 오면 항상 우선.
    svc = SessionService()
    svc.seed_phase(SessionPhase.REGULAR)
    assert svc.phase_for(SAMSUNG) is SessionPhase.REGULAR
    svc.on_market_status(jif("41"))  # 장마감 JIF → 덮어씀
    assert svc.phase_for(SAMSUNG) is SessionPhase.DEAD


def test_seed_phase_does_not_override_received_jif() -> None:
    svc = SessionService()
    svc.on_market_status(jif("21"))
    svc.seed_phase(SessionPhase.DEAD)  # 이미 JIF 수신 → 시딩 무시
    assert svc.phase_for(SAMSUNG) is SessionPhase.REGULAR


def test_sessions_covers_all_underlyings() -> None:
    # 3종 모두 KOSPI 주식 → 시장 phase 공유.
    svc = SessionService()
    svc.on_market_status(jif("21"))
    all_sessions = svc.sessions()
    assert set(all_sessions) == set(Underlying)
    for session_map in all_sessions.values():
        assert reference_instrument(session_map) is Instrument.KR_STOCK
