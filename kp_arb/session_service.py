"""SessionService (DESIGN.md §5.3).

LS 장운영데이터(JIF 실시간 + 휴장일)를 소비해 underlying별 instrument 상태 맵을 산출한다.
- 맵 산출은 기존 ``session.build_session``을 **그대로 재사용**(수정 없음).
- 미지 코드/JIF 미수신/휴장일은 보수적으로 데드존(신규 진입 금지) 처리.

[라이브 정합 v6.3] JIF는 **시장 단위** 이벤트다(tr_key="0" 구독, 실측):
``body = {jangubun(시장구분), jstatus(상태코드)}``.
- 실측: 개장 카운트다운 jstatus "24"→"23"→"22" (jangubun "1"=주식) — xingAPI 유래 코드표와 부합.
- 매핑은 **실측/문서로 확인된 코드만** 채우고 미지 코드는 DEAD(보수).
- 파생(선물) 시장 jangubun은 미실측 — 확인 전까지 주식 시장 phase를 공용 적용.
"""
from __future__ import annotations

from typing import Any

from .domain.enums import Instrument, SessionPhase, Underlying
from .domain.models import InstrumentStatus
from .gateways.ls_ws import MarketStatus
from .session import build_session

# 시장구분(jangubun). 실측: "1"=주식(KOSPI). "5"=선물/옵션(자동M 대상, JIF 코드표).
STOCK_MARKET = "1"
FUTURES_MARKET = "5"

# jstatus → SessionPhase. 실측(개장 카운트다운) + xingAPI 코드표 부합분만. 미지는 DEAD.
_JSTATUS_PHASE: dict[str, SessionPhase] = {
    "11": SessionPhase.PRE_OPEN,  # 장전동시호가 개시
    "22": SessionPhase.PRE_OPEN,  # 장개시 10초전 (실측)
    "23": SessionPhase.PRE_OPEN,  # 장개시 1분전 (실측)
    "24": SessionPhase.PRE_OPEN,  # 장개시 5분전 (실측)
    "25": SessionPhase.PRE_OPEN,  # 장개시 10분전
    "21": SessionPhase.REGULAR,   # 장시작
    "41": SessionPhase.DEAD,      # 장마감
}

# 정지 발동/해제 코드 — jangubun별로 같은 번호도 뜻이 다르다(JIF 코드표, DESIGN-auto-m-exec §8).
# 주식(1,2): 값=정지 사유. 62/70(해제,호가접수개시)은 회복 동시호가라 접속매매 불가 → 정지 유지,
#            63/71(동시호가종료)에서 접속 재개.
_STOCK_HALT: dict[str, str] = {
    "61": "서킷1", "62": "서킷1", "68": "서킷2", "70": "서킷2",
    "64": "사이드카매도", "66": "사이드카매수",
}
_STOCK_RESUME = {"63", "65", "67", "71"}  # 동시호가종료·사이드카 해제 → 재개
# 선물/옵션(5): 63 서킷(장중동시마감)=정지, 62 해제=재개, 70~77 변동성 확대=거래 계속(정보).
_FUT_HALT: dict[str, str] = {"63": "서킷"}
_FUT_RESUME = {"62"}
_FUT_INFO = {"70", "71", "72", "73", "74", "75", "76", "77"}

# instrument → JIF 시장구분(jangubun). 자동T=주식(1) / 자동M=주식선물(5).
_INSTRUMENT_MARKET: dict[Instrument, str] = {
    Instrument.KR_STOCK: STOCK_MARKET,
    Instrument.KR_ETF: STOCK_MARKET,
    Instrument.KR_STOCK_FUTURE: FUTURES_MARKET,
    Instrument.KR_FX_FUTURE: FUTURES_MARKET,
}


def market_of_instrument(instrument: Instrument) -> str:
    """instrument → JIF 시장구분(jangubun). 미지는 보수적으로 주식(1)."""
    return _INSTRUMENT_MARKET.get(instrument, STOCK_MARKET)


def phase_from_jif(body: dict[str, Any]) -> SessionPhase:
    """JIF body(jstatus)를 SessionPhase로 매핑. 미지/누락은 보수적으로 DEAD."""
    code = str(body.get("jstatus", ""))
    return _JSTATUS_PHASE.get(code, SessionPhase.DEAD)


