"""SessionService 계약 테스트. 녹화 JIF 프레임/휴장일 입력 → 세션 맵 검증."""
from kp_arb.domain.enums import Instrument, SessionPhase, Underlying
from kp_arb.gateways.ls_ws import MarketStatus
from kp_arb.session import reference_instrument, tradeable_instruments
from kp_arb.session_service import SessionService, phase_from_jif

SAMSUNG = Underlying.SAMSUNG
SAMSUNG_CODE = Underlying.SAMSUNG.krx_code
HYNIX = Underlying.SK_HYNIX
HYNIX_CODE = Underlying.SK_HYNIX.krx_code


def jif(code: str, jang_cd: str) -> MarketStatus:
    """녹화 JIF 프레임(파싱된 MarketStatus)."""
    return MarketStatus(tr_key=code, body={"jang_cd": jang_cd})


# --- JIF 코드 → phase 매핑 (순수 함수) ---


def test_phase_from_jif_mapping() -> None:
    assert phase_from_jif({"jang_cd": "10"}) is SessionPhase.PRE_OPEN
    assert phase_from_jif({"jang_cd": "20"}) is SessionPhase.REGULAR
    assert phase_from_jif({"jang_cd": "40"}) is SessionPhase.AFTER_MARKET
    assert phase_from_jif({"jang_cd": "90"}) is SessionPhase.DEAD


def test_phase_from_jif_unknown_is_dead() -> None:
    assert phase_from_jif({"jang_cd": "99"}) is SessionPhase.DEAD
    assert phase_from_jif({}) is SessionPhase.DEAD  # 누락도 보수적으로 DEAD


# --- SessionService: JIF → 세션 맵 ---


def test_regular_jif_yields_regular_session() -> None:
    svc = SessionService()
    svc.on_market_status(jif(SAMSUNG_CODE, "20"))
    s = svc.session_for(SAMSUNG)
    assert svc.phase_for(SAMSUNG) is SessionPhase.REGULAR
    t = tradeable_instruments(s)
    assert Instrument.KR_STOCK in t
    assert Instrument.KR_ETF in t
    assert Instrument.KR_STOCK_FUTURE in t
    assert reference_instrument(s) is Instrument.KR_STOCK


def test_after_market_jif_yields_after_market_session() -> None:
    svc = SessionService()
    svc.on_market_status(jif(SAMSUNG_CODE, "40"))
    s = svc.session_for(SAMSUNG)
    assert tradeable_instruments(s) == {Instrument.KR_STOCK, Instrument.KR_STOCK_FUTURE}
    assert reference_instrument(s) is Instrument.KR_STOCK


def test_preopen_is_auction_no_reference() -> None:
    svc = SessionService()
    svc.on_market_status(jif(SAMSUNG_CODE, "10"))
    s = svc.session_for(SAMSUNG)
    assert s[Instrument.KR_STOCK].tradeable is True
    assert s[Instrument.KR_STOCK].is_auction is True
    assert reference_instrument(s) is None  # 동시호가는 레퍼런스 아님


def test_no_jif_is_deadzone() -> None:
    svc = SessionService()
    assert svc.phase_for(SAMSUNG) is SessionPhase.DEAD
    s = svc.session_for(SAMSUNG)
    assert tradeable_instruments(s) == set()
    assert reference_instrument(s) is None


def test_holiday_overrides_regular() -> None:
    svc = SessionService()
    svc.on_market_status(jif(SAMSUNG_CODE, "20"))
    svc.set_holiday(True)
    s = svc.session_for(SAMSUNG)
    assert tradeable_instruments(s) == set()
    assert reference_instrument(s) is None


def test_unknown_issue_code_ignored() -> None:
    svc = SessionService()
    svc.on_market_status(MarketStatus(tr_key="999999", body={"jang_cd": "20"}))
    assert svc.phase_for(SAMSUNG) is SessionPhase.DEAD  # 갱신되지 않음


def test_per_underlying_independent() -> None:
    svc = SessionService()
    svc.on_market_status(jif(SAMSUNG_CODE, "20"))  # 삼성 정규장
    svc.on_market_status(jif(HYNIX_CODE, "40"))    # 하이닉스 애프터마켓
    # 정규장은 ETF 거래 가능, 애프터마켓은 불가 → 종목별 독립 산출 확인.
    assert Instrument.KR_ETF in tradeable_instruments(svc.session_for(SAMSUNG))
    assert Instrument.KR_ETF not in tradeable_instruments(svc.session_for(HYNIX))
    assert reference_instrument(svc.session_for(HYNIX)) is Instrument.KR_STOCK


def test_sessions_covers_all_underlyings() -> None:
    svc = SessionService()
    svc.on_market_status(jif(SAMSUNG_CODE, "20"))
    all_sessions = svc.sessions()
    assert set(all_sessions) == set(Underlying)
    # JIF 미수신 종목은 데드존
    assert tradeable_instruments(all_sessions[Underlying.HYUNDAI]) == set()
