"""SessionService (DESIGN.md §5.3).

LS 장운영데이터(JIF 실시간 + 휴장일)를 소비해 underlying별 instrument 상태 맵을 산출한다.
- JIF body → SessionPhase 매핑(순수 함수 ``phase_from_jif``)만 새로 정의.
- 맵 산출은 기존 ``session.build_session``을 **그대로 재사용**(수정 없음).
- 미지 코드/JIF 미수신/휴장일은 보수적으로 데드존(신규 진입 금지) 처리.

정확한 JIF 상태 코드/필드명은 라이브 구현 시 확인(여기선 녹화 프레임 기준 placeholder).
``on_market_status``는 ``LSWebSocketClient.on_market_status`` 콜백에 바로 연결할 수 있다.
"""
from __future__ import annotations

from typing import Any

from .domain.enums import Instrument, SessionPhase, Underlying
from .domain.models import InstrumentStatus
from .gateways.ls_ws import MarketStatus
from .session import build_session

# JIF 장운영 상태 코드 → SessionPhase (라이브 구현 시 실제 코드 확인).
_JIF_PHASE: dict[str, SessionPhase] = {
    "10": SessionPhase.PRE_OPEN,     # 장개시전 동시호가
    "20": SessionPhase.REGULAR,      # 정규장
    "30": SessionPhase.NXT,          # 시간외/NXT
    "40": SessionPhase.NIGHT_DERIV,  # 파생 야간
    "90": SessionPhase.DEAD,         # 장마감
}

JIF_PHASE_FIELD = "jang_cd"


def phase_from_jif(body: dict[str, Any], *, field: str = JIF_PHASE_FIELD) -> SessionPhase:
    """JIF body의 상태 코드를 SessionPhase로 매핑. 미지/누락은 보수적으로 DEAD."""
    code = str(body.get(field, ""))
    return _JIF_PHASE.get(code, SessionPhase.DEAD)


class SessionService:
    """underlying별 최신 SessionPhase를 보존하고 instrument 상태 맵을 산출."""

    DEFAULT_PHASE = SessionPhase.DEAD  # JIF 미수신 시 보수적 기본값

    def __init__(self) -> None:
        self._phase: dict[Underlying, SessionPhase] = {}
        self._is_holiday = False

    def set_holiday(self, is_holiday: bool) -> None:
        """휴장일 조회 결과를 반영. 휴장이면 모든 instrument 비거래(데드존)."""
        self._is_holiday = is_holiday

    def on_market_status(self, status: MarketStatus) -> None:
        """LS JIF 이벤트 수신 → 해당 underlying의 phase 갱신. 미지 종목은 무시."""
        underlying = Underlying.from_krx_code(status.tr_key)
        if underlying is None:
            return
        self._phase[underlying] = phase_from_jif(status.body)

    def phase_for(self, underlying: Underlying) -> SessionPhase:
        return self._phase.get(underlying, self.DEFAULT_PHASE)

    def session_for(self, underlying: Underlying) -> dict[Instrument, InstrumentStatus]:
        return build_session(self.phase_for(underlying), is_holiday=self._is_holiday)

    def sessions(self) -> dict[Underlying, dict[Instrument, InstrumentStatus]]:
        return {u: self.session_for(u) for u in Underlying}
