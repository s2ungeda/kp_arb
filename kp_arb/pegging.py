"""호가 추적(페깅) 주문 판단 — 순수 로직 (주문 테스트 도구용).

선택한 호가 단계(예: 매수 2호가)에 지정가를 걸어 두고, 호가가 움직이면
**정정**으로 따라 옮긴다. LS는 정정 TR(CSPAT00701 등), HL도 modify 액션이
있어 두 거래소 모두 요청 한 번으로 처리된다.

이 모듈은 "지금 무엇을 해야 하는가"만 판단한다(주문 실행은 창/시스템 몫).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .domain.enums import Side
from .domain.models import Quote


class PegAction(StrEnum):
    NONE = "none"    # 그대로 둠 (목표가와 일치)
    PLACE = "place"  # 신규 주문
    AMEND = "amend"  # 정정 (LS·HL 공통)
    WAIT = "wait"    # 목표가를 알 수 없음 (호가 미수신 등)


@dataclass(frozen=True)
class PegDecision:
    action: PegAction
    price: float | None = None  # PLACE/AMEND/CANCEL_PLACE일 때 새 주문가


def target_price(quote: Quote | None, side: Side, level: int) -> float | None:
    """호가 N단계 가격. 매수면 매수호가 쪽, 매도면 매도호가 쪽. 없으면 None.

    다단계(bids/asks)가 있으면 그걸 쓰고, 없으면 1호가(bid/ask)만 지원.
    """
    if quote is None or level < 1:
        return None
    depth = quote.bids if side is Side.BUY else quote.asks
    if depth:
        return depth[level - 1][0] if level <= len(depth) else None
    if level == 1:
        return quote.bid if side is Side.BUY else quote.ask
    return None  # 다단계 정보 없음 (예: HL bbo)


def decide(
    *,
    current_price: float | None,
    target: float | None,
) -> PegDecision:
    """현재 걸려 있는 주문가와 목표가를 비교해 할 일을 결정.

    current_price=None 은 '걸린 주문 없음'을 뜻한다.
    """
    if target is None:
        return PegDecision(PegAction.WAIT)
    if current_price is None:
        return PegDecision(PegAction.PLACE, target)
    if current_price == target:
        return PegDecision(PegAction.NONE)
    return PegDecision(PegAction.AMEND, target)
