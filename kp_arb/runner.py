"""DryRunner — 단일 asyncio 프로세스 결선 엔트리포인트 (DESIGN.md §4).

지금까지 컴포넌트(세션·엔진·리스크·FX리포터·StateStore·게이트웨이)를 한 프로세스로 묶어
한 사이클을 돌린다: 시세 수신 → MarketState 조립 → 상태저장 → 리스크·전략 → (라우팅) → FX 노출 발행.

드라이런: mock 게이트웨이 + NoopStrategy로 라이브 없이 결선을 검증한다.
"""
from __future__ import annotations

from .domain.enums import Account, Underlying
from .domain.models import MarketState, Position, Quote
from .engine import ArbEngine
from .fx_reporter import FXExposureReporter
from .gateways.base import HLGateway, LSGateway
from .gateways.hl import Mark
from .gateways.ls_ws import MarketStatus
from .risk import RiskManager, RiskState
from .session import reference_instrument
from .session_service import SessionService
from .state_store import StateStore
from .strategy.base import Strategy


class DryRunner:
    def __init__(
        self,
        *,
        session: SessionService,
        strategy: Strategy,
        ls: LSGateway,
        hl: HLGateway,
        reporter: FXExposureReporter,
        store: StateStore,
        risk: RiskManager | None = None,
    ) -> None:
        self._session = session
        self._ls = ls
        self._hl = hl
        self._reporter = reporter
        self._store = store
        self._engine = ArbEngine(session=session, strategy=strategy, ls=ls, hl=hl, risk=risk)

    # --- 시세/장운영 주입(게이트웨이 콜백 대체; 녹화 픽스처로 구동) ---

    def feed_quote(self, quote: Quote) -> None:
        self._engine.on_quote(quote)

    def feed_mark(self, mark: Mark) -> None:
        self._engine.on_mark(mark)

    def feed_market_status(self, status: MarketStatus) -> None:
        self._session.on_market_status(status)

    def set_fx(self, usdkrw: float) -> None:
        self._engine.set_fx(usdkrw)

    # --- 한 사이클 ---

    async def run_cycle(self, *, ts: float) -> list[str]:
        positions = await self._collect_positions()
        await self._update_risk_state()

        order_ids: list[str] = []
        for underlying in Underlying:
            state = self._engine.build_market_state(underlying, positions)
            await self._persist_state(state, ts=ts)
            order_ids.extend(await self._engine.step(underlying))

        for position in positions:
            await self._store.save_position(position, ts=ts)

        report = await self._reporter.report(
            positions, self._engine.market.hl_mark_usd, ts=ts
        )
        await self._store.add_fx_report(
            ts=ts, exposure_usd=report.exposure_usd, sent_ok=bool(self._reporter.last_sent_ok)
        )
        await self._store.log_event(
            ts=ts, level="INFO", component="runner", message=f"cycle orders={len(order_ids)}"
        )
        return order_ids

    async def _collect_positions(self) -> list[Position]:
        positions: list[Position] = []
        for account in (Account.KR_STOCK, Account.KR_DERIV):
            positions.extend(await self._ls.get_positions(account))
        positions.extend(await self._hl.get_positions())
        return positions

    async def _update_risk_state(self) -> None:
        reference_available = {
            underlying: reference_instrument(self._session.session_for(underlying)) is not None
            for underlying in Underlying
        }
        funds = {
            account: await self._ls.get_balance(account)
            for account in (Account.KR_STOCK, Account.KR_DERIV)
        }
        self._engine.risk_state = RiskState(
            reference_available=reference_available,
            account_available_funds=funds,
            hl_margin_ratio=None,
        )

    async def _persist_state(self, state: MarketState, *, ts: float) -> None:
        await self._store.add_market_state(
            ts=ts,
            underlying=state.underlying,
            ref_instrument=state.reference_instrument,
            kr_price=state.reference_price_krw,
            hl_mark=state.hl_mark_usd,
            fx=state.usdkrw,
        )
        for instrument, status in state.session.items():
            await self._store.add_session_log(
                ts=ts,
                underlying=state.underlying,
                instrument=instrument,
                tradeable=status.tradeable,
                is_reference=status.is_reference,
            )
