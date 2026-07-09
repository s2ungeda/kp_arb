"""상대호가 괴리(disp)·진입/청산 스프레드 — 순수 로직 (DESIGN §6.1, 원본 IM.xlsx).

- disp = (환산가 − 기준가) ÷ 기준가. 매도호가/매수호가 각각.
- 방향 A(국내 롱 + HL 숏) 기준:
    진입 = HL 매수호가disp − 국내 매도호가disp   (HL bid에 팔고 국내 ask에 사는 taker 조합)
    청산 = HL 매도호가disp − 국내 매수호가disp
  방향 B(국내 숏[선물만] + HL 롱)는 부호 반대(진입_B = −청산_A, 청산_B = −진입_A).
"""
from __future__ import annotations

from dataclasses import dataclass


def disp(price: float | None, base: float | None) -> float | None:
    """기준가 대비 괴리 비율. 입력이 없거나 기준가가 0 이하이면 None."""
    if price is None or base is None or base <= 0 or price <= 0:
        return None
    return (price - base) / base


@dataclass(frozen=True)
class SideDisp:
    """한 상품의 매도/매수호가 괴리."""

    ask: float | None  # 매도호가 괴리
    bid: float | None  # 매수호가 괴리


def side_disp(
    ask_price: float | None, bid_price: float | None, base: float | None
) -> SideDisp:
    return SideDisp(ask=disp(ask_price, base), bid=disp(bid_price, base))


@dataclass(frozen=True)
class PairSpread:
    """HL vs 국내 상대(선물/ETF) 한 쌍의 진입/청산 스프레드 (방향 A 기준)."""

    entry: float | None  # HL bid disp − 국내 ask disp (벌어질수록 진입 매력)
    exit: float | None   # HL ask disp − 국내 bid disp (좁혀지면/음수면 청산)


def pair_spread(hl: SideDisp, kr: SideDisp) -> PairSpread:
    entry = hl.bid - kr.ask if hl.bid is not None and kr.ask is not None else None
    exit_ = hl.ask - kr.bid if hl.ask is not None and kr.bid is not None else None
    return PairSpread(entry=entry, exit=exit_)


def net_entry(spread: PairSpread, fee_rate: float) -> float | None:
    """순진입 = 진입값 − 왕복호가비용/2 − 왕복수수료.

    "지금 진입해서 괴리가 0으로 완전 수렴했을 때 남는 기대 %".
    유도: 실현수익 = 진입값(t0) − 청산값(수렴 시). 수렴 시 청산값 ≈ 호가폭합/2
    (호가폭이 유지된다고 가정)이고, 호가폭합 = 청산−진입(같은 시점)이므로
    순진입 = 진입 − (청산−진입)/2 − 수수료.
    """
    if spread.entry is None or spread.exit is None:
        return None
    half_width = (spread.exit - spread.entry) / 2.0
    return spread.entry - half_width - fee_rate


@dataclass(frozen=True)
class PairBoard:
    """모니터·기록용 한 쌍(HL vs 국내 상대)의 괴리 전체."""

    hl: SideDisp
    kr: SideDisp
    spread: PairSpread
    hl_last: float | None = None  # HL 현재가(체결) 괴리 — 엑셀 시세!AD7(메인 I22)
    kr_last: float | None = None  # 국내 상대 현재가 괴리 — 엑셀 시세!AD61/AD89(메인 K19/M19)
    net_entry: float | None = None  # 순진입 (완전 수렴 가정 기대 수익, 수수료 차감)
