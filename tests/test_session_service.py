"""SessionService 계약 테스트. 실측 JIF 프레임(시장 단위) → 세션 맵 검증.

[정합 v6.3] JIF는 시장 단위(tr_key="0", body={jangubun, jstatus})임이 라이브 실측으로
확인되어, 종전의 '종목 단위(jang_cd)' 가정 테스트를 실제 계약으로 정정했다.
"""
from kp_arb.domain.enums import Instrument, SessionPhase, Underlying
from kp_arb.gateways.ls_ws import MarketStatus
from kp_arb.session import reference_instrument, tradeable_instruments
from kp_arb.session_service import SessionService, phase_from_jif

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
