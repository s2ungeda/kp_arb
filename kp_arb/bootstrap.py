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
from .domain.enums import Account, Underlying
from .domain.models import OrderIntent, Position, Quote
from .gateways.ls import LSApiGateway
from .gateways.ls_ws import Fill, LSWebSocketClient, OrderEvent
from .order_book import OrderBook, TrackedOrder
from .session_service import SessionService


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
    ) -> None:
        self._gw = gateway
        self.order_book = order_book
        self.session = session
        self._stock_ws = stock_ws
        self._deriv_ws = deriv_ws
        self.on_quote: list[Callable[[Quote], None]] = []  # 엔진/전략 결선용
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
        self.order_book.load_snapshot(
            positions=positions, balances=balances, open_orders=open_orders
        )

    # --- 주문 (등록까지 한 번에 — 이후 상태는 이벤트로만) ---

    async def place(self, intent: OrderIntent) -> str:
        order_id = await self._gw.place_order(intent)
        self.order_book.track(order_id, intent)
        return order_id

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
            for handler in self.on_quote:
                handler(quote)

        def apply_fill(fill: Fill) -> None:
            self.order_book.on_fill(fill)

        def apply_event(event: OrderEvent) -> None:
            self.order_book.on_order_event(event)

        for underlying in Underlying:
            self._stock_ws.subscribe_quotes(underlying)
        self._stock_ws.subscribe_market_status()
        self._stock_ws.subscribe_stock_fills()
        self._stock_ws.on_quote.append(fan_quote)
        self._stock_ws.on_market_status.append(self.session.on_market_status)
        self._stock_ws.on_fill.append(apply_fill)
        self._stock_ws.on_order_event.append(apply_event)
        if self._deriv_ws is not None:
            self._deriv_ws.subscribe_futures_fills()
            self._deriv_ws.on_fill.append(apply_fill)
            self._deriv_ws.on_order_event.append(apply_event)

    async def start(self) -> None:
        """최초 스냅샷 → 세션 초기값(옵션) → WS 결선 → 실시간 수신 시작(재연결 포함)."""
        await self.refresh_snapshot()
        self._seed_session_from_env()
        self._wire()
        self._tasks = [asyncio.create_task(self._stock_ws.run())]
        if self._deriv_ws is not None:
            self._tasks.append(asyncio.create_task(self._deriv_ws.run()))

    async def wait(self) -> None:
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []


async def bootstrap_live(session: object) -> LiveSystem:
    """라이브(모의/실전) 조립 — aiohttp 세션을 받아 LS-only LiveSystem 구성.

    HL 게이트웨이는 라이브 결선 시 이 함수에 추가(슬롯 예비).
    """
    from .config import current_mode
    from .gateways.ls import LIVE_BASE_URL
    from .gateways.ls_http import AiohttpRestTransport, AiohttpTokenTransport
    from .gateways.ls_ws_live import LSWebSocketConnector, ls_ws_url

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
    )

    url = ls_ws_url(current_mode())

    async def ws_for(account: Account) -> LSWebSocketClient:
        cred = accounts.for_account(account)
        token = (await token_tx.fetch_token(cred.appkey, cred.appsecret)).access_token
        return LSWebSocketClient(LSWebSocketConnector(url), token=token)

    return LiveSystem(
        gateway=gateway,
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=await ws_for(Account.KR_STOCK),
        deriv_ws=await ws_for(Account.KR_DERIV),
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
            system = await bootstrap_live(http)
            quotes = {"n": 0}
            system.on_quote.append(lambda q: quotes.__setitem__("n", quotes["n"] + 1))
            await system.start()
            ob = system.order_book
            print(f"[snapshot] stock bal = {ob.balance(Account.KR_STOCK):,.0f}")
            print(f"[snapshot] deriv bal = {ob.balance(Account.KR_DERIV):,.0f}")
            print(f"[snapshot] positions = {ob.positions()}")
            print(f"[snapshot] open orders = {[o.order_id for o in ob.open_orders()]}")
            print(f"[live] {seconds:.0f}s 실시간 수신 중 ...")
            await asyncio.sleep(seconds)
            print(f"[live] quotes received = {quotes['n']}")
            print(f"[live] session phase(samsung) = "
                  f"{system.session.phase_for(Underlying.SAMSUNG).value}")
            await system.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()
