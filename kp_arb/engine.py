"""ArbEngine — 전략 비종속 오케스트레이터 (DESIGN.md §5.5).

흐름: MarketState 조립 → Strategy.evaluate() → (RiskManager 검증: 블록 4-3)
→ 게이트웨이·계좌 라우팅.
엔진은 **결정 로직을 갖지 않는다**(전략 플러그인이 담당). 시세는 게이트웨이 콜백으로
갱신되는 스냅샷(MarketData)에서 읽는다. InstrumentSelector(4-2)·RiskManager(4-3)는 이후 주입.
"""
from __future__ import annotations

from collections.abc import Sequence

from .domain.enums import Account, Underlying, Venue
from .domain.models import MarketState, OrderIntent, Position, Quote
from .gateways.base import HLGateway, LSGateway
from .gateways.hl import Mark
from .risk import RiskManager, RiskState
from .session import reference_instrument
from .session_service import SessionService
from .strategy.base import Strategy


class MarketData:
    """엔진이 읽는 최신 시세 스냅샷. 게이트웨이 스트림 콜백이 갱신한다."""

    def __init__(self) -> None:
        self.reference_price_krw: dict[Underlying, float] = {}
        self.hl_mark_usd: dict[Underlying, float] = {}
        self.usdkrw: float | None = None


class ArbEngine:
    def __init__(
        self,
        *,
        session: SessionService,
        strategy: Strategy,
        ls: LSGateway,
        hl: HLGateway,
        risk: RiskManager | None = None,
    ) -> None:
        self._session = session
        self._strategy = strategy
        self._ls = ls
        self._hl = hl
        self._risk = risk
        self.market = MarketData()
        self.risk_state = RiskState()  # 리스크 판단 상태(러너/게이트웨이가 갱신)

    # --- 시세 스냅샷 갱신(게이트웨이 콜백 연결용) ---

    def on_quote(self, quote: Quote) -> None:
        self.market.reference_price_krw[quote.underlying] = quote.mid

    def on_mark(self, mark: Mark) -> None:
        self.market.hl_mark_usd[mark.underlying] = mark.price

    def set_fx(self, usdkrw: float) -> None:
        self.market.usdkrw = usdkrw

    # --- MarketState 조립 ---

    def build_market_state(
        self, underlying: Underlying, positions: Sequence[Position]
    ) -> MarketState:
        session_map = self._session.session_for(underlying)
        return MarketState(
            underlying=underlying,
            reference_instrument=reference_instrument(session_map),
            reference_price_krw=self.market.reference_price_krw.get(underlying),
            hl_mark_usd=self.market.hl_mark_usd.get(underlying),
            usdkrw=self.market.usdkrw,
            session=session_map,
            positions=[p for p in positions if p.underlying is underlying],
        )

    # --- 오케스트레이션 ---

    async def step(self, underlying: Underlying) -> list[str]:
        positions = await self._collect_positions()
        state = self.build_market_state(underlying, positions)
        intents = list(self._strategy.evaluate(state))
        if self._risk is not None:
            intents = self._risk.filter(intents, self.risk_state)
        order_ids: list[str] = []
        for intent in intents:
            order_ids.append(await self._route(intent))
        return order_ids

    async def run_once(self) -> dict[Underlying, list[str]]:
        return {u: await self.step(u) for u in Underlying}

    async def _collect_positions(self) -> list[Position]:
        positions: list[Position] = []
        for account in (Account.KR_STOCK, Account.KR_DERIV):
            positions.extend(await self._ls.get_positions(account))
        positions.extend(await self._hl.get_positions())
        return positions

    async def _route(self, intent: OrderIntent) -> str:
        if intent.venue is Venue.LS:
            return await self._ls.place_order(intent)
        return await self._hl.place_order(intent)
