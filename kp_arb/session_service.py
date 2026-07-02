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

# 시장구분(jangubun). 실측: "1"=주식(KOSPI). 파생 시장 코드는 실측 후 추가.
STOCK_MARKET = "1"

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


def phase_from_jif(body: dict[str, Any]) -> SessionPhase:
    """JIF body(jstatus)를 SessionPhase로 매핑. 미지/누락은 보수적으로 DEAD."""
    code = str(body.get("jstatus", ""))
    return _JSTATUS_PHASE.get(code, SessionPhase.DEAD)


class SessionService:
    """시장(jangubun)별 최신 SessionPhase를 보존하고 instrument 상태 맵을 산출."""

    DEFAULT_PHASE = SessionPhase.DEAD  # JIF 미수신 시 보수적 기본값

    def __init__(self) -> None:
        self._market_phase: dict[str, SessionPhase] = {}
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
        """LS JIF 이벤트 수신 → 해당 시장(jangubun)의 phase 갱신."""
        market = str(status.body.get("jangubun", ""))
        if not market:
            return
        self._market_phase[market] = phase_from_jif(status.body)

    def phase_for(self, underlying: Underlying) -> SessionPhase:
        # 3종 모두 KOSPI 주식 — 주식 시장 phase 적용. (파생 시장 분리는 실측 후.)
        return self._market_phase.get(STOCK_MARKET, self.DEFAULT_PHASE)

    def session_for(self, underlying: Underlying) -> dict[Instrument, InstrumentStatus]:
        return build_session(self.phase_for(underlying), is_holiday=self._is_holiday)

    def sessions(self) -> dict[Underlying, dict[Instrument, InstrumentStatus]]:
        return {u: self.session_for(u) for u in Underlying}
