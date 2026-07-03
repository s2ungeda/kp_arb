"""부트스트랩 — 시스템 시동 절차 (DESIGN.md §5.9 운영 모델).

시동 순서:
1) 계좌·비밀 로드(keyring/env) → 계좌별 게이트웨이 생성
2) t8401 마스터로 **선물 최근월물 코드 자동 조회**(만기 롤오버 대응)
3) **최초 REST 스냅샷 1회**: 잔고(주식/선물)·포지션·미체결 → OrderBook 초기화
4) WS 결선: 시세(H1_/JIF)+체결통보(SC*/O01·C01·H01) → OrderBook·SessionService
   — 이후는 실시간 이벤트가 기본(체결 대기 폴링 없음)
같은 스냅샷(`refresh_snapshot`)은 온디맨드(추후 UI 조회 버튼)로 재호출 가능.

계좌 통보는 접속 토큰의 계좌 것만 오므로 **계좌별 WS 2개**(주식/선물옵션)를 쓴다(실측).
HL 게이트웨이는 슬롯만 예비(라이브 결선 시 추가).

수동 실행(모의 시동 확인): ``python -m kp_arb.bootstrap [초]``
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from .config import LSAccounts
from .domain.enums import Account, Instrument, Underlying, Venue
from .domain.models import OrderIntent, Position, Quote
from .engine import ArbEngine
from .gateways.base import HLGateway
from .gateways.hl import Mark
from .gateways.hl_ws import HLWebSocketClient
from .gateways.ls import LSApiGateway
from .gateways.ls_ws import (
    ExpectedPrice,
    Fill,
    LSWebSocketClient,
    OrderEvent,
    TradeTick,
)
from .order_book import OrderBook, TrackedOrder
from .risk import RiskManager, RiskState
from .session import reference_instrument
from .session_service import SessionService
from .strategy.base import Strategy


def select_near_month_futures(rows: list[dict[str, object]]) -> dict[Underlying, str]:
    """t8401 마스터 행에서 underlying별 **최근월물** 주식선물 코드를 고른다. 순수 로직.

    행: {hname: "삼성전자   F 202607", shcode: "A1167000", basecode: "A005930"}.
    스프레드(" SP ") 제외, hname 끝의 YYYYMM 최소(=최근월물) 선택.
    """
    base_to_underlying = {f"A{u.krx_code}": u for u in Underlying}
    best: dict[Underlying, tuple[str, str]] = {}  # underlying -> (yyyymm, shcode)
    for row in rows:
        underlying = base_to_underlying.get(str(row.get("basecode", "")))
        hname = str(row.get("hname", ""))
        parts = hname.split()
        if underlying is None or len(parts) < 3 or parts[-2] != "F":
            continue  # 대상 아님 또는 스프레드(SP)
        yyyymm = parts[-1]
        if not (len(yyyymm) == 6 and yyyymm.isdigit()):
            continue
        shcode = str(row.get("shcode", ""))
        if underlying not in best or yyyymm < best[underlying][0]:
            best[underlying] = (yyyymm, shcode)
    return {u: shcode for u, (_, shcode) in best.items()}


class LiveSystem:
    """조립된 부품(게이트웨이·WS·OrderBook·세션)의 시동/결선/온디맨드 조회."""

    def __init__(
        self,
        *,
        gateway: LSApiGateway,
        order_book: OrderBook,
        session: SessionService,
        stock_ws: LSWebSocketClient,
        deriv_ws: LSWebSocketClient | None = None,
        hl_gateway: HLGateway | None = None,
        hl_ws: HLWebSocketClient | None = None,
        futures_symbols: dict[Underlying, str] | None = None,
        etf_symbols: dict[Underlying, str] | None = None,
    ) -> None:
        self._gw = gateway
        # 취급 종목코드 (공개 — UI/도구가 상품 가용성 판단에 사용. 예: 현대차 ETF 없음)
        self.futures_symbols = dict(futures_symbols or {})
        self.etf_symbols = dict(etf_symbols or {})
        self._hl = hl_gateway
        self._hl_ws = hl_ws
        self.order_book = order_book
        self.session = session
        self._stock_ws = stock_ws
        self._deriv_ws = deriv_ws
        # 최신 호가판 보관소 — 주식 10호가·선물 5호가 등 다단계 포함(Quote.bids/asks).
        # 키: (underlying, instrument, market["krx"|"nxt"|"hl"]). 모니터·페깅·전략이 공유.
        self.quotes: dict[tuple[Underlying, Instrument, str], Quote] = {}
        self.on_quote: list[Callable[[Quote], None]] = []  # 호가(LS 주식/선물/ETF + HL bbo)
        self.on_mark: list[Callable[[Mark], None]] = []    # HL 마크
        self.on_trade: list[Callable[[TradeTick], None]] = []        # 체결(현재가)
        self.on_expected: list[Callable[[ExpectedPrice], None]] = []  # 예상체결가
        self.on_funding: list[Callable[[Underlying, float], None]] = []  # HL 예정 펀딩률
        self.on_fill: list[Callable[[Fill], None]] = []  # 체결통보 (OrderBook 반영 후 호출)
        self._tasks: list[asyncio.Task[None]] = []

    # --- 스냅샷 (최초 실행 + 온디맨드/UI 조회 버튼) ---

    async def refresh_snapshot(self) -> None:
        positions: list[Position] = []
        balances: dict[Account, float] = {}
        open_orders: list[TrackedOrder] = []
        for account in (Account.KR_STOCK, Account.KR_DERIV):
            balances[account] = await self._gw.get_balance(account)
            positions.extend(await self._gw.get_positions(account))
            open_orders.extend(await self._gw.get_open_orders(account))
        if self._hl is not None:
            positions.extend(await self._hl.get_positions())
            open_orders.extend(await self._hl.get_open_orders())
        self.order_book.load_snapshot(
            positions=positions, balances=balances, open_orders=open_orders
        )

    async def price_snapshots(self) -> dict[tuple[Underlying, Instrument], float]:
        """취급 전 종목 현재가 1회 조회(창 오픈 시 초기 표시용 — 마감 후엔 종가)."""
        return await self._gw.get_price_snapshots()

    # --- 주문 (등록까지 한 번에 — 이후 상태는 이벤트로만) ---

    async def place(self, intent: OrderIntent) -> str:
        """venue 라우팅 주문 + OrderBook 등록. 이후 상태는 이벤트로만."""
        if intent.venue is Venue.LS:
            order_id = await self._gw.place_order(intent)
        elif self._hl is not None:
            order_id = await self._hl.place_order(intent)
        else:
            raise RuntimeError("HL gateway not configured")
        self.order_book.track(order_id, intent)
        return order_id

    async def amend_price(self, order_id: str, price: float) -> str:
        """가격 정정 (venue 라우팅) — 새 주문번호를 등록하고 원주문은 취소 처리.

        LS는 CSPAT00701/CFOAT00200, HL은 modify 액션(취소+신규를 서버에서 한 번에).
        """
        order = self.order_book.order(order_id)
        if order is None:
            raise ValueError(f"unknown order {order_id}")
        if order.intent.venue is Venue.LS:
            new_id = await self._gw.amend_order(order_id, price=price)
        else:
            assert self._hl is not None
            new_id = await self._hl.amend_order(order_id, price=price)
        new_intent = order.intent.model_copy(update={"price": price})
        self.order_book.track(new_id, new_intent)
        if new_id != order_id:
            self.order_book.on_cancel(order_id)  # 원주문은 정정으로 소멸
        return new_id

    async def cancel(self, order_id: str) -> None:
        """venue 라우팅 취소. HL은 취소 통보 채널이 없어 로컬 상태도 갱신."""
        order = self.order_book.order(order_id)
        if order is None:
            raise ValueError(f"unknown order {order_id}")
        if order.intent.venue is Venue.LS:
            await self._gw.cancel_order(order_id)  # 상태는 SC3/H01 통보로 전이
        else:
            assert self._hl is not None
            await self._hl.cancel_order(order_id)
            self.order_book.on_cancel(order_id)

    # --- 시동 ---

    def _seed_session_from_env(self) -> None:
        """장중 재시작용 초기 세션(KP_SESSION_INIT=regular 등). JIF 수신 시 항상 JIF 우선.

        LS REST에는 '현재 장상태' 조회 TR이 없다 — 표준 운영은 **개장 전 시동**
        (JIF 카운트다운을 자연 수신). 미설정/미지 값이면 보수적 DEAD 유지.
        """
        import os

        from .domain.enums import SessionPhase

        raw = os.environ.get("KP_SESSION_INIT", "").strip().lower()
        if not raw:
            return
        try:
            self.session.seed_phase(SessionPhase(raw))
        except ValueError:
            pass  # 미지 값 → 시딩하지 않음(DEAD 유지)

    def _wire(self) -> None:
        def fan_quote(quote: Quote) -> None:
            # 최신 호가판 보관(다단계 포함) 후 콜백 전달.
            self.quotes[(quote.underlying, quote.instrument, quote.market)] = quote
            for handler in self.on_quote:
                handler(quote)

        def fan_trade(tick: TradeTick) -> None:
            for handler in self.on_trade:
                handler(tick)

        def fan_expected(expected: ExpectedPrice) -> None:
            for handler in self.on_expected:
                handler(expected)

        def apply_fill(fill: Fill) -> None:
            self.order_book.on_fill(fill)
            for handler in self.on_fill:  # OrderBook 반영 뒤라 상태 조회가 안전
                handler(fill)

        def apply_event(event: OrderEvent) -> None:
            self.order_book.on_order_event(event)

        for underlying in Underlying:
            self._stock_ws.subscribe_quotes(underlying)
            self._stock_ws.subscribe_trades(underlying)  # 현재가(S3_) + 예상체결(YS3)
        if self.futures_symbols:
            self._stock_ws.subscribe_futures_quotes(self.futures_symbols)
        self._stock_ws.subscribe_market_status()
        self._stock_ws.subscribe_stock_fills()
        self._stock_ws.on_quote.append(fan_quote)
        self._stock_ws.on_trade.append(fan_trade)
        self._stock_ws.on_expected.append(fan_expected)
        self._stock_ws.on_market_status.append(self.session.on_market_status)
        self._stock_ws.on_fill.append(apply_fill)
        self._stock_ws.on_order_event.append(apply_event)
        if self._deriv_ws is not None:
            self._deriv_ws.subscribe_futures_fills()
            self._deriv_ws.on_fill.append(apply_fill)
            self._deriv_ws.on_order_event.append(apply_event)
        if self._hl_ws is not None:
            def fan_mark(mark: Mark) -> None:
                for handler in self.on_mark:
                    handler(mark)

            def fan_funding(underlying: Underlying, rate: float) -> None:
                for handler in self.on_funding:
                    handler(underlying, rate)

            self._hl_ws.subscribe_marks()
            self._hl_ws.subscribe_bbo()     # 최우선호가+잔량 → on_quote(HL_PERP)
            self._hl_ws.subscribe_l2book()  # 호가창 다단계(2호가~) — 페깅 N호가용
            self._hl_ws.subscribe_trades()  # 공개 체결(현재가) — 마크(1초)보다 빠름
            self._hl_ws.on_mark.append(fan_mark)
            self._hl_ws.on_quote.append(fan_quote)
            self._hl_ws.on_trade.append(fan_trade)
            self._hl_ws.on_funding.append(fan_funding)
            self._hl_ws.on_fill.append(apply_fill)  # HL 체결 → OrderBook (oid로 매칭)

    async def start(self) -> None:
        """최초 스냅샷 → 세션 초기값(옵션) → WS 결선 → 실시간 수신 시작(재연결 포함)."""
        await self.refresh_snapshot()
        self._seed_session_from_env()
        self._wire()
        self._tasks = [asyncio.create_task(self._stock_ws.run())]
        if self._deriv_ws is not None:
            self._tasks.append(asyncio.create_task(self._deriv_ws.run()))
        if self._hl_ws is not None:
            self._tasks.append(asyncio.create_task(self._hl_ws.run()))

    # --- 엔진 연결 (실시간 시세·포지션·잔고 → 전략 판단) ---

    def attach_engine(self, strategy: Strategy, *, risk: RiskManager | None = None) -> ArbEngine:
        """전략 엔진을 실시간 데이터에 연결해 돌려준다.

        - 포지션: OrderBook 실시간 값 (반복 조회 없음)
        - 주문: self.place (주문 등록 포함, venue 라우팅)
        - 시세: 국내 호가(on_quote)·HL 마크(on_mark)가 엔진 시장 상태로 흘러감
        """
        engine = ArbEngine(
            session=self.session,
            strategy=strategy,
            risk=risk,
            positions_provider=self.order_book.positions,
            place_fn=self.place,
        )
        self.on_quote.append(engine.on_quote)
        self.on_mark.append(engine.on_mark)
        return engine

    def _refresh_risk_state(self, engine: ArbEngine) -> None:
        """리스크 판단 상태를 실시간 값으로 갱신(레퍼런스 유무·계좌 잔고)."""
        engine.risk_state = RiskState(
            reference_available={
                u: reference_instrument(self.session.session_for(u)) is not None
                for u in Underlying
            },
            account_available_funds={
                a: self.order_book.balance(a)
                for a in (Account.KR_STOCK, Account.KR_DERIV)
            },
            hl_margin_ratio=None,  # HL 마진비율 산출은 추후(§8)
        )

    async def run_strategy_loop(
        self, engine: ArbEngine, *, interval_s: float = 1.0, max_cycles: int | None = None
    ) -> None:
        """전략을 주기적으로 실행. 매 주기: 리스크 상태 갱신 → 3종목 판단·주문."""
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            self._refresh_risk_state(engine)
            await engine.run_once()
            cycles += 1
            await asyncio.sleep(interval_s)

    async def wait(self) -> None:
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []


async def bootstrap_live(
    session: object, *, config_path: str | None = None
) -> LiveSystem:
    """라이브(모의/실전) 조립 — 취급 종목은 config.yaml, 비밀은 keyring/env.

    HL 비밀 미등록 시 LS-only로 동작.
    """
    from .config import ConfigError, current_mode, default_secrets, load_config
    from .gateways.hl_live import HLSdkGateway
    from .gateways.hl_ws import HLWebSocketConnector
    from .gateways.ls import LIVE_BASE_URL
    from .gateways.ls_http import AiohttpRestTransport, AiohttpTokenTransport
    from .gateways.ls_ws_live import LSWebSocketConnector, ls_ws_url

    config = load_config(config_path) if config_path else load_config()
    etf_symbols = config.etf_symbols()
    accounts = LSAccounts.load()
    token_tx = AiohttpTokenTransport(session, LIVE_BASE_URL)
    gateway = LSApiGateway.from_accounts(
        accounts,
        token_transport=token_tx,
        rest_transport=AiohttpRestTransport(session),
        base_url=LIVE_BASE_URL,
    )
    # 선물 최근월물 자동 조회(만기 롤오버 대응) 후 게이트웨이 재조립
    futures_symbols = select_near_month_futures(await gateway.fetch_futures_master())
    gateway = LSApiGateway.from_accounts(
        accounts,
        token_transport=token_tx,
        rest_transport=AiohttpRestTransport(session),
        base_url=LIVE_BASE_URL,
        futures_symbols=futures_symbols,
        etf_symbols=etf_symbols,
    )

    url = ls_ws_url(current_mode())

    async def ws_for(account: Account) -> LSWebSocketClient:
        cred = accounts.for_account(account)
        token = (await token_tx.fetch_token(cred.appkey, cred.appsecret)).access_token
        return LSWebSocketClient(
            LSWebSocketConnector(url), token=token, etf_symbols=etf_symbols
        )

    # HL 슬롯 — 비밀(HL_AGENT_KEY/HL_ACCOUNT_ADDRESS) 없으면 LS-only.
    hl_gateway = None
    hl_ws = None
    try:
        hl_gateway = HLSdkGateway.from_secrets(symbols=config.hl_symbols())
        hl_ws = HLWebSocketClient(HLWebSocketConnector(), symbols=config.hl_symbols())
        hl_ws.subscribe_user_fills(str(default_secrets().get("HL_ACCOUNT_ADDRESS")))
    except ConfigError:
        pass

    return LiveSystem(
        gateway=gateway,
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=await ws_for(Account.KR_STOCK),
        deriv_ws=await ws_for(Account.KR_DERIV),
        hl_gateway=hl_gateway,
        hl_ws=hl_ws,
        futures_symbols=futures_symbols,
        etf_symbols=etf_symbols,
    )


def main() -> None:
    """모의 시동 스모크: 시동 → 스냅샷 출력 → N초 실시간 수신 → 종료."""
    import sys

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

    async def run() -> None:
        import aiohttp

        async with aiohttp.ClientSession() as http:
            from .strategy.noop import NoopStrategy

            system = await bootstrap_live(http)
            quotes = {"n": 0}
            marks: dict[str, float] = {}
            system.on_quote.append(lambda q: quotes.__setitem__("n", quotes["n"] + 1))
            system.on_mark.append(lambda m: marks.__setitem__(m.underlying.value, m.price))
            engine = system.attach_engine(NoopStrategy())
            await system.start()
            ob = system.order_book
            print(f"[snapshot] stock bal = {ob.balance(Account.KR_STOCK):,.0f}")
            print(f"[snapshot] deriv bal = {ob.balance(Account.KR_DERIV):,.0f}")
            print(f"[snapshot] positions = {ob.positions()}")
            print(f"[snapshot] open orders = {[o.order_id for o in ob.open_orders()]}")
            print(f"[live] {seconds:.0f}s 실시간 수신 + 전략 루프(Noop) ...")
            loop_task = asyncio.create_task(
                system.run_strategy_loop(engine, interval_s=1.0)
            )
            await asyncio.sleep(seconds)
            loop_task.cancel()
            state = engine.build_market_state(Underlying.SAMSUNG, ob.positions())
            print(f"[live] quotes received = {quotes['n']} / hl marks = {marks}")
            print(f"[live] MarketState(samsung): ref={state.reference_instrument} "
                  f"kr={state.reference_price_krw} hl={state.hl_mark_usd}")
            print(f"[live] session phase(samsung) = "
                  f"{system.session.phase_for(Underlying.SAMSUNG).value}")
            await system.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()