def classify_jstatus(jangubun: str, jstatus: str) -> tuple[str, SessionPhase | str | None]:
    """JIF 코드 분류 (DESIGN-auto-m-exec §8). **jangubun별로 61~77 의미가 다르다.**

    반환:
    - ``("phase", SessionPhase)`` — 시간대(장전/장시작/장마감/당일종료 등)
    - ``("halt", 사유)`` — 정지 발동(서킷·사이드카). 사유는 표시용 문자열
    - ``("resume", None)`` — 정지 해제(접속 재개)
    - ``("info", 사유)`` — 선물 변동성 확대(70~77): 거래 계속, 정지 아님
    - ``("unknown", None)`` — 미분류(보수적으로 상위에서 DEAD 처리)
    """
    g, j = str(jangubun), str(jstatus)
    if j in _JSTATUS_PHASE:  # 시간대 코드(공통)
        return ("phase", _JSTATUS_PHASE[j])
    if (g in ("1", "2") and j == "69") or (g == FUTURES_MARKET and j == "61"):
        return ("phase", SessionPhase.DEAD)  # 당일 장종료(서킷3단계 / 선물 당일종료)
    if g in ("1", "2"):
        if j in _STOCK_HALT:
            return ("halt", _STOCK_HALT[j])
        if j in _STOCK_RESUME:
            return ("resume", None)
    elif g == FUTURES_MARKET:
        if j in _FUT_HALT:
            return ("halt", _FUT_HALT[j])
        if j in _FUT_RESUME:
            return ("resume", None)
        if j in _FUT_INFO:
            return ("info", "변동성확대")
    return ("unknown", None)


class SessionService:
    """시장(jangubun)별 최신 SessionPhase를 보존하고 instrument 상태 맵을 산출."""

    DEFAULT_PHASE = SessionPhase.DEAD  # JIF 미수신 시 보수적 기본값

    def __init__(self) -> None:
        self._market_phase: dict[str, SessionPhase] = {}
        self._market_halt: dict[str, str] = {}  # 시장별 정지 사유(§8 오버레이)
        self._is_holiday = False

    def set_holiday(self, is_holiday: bool) -> None:
        """휴장일 조회 결과를 반영. 휴장이면 모든 instrument 비거래(데드존)."""
        self._is_holiday = is_holiday

    def seed_phase(self, phase: SessionPhase, *, market: str = STOCK_MARKET) -> None:
        """시작 시 초기 phase 시딩(운영자 명시 입력 — 장중 재시작용).

        LS REST에는 '현재 장상태' 조회 TR이 없어(JIF는 변화 push만) 장중 재시작 시
        운영자가 KP_SESSION_INIT으로 명시한다. **이미 JIF로 수신한 상태는 덮지 않으며**,
        이후 JIF 이벤트가 오면 항상 그것이 우선한다.
        """
        self._market_phase.setdefault(market, phase)

    def on_market_status(self, status: MarketStatus) -> None:
        """LS JIF 이벤트 수신 → 시장(jangubun) phase / 정지 오버레이 갱신 (§8).

        분류(``classify_jstatus``)로 갈라 처리한다:
        - 시간대(phase) → 해당 시장 phase 갱신
        - 정지 발동/해제 → **정지 오버레이만** 갱신(phase는 건드리지 않음). 이래야
          정지(사이드카·서킷)가 풀린 뒤 DEAD에 갇히지 않는다(정지 코드로 phase를
          DEAD로 만들면 해제돼도 REGULAR로 못 돌아옴 — 그 버그를 여기서 막는다).
        - 변동성(info) → 거래 계속, 무시 / 미지(unknown) → 보수적으로 DEAD
        """
        market = str(status.body.get("jangubun", ""))
        if not market:
            return
        kind, reason = classify_jstatus(market, str(status.body.get("jstatus", "")))
        if kind == "phase" and isinstance(reason, SessionPhase):
            self._market_phase[market] = reason
        elif kind == "halt" and isinstance(reason, str):
            self._market_halt[market] = reason
        elif kind == "resume":
            self._market_halt.pop(market, None)
        elif kind == "unknown":
            self._market_phase[market] = SessionPhase.DEAD

    def halt_for(self, market: str = STOCK_MARKET) -> str | None:
        """해당 시장의 현재 정지 사유(없으면 None). §8 정지 오버레이."""
        return self._market_halt.get(market)

    def phase_for_market(self, market: str) -> SessionPhase:
        """시장(jangubun)별 phase. 파생 시장 미수신 시 주식 시장 phase 공용(기존 규칙)."""
        if market in self._market_phase:
            return self._market_phase[market]
        return self._market_phase.get(STOCK_MARKET, self.DEFAULT_PHASE)

    def is_tradeable(self, market: str) -> bool:
        """해당 시장에 지금 발주 가능한가 — phase가 데드 아님 AND 정지 없음 (§8).

        시장별로 본다: 주식(1) 사이드카는 선물(5) 발주를 막지 않는다(결정3).
        """
        return (self.phase_for_market(market) is not SessionPhase.DEAD
                and self._market_halt.get(market) is None)

    def phase_for(self, underlying: Underlying) -> SessionPhase:
        # 3종 모두 KOSPI 주식 — 주식 시장 phase 적용. (파생 시장 분리는 실측 후.)
        return self._market_phase.get(STOCK_MARKET, self.DEFAULT_PHASE)

    def session_for(self, underlying: Underlying) -> dict[Instrument, InstrumentStatus]:
        return build_session(self.phase_for(underlying), is_holiday=self._is_holiday)

    def sessions(self) -> dict[Underlying, dict[Instrument, InstrumentStatus]]:
        return {u: self.session_for(u) for u in Underlying}
