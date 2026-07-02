"""ArbEngine — 전략 비종속 오케스트레이터 (DESIGN.md §5.5).

흐름: MarketState 조립 → Strategy.evaluate() → RiskManager 검증 → 주문 실행.
엔진은 **결정 로직을 갖지 않는다**(전략 플러그인이 담당). 시세는 콜백으로 갱신되는
최신값 저장소(MarketData)에서 읽는다.

입력원은 주입으로 바꿀 수 있다(라이브 연결용):
- ``positions_provider``: 포지션 공급 함수 — 주면 OrderBook의 실시간 값을 쓰고,
  없으면 게이트웨이 REST 조회(기존 동작).
- ``place_fn``: 주문 실행 함수 — 주면 LiveSystem.place(주문 등록 포함)를 쓰고,
  없으면 게이트웨이 직접 호출(기존 동작).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from .domain.enums import Account, Instrument, Underlying, Venue
from .domain.models import MarketState, OrderIntent, Position, Quote
from .gateways.base import HLGateway, LSGateway
from .gateways.hl import Mark
from .risk import RiskManager, RiskState
from .session import reference_instrument
from .session_service import SessionService
from .strategy.base import Strategy


class MarketData:
    """엔진이 읽는 최신 시세 저장소. 실시간 콜백이 갱신한다."""

    def __init__(self) -> None:
        self.reference_price_krw: dict[Underlying, float] = {}  # 주식(레퍼런스) 중간가
        self.etf_price_krw: dict[Underlying, float] = {}        # 레버리지 ETF 중간가
        self.hl_mark_usd: dict[Underlying, float] = {}
        self.usdkrw: float | None = None


class ArbEngine:
    def __init__(
        self,
        *,
        session: SessionService,
        strategy: Strategy,
        ls: LSGateway | None = None,
        hl: HLGateway | None = None,
        risk: RiskManager | None = None,
        positions_provider: Callable[[], Sequence[Position]] | None = None,
        place_fn: Callable[[OrderIntent], Awaitable[str]] | None = None,
    ) -> None:
        if positions_provider is None and (ls is None or hl is None):
            raise ValueError("ls/hl 게이트웨이 또는 positions_provider가 필요")
        if place_fn is None and (ls is None or hl is None):
            raise ValueError("ls/hl 게이트웨이 또는 place_fn이 필요")
        self._session = session
        self._strategy = strategy
        self._ls = ls
        self._hl = hl
        self._risk = risk
        self._positions_provider = positions_provider
        self._place_fn = place_fn
        self.market = MarketData()
        self.risk_state = RiskState()  # 리스크 판단 상태(연결부가 갱신)

    # --- 시세 스냅샷 갱신(게이트웨이 콜백 연결용) ---

    def on_quote(self, quote: Quote) -> None:
        # ETF 시세는 별도 보관 — 주식(레퍼런스) 가격을 덮어쓰지 않는다.
        if quote.instrument is Instrument.KR_ETF:
            self.market.etf_price_krw[quote.underlying] = quote.mid
        else:
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
        if self._positions_provider is not None:
            return list(self._positions_provider())  # 실시간(OrderBook) 값
        assert self._ls is not None and self._hl is not None
        positions: list[Position] = []
        for account in (Account.KR_STOCK, Account.KR_DERIV):
            positions.extend(await self._ls.get_positions(account))
        positions.extend(await self._hl.get_positions())
        return positions

    async def _route(self, intent: OrderIntent) -> str:
        if self._place_fn is not None:
            return await self._place_fn(intent)  # LiveSystem.place (주문 등록 포함)
        assert self._ls is not None and self._hl is not None
        if intent.venue is Venue.LS:
            return await self._ls.place_order(intent)
        return await self._hl.place_order(intent)
