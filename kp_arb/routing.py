"""instrument -> LS 계좌 라우팅 (DESIGN.md §3, §5.1). 순수 로직."""
from __future__ import annotations

from .domain.enums import Account, Instrument

_KR_ROUTING: dict[Instrument, Account] = {
    Instrument.KR_STOCK: Account.KR_STOCK,
    Instrument.KR_ETF: Account.KR_STOCK,
    Instrument.KR_STOCK_FUTURE: Account.KR_DERIV,
}


def account_for(instrument: Instrument) -> Account:
    """국내 instrument를 대상 LS 계좌로 매핑. HL_PERP은 LS 계좌가 없어 ValueError."""
    try:
        return _KR_ROUTING[instrument]
    except KeyError as exc:
        raise ValueError(f"{instrument} is not a domestic instrument (no LS account)") from exc
