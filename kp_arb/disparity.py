"""상대호가 괴리(disp)·진입/청산 스프레드 — 순수 로직 (DESIGN §6.1, 원본 meme.xlsx).

- disp = (환산가 − 기준가) ÷ 기준가. 매도호가/매수호가 각각.
- 방향 A(국내 롱 + HL 숏), **국내 다리는 maker**(같은 방향 호가에 걸어 체결 대기 —
  자동2 모드와 일치) 기준 (엑셀 개정판 메인!L12/L14, 2026-07-13):
    진입(en) = HL 매수호가disp − 국내 매수호가disp   (국내 bid에 걸어 매수, HL bid에 매도)
    청산(ex) = HL 매도호가disp − 국내 매도호가disp   (국내 ask에 걸어 매도, HL ask에 환매수)
  방향 B(국내 숏[선물만] + HL 롱)는 부호 반대(진입_B = −청산_A, 청산_B = −진입_A).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def est_price(levels: Sequence[tuple[float, float]], qty: float) -> float | None:
    """estprice — 주문수량을 다 받아줄 수 있는 첫 호가 (DESIGN §6.2-3).

    levels는 **상대편** 호가 사다리 [(가격, 잔량), ...] 최우선부터 —
    매수 주문이면 매도호가, 매도 주문이면 매수호가를 넣는다.
    누적 잔량 ≥ 주문수량이 되는 호가의 가격. 사다리 전체로도 부족하면 None
    (신호·주문 금지). 진입/청산 공식의 매수가/매도가 자리에 이 값을 쓴다.
    """
    if qty <= 0:
        return None
    cumulative = 0.0
    for price, size in levels:
        cumulative += size
        if cumulative >= qty:
            return price
    return None


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
    """HL vs 국내 상대(주식선물) 한 쌍의 진입/청산 스프레드 (방향 A, 국내 maker 기준)."""

    entry: float | None  # HL bid disp − 국내 bid disp (벌어질수록 진입 매력) — 메인!L12
    exit: float | None   # HL ask disp − 국내 ask disp (좁혀지면/음수면 청산) — 메인!L14


def pair_spread(hl: SideDisp, kr: SideDisp) -> PairSpread:
    entry = hl.bid - kr.bid if hl.bid is not None and kr.bid is not None else None
    exit_ = hl.ask - kr.ask if hl.ask is not None and kr.ask is not None else None
    return PairSpread(entry=entry, exit=exit_)


def net_entry(spread: PairSpread, fee_rate: float) -> float | None:
    """순진입 = 진입값 − (청산−진입)/2 − 왕복수수료.

    "지금 진입해서 괴리가 0으로 완전 수렴했을 때 남는 기대 %".
    유도: 실현수익 = 진입값(t0) − 청산값(수렴 시). 중간가 괴리가 0으로 수렴하면
    청산값 ≈ (청산−진입)/2 (같은 시점 청산−진입 = HL호가폭 − 국내호가폭 —
    국내 다리는 maker라 국내 폭은 벌고 HL 폭은 낸다. 호가폭 유지 가정).
    """
    if spread.entry is None or spread.exit is None:
        return None
    half_width = (spread.exit - spread.entry) / 2.0
    return spread.entry - half_width - fee_rate


def net_exit(spread: PairSpread) -> float | None:
    """순청산 = (진입+청산)/2 — 중간가 기준 괴리(호가폭 효과 제거).

    포지션 보유 중 **≤ 0이면 수렴 완료**로 보고 청산. 수수료는 진입 판단(순진입)에서
    이미 차감했으므로 여기선 빼지 않는다.
    """
    if spread.entry is None or spread.exit is None:
        return None
    return (spread.entry + spread.exit) / 2.0


@dataclass(frozen=True)
class PairBoard:
    """모니터·기록용 한 쌍(HL vs 국내 상대)의 괴리 전체."""

    hl: SideDisp
    kr: SideDisp
    spread: PairSpread
    hl_last: float | None = None  # HL 현재가(체결) 괴리 — 엑셀 시세!AD7(메인 I22)
    kr_last: float | None = None  # 국내 상대 현재가 괴리 — 엑셀 시세!AD61/AD89(메인 K19/M19)
    net_entry: float | None = None  # 순진입 (완전 수렴 가정 기대 수익, 수수료 차감)
    net_exit: float | None = None   # 순청산 (≤0이면 수렴 완료 — 청산 신호)
