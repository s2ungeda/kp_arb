"""세션 모델 (DESIGN.md §7). 장운영 단계 -> instrument별 상태 맵. 순수 로직.

실제 phase/시간은 LS 장운영데이터(JIF + 휴장일)에서 산출한다. 여기서는 그 결과인
phase를 입력으로 받아 instrument 상태를 결정론적으로 만든다.
"""
from __future__ import annotations

from .domain.enums import Instrument, SessionPhase
from .domain.models import InstrumentStatus

_KR_INSTRUMENTS: tuple[Instrument, ...] = (
    Instrument.KR_STOCK,
    Instrument.KR_ETF,
    Instrument.KR_STOCK_FUTURE,
)


def build_session(
    phase: SessionPhase, *, is_holiday: bool = False
) -> dict[Instrument, InstrumentStatus]:
    status: dict[Instrument, InstrumentStatus] = {
        i: InstrumentStatus(instrument=i) for i in _KR_INSTRUMENTS
    }
    if is_holiday or phase is SessionPhase.DEAD:
        return status

    if phase is SessionPhase.REGULAR:
        for i in (Instrument.KR_ETF, Instrument.KR_STOCK_FUTURE):
            status[i] = InstrumentStatus(instrument=i, tradeable=True)
        # 정규장 기본 레퍼런스 = 주식 (주식 vs 주식선물은 [OPEN] DESIGN.md §13)
        status[Instrument.KR_STOCK] = InstrumentStatus(
            instrument=Instrument.KR_STOCK, tradeable=True, is_reference=True
        )
    elif phase is SessionPhase.AFTER_MARKET:
        # 애프터마켓 ~20:00 (2026-09-14~): 주식·주식선물 연장 거래. 레퍼런스 = 주식.
        status[Instrument.KR_STOCK_FUTURE] = InstrumentStatus(
            instrument=Instrument.KR_STOCK_FUTURE, tradeable=True
        )
        status[Instrument.KR_STOCK] = InstrumentStatus(
            instrument=Instrument.KR_STOCK, tradeable=True, is_reference=True
        )
    elif phase in (SessionPhase.PRE_OPEN, SessionPhase.NXT):
        # 동시호가/시간외: 거래 가능 표시하되 레퍼런스로는 쓰지 않음(보수적)
        status[Instrument.KR_STOCK] = InstrumentStatus(
            instrument=Instrument.KR_STOCK,
            tradeable=True,
            is_auction=phase is SessionPhase.PRE_OPEN,
        )
    return status


def tradeable_instruments(session: dict[Instrument, InstrumentStatus]) -> set[Instrument]:
    return {i for i, s in session.items() if s.tradeable}


def reference_instrument(session: dict[Instrument, InstrumentStatus]) -> Instrument | None:
    for i, s in session.items():
        if s.is_reference:
            return i
    return None
