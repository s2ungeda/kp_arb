"""RiskManager — 전략 비종속 리스크 골격 (DESIGN.md §5.6, §8). 순수 로직.

전략 비종속 가드만 고정한다(전략 의존 임계값은 config로 주입):
1. kill-switch: 걸리면 모든 신규 진입 거부.
2. 레퍼런스 가용성: live 레퍼런스 없으면(데드존) 신규 진입 거부.
3. HL 마진비율: 하한 미만이거나 미지(None)면 HL 주문 거부(보수적).
4. 계좌별 자금/증거금 버퍼: 주문 후 버퍼 미만이면 거부.

엔진 결선(strategy → RiskManager → 라우팅)은 통합 블록에서. 여기선 판정만.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from .domain.enums import Account, Underlying, Venue
from .domain.models import OrderIntent


def _default_cost(intent: OrderIntent) -> float:
    """주문 소요자금 추정(기본). 지정가 명목 = qty * price. 시장가(가격 없음)는 0."""
    return intent.qty * (intent.price or 0.0)


@dataclass
class RiskLimits:
    """전략 의존 임계값(config 주입)."""

    hl_margin_floor: float = 0.0
    account_buffer: Mapping[Account, float] = field(default_factory=dict)
    cost_fn: Callable[[OrderIntent], float] = _default_cost


@dataclass
class RiskState:
    """리스크 판단에 필요한 1급 상태(엔진/게이트웨이가 채움)."""

    reference_available: Mapping[Underlying, bool] = field(default_factory=dict)
    account_available_funds: Mapping[Account, float] = field(default_factory=dict)
    hl_margin_ratio: float | None = None
    kill_switch: bool = False


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None


class RiskManager:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self._limits = limits or RiskLimits()

    def check(self, intent: OrderIntent, state: RiskState) -> RiskDecision:
        if state.kill_switch:
            return RiskDecision(False, "kill-switch engaged")

        if not state.reference_available.get(intent.underlying, False):
            return RiskDecision(False, "no live reference (deadzone) — new entry blocked")

        if intent.venue is Venue.HYPERLIQUID:
            ratio = state.hl_margin_ratio
            if ratio is None or ratio < self._limits.hl_margin_floor:
                return RiskDecision(False, "HL margin ratio below floor")

        if intent.account is not None:
            cost = self._limits.cost_fn(intent)
            available = state.account_available_funds.get(intent.account, 0.0)
            buffer = self._limits.account_buffer.get(intent.account, 0.0)
            if available - cost < buffer:
                return RiskDecision(False, f"account {intent.account.value} buffer breach")

        return RiskDecision(True)

    def allow(self, intent: OrderIntent, state: RiskState) -> bool:
        return self.check(intent, state).allowed

    def filter(self, intents: Iterable[OrderIntent], state: RiskState) -> list[OrderIntent]:
        """통과한 주문만 남긴다(엔진이 라우팅 전에 호출)."""
        return [intent for intent in intents if self.allow(intent, state)]
