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
from collections.abc import Callable, Coroutine
from typing import Any

from .config import CarryRates, FeeRates, LSAccounts
from .disparity import (
    PairBoard,
    SideDisp,
    disp,
    net_entry,
    net_exit,
    pair_spread,
    side_disp,
)
from .domain.enums import Account, Instrument, Underlying, Venue
from .domain.models import OrderIntent, Position, Quote
from .engine import ArbEngine
from .etf_theory import EtfTheoryInputs, theory_after, theory_regular
from .gateways.base import HLGateway
from .gateways.hl import Mark
from .gateways.hl_ws import HLWebSocketClient
from .gateways.ls import LSApiGateway, OrderGoneError
from .gateways.ls_rest import RestError
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
from .theory import (
    carry_theory,
    days_to_expiry,
    select_usd_futures,
)


def select_near_month(
    rows: list[dict[str, object]],
) -> dict[Underlying, tuple[str, int]]:
    """t8401 마스터 행에서 underlying별 **최근월물** (shcode, 만기 YYYYMM). 순수 로직.

    행: {hname: "삼성전자   F 202607", shcode: "A1167000", basecode: "A005930"}.
    스프레드(" SP ") 제외, hname 끝의 YYYYMM 최소(=최근월물) 선택.
    만기는 캐리 이론가 잔존일 계산에 쓴다(DESIGN §6.1).
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
    return {u: (shcode, int(ym)) for u, (ym, shcode) in best.items()}


def select_near_month_futures(rows: list[dict[str, object]]) -> dict[Underlying, str]:
    """underlying별 최근월물 주식선물 **코드만** (기존 호환)."""
    return {u: shcode for u, (shcode, _) in select_near_month(rows).items()}


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
        futures_expiry: dict[Underlying, int] | None = None,
        fx_futures: tuple[str, int] | None = None,
        carry_rates: CarryRates | None = None,
        fees: FeeRates | None = None,
    ) -> None:
        self._gw = gateway
        # 취급 종목코드 (공개 — UI/도구가 상품 가용성 판단에 사용. 예: 현대차 ETF 없음)
        self.futures_symbols = dict(futures_symbols or {})
        self.etf_symbols = dict(etf_symbols or {})
        self.futures_expiry = dict(futures_expiry or {})  # 만기 YYYYMM (캐리 잔존일용)
        # 원달러선물 (shcode, 만기YYYYMM) — t8426 최근월물 (bootstrap_live에서 조회).
        self._fx_futures = fx_futures
        # 캐리 이론가 연이자율·왕복 수수료 — config.yaml 조정 대상
        self._carry = carry_rates if carry_rates is not None else CarryRates()
        self._fees = fees if fees is not None else FeeRates()
        # 환율이론가(원달러선물 현물환산, DESIGN §6.1) — WS(FC0) 실시간 + 예비 조회 갱신.
        self.usdkrw_theory: float | None = None
        self.usdkrw_futures: float | None = None  # 원달러선물 현재가 원값(표시용)
        self._hl = hl_gateway
        self._hl_ws = hl_ws
        self.order_book = order_book
        self.session = session
        self._stock_ws = stock_ws
        self._deriv_ws = deriv_ws
        # 최신 호가판 보관소 — 주식 10호가·선물 5호가 등 다단계 포함(Quote.bids/asks).
        # 키: (underlying, instrument, market["krx"|"nxt"|"hl"]). 모니터·페깅·전략이 공유.
        self.quotes: dict[tuple[Underlying, Instrument, str], Quote] = {}
        # 최신 체결가 보관소 — 시장별(krx/nxt/hl/uni). ETF 이론가의 기초가(KRX)에도 사용.
        self.trades: dict[tuple[Underlying, Instrument, str], float] = {}
        # 기초 주식 등락률(%, drate) — ETF 이론가의 핵심 입력 (ETF 이론가.md §2).
        self.stock_change_pct: dict[tuple[Underlying, str], float] = {}
        # 예상체결가(동시호가) — (underlying, instrument)별. 기초 주식의 예상등락률 포함.
        self.expected_prices: dict[tuple[Underlying, Instrument], float] = {}
        self.stock_exp_change_pct: dict[Underlying, float] = {}
        # ETF 이론가 고정 입력(전일NAV·배율·기초 전일종가) — 시동 시 1회 조회.
        self.etf_theory: dict[Underlying, EtfTheoryInputs] = {}
        self.on_quote: list[Callable[[Quote], None]] = []  # 호가(LS 주식/선물/ETF + HL bbo)
        self.on_mark: list[Callable[[Mark], None]] = []    # HL 마크
        self.on_trade: list[Callable[[TradeTick], None]] = []        # 체결(현재가)
        self.on_expected: list[Callable[[ExpectedPrice], None]] = []  # 예상체결가
        self.on_funding: list[Callable[[Underlying, float], None]] = []  # HL 예정 펀딩률
        self.on_fill: list[Callable[[Fill], None]] = []  # 체결통보 (OrderBook 반영 후 호출)
        self._tasks: list[asyncio.Task[None]] = []

    # --- 스냅샷 (최초 실행 + 온디맨드/UI 조회 버튼) ---

    async def refresh_snapshot(self) -> None:
        import logging

        positions: list[Position] = []
        balances: dict[Account, float] = {}
        open_orders: list[TrackedOrder] = []
        for account in (Account.KR_STOCK, Account.KR_DERIV):
            # 실계좌 환경 편차(선물 계좌 없음, 형식 거부 등)로 한 계좌 조회가
            # 실패해도 시동을 멈추지 않는다 — 해당 계좌만 빼고 계속.
            try:
                balances[account] = await self._gw.get_balance(account)
                positions.extend(await self._gw.get_positions(account))
                open_orders.extend(await self._gw.get_open_orders(account))
            except RestError:
                logging.getLogger("kp_arb.bootstrap").warning(
                    "%s 계좌 스냅샷 실패 — 이 계좌 없이 계속", account.value, exc_info=True
                )
                balances.setdefault(account, 0.0)
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
        수량은 **잔량 기준**으로 보낸다 — 부분체결 후 원수량 정정은 거부됨(실측 01442).
        """
        order = self.order_book.order(order_id)
        if order is None:
            raise ValueError(f"unknown order {order_id}")
        qty = order.remaining_qty
        if qty <= 0:
            raise OrderGoneError(f"order {order_id} has no remaining qty")
        if order.intent.venue is Venue.LS:
            new_id = await self._gw.amend_order(order_id, qty=qty, price=price)
        else:
            assert self._hl is not None
            new_id = await self._hl.amend_order(order_id, qty=qty, price=price)
        new_intent = order.intent.model_copy(update={"price": price, "qty": qty})
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
            self.trades[(tick.underlying, tick.instrument, tick.market)] = tick.price
            if tick.instrument is Instrument.KR_STOCK and tick.change_pct is not None:
                self.stock_change_pct[(tick.underlying, tick.market)] = tick.change_pct
            for handler in self.on_trade:
                handler(tick)

        def fan_expected(expected: ExpectedPrice) -> None:
            self.expected_prices[(expected.underlying, expected.instrument)] = expected.price
            if (expected.instrument is Instrument.KR_STOCK
                    and expected.change_pct is not None):
                self.stock_exp_change_pct[expected.underlying] = expected.change_pct
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
        if self._fx_futures is not None:
            # 원달러선물 체결(FC0) 실시간 → 환율이론가 (예비는 _fx_loop 30초 조회)
            self._stock_ws.subscribe_fx(self._fx_futures[0])
            self._stock_ws.on_fx_price.append(self._apply_fx_price)
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
        # 시동 REST 조회들은 **순차 실행** — 동시에 나가면 서버 계정당 초당 한도에
        # 걸려 일부(t1901 등)가 실패한다(운영 실측). 환율 폴링은 그 뒤에 시작.
        self._tasks = [asyncio.create_task(self._startup_queries())]
        self._tasks.append(asyncio.create_task(self._guarded_ws("주식", self._stock_ws.run())))
        if self._deriv_ws is not None:
            self._tasks.append(
                asyncio.create_task(self._guarded_ws("선물", self._deriv_ws.run()))
            )
        if self._hl_ws is not None:
            self._tasks.append(asyncio.create_task(self._guarded_ws("HL", self._hl_ws.run())))

    def _apply_fx_price(self, price: float) -> None:
        """원달러선물 현재가 → 환율이론가(현물환산) 갱신. WS(FC0)·예비 조회 공용."""
        from datetime import date

        if self._fx_futures is None or price <= 0:
            return
        _, ym = self._fx_futures
        days = days_to_expiry(ym, "USD", date.today())
        self.usdkrw_futures = price
        self.usdkrw_theory = carry_theory(price, days, self._carry.fx)

    async def _fx_loop(self) -> None:
        """환율 예비 갱신 — 시동 직후 초기값 + 30초 간격 확인 조회(t2111).

        본선은 WS(FC0, K200선물 계열 TR — 사용자 확인) 실시간이고, 이 루프는
        WS가 조용할 때(체결 없음·미실측 필드 불일치)의 안전망이다. 주간(08~16시)만.
        """
        import logging
        from datetime import datetime

        if self._fx_futures is None:
            return
        code, _ = self._fx_futures
        log = logging.getLogger("kp_arb.bootstrap")
        failures = 0
        while True:
            hour = datetime.now().hour
            if not 8 <= hour < 16:  # 주간 세션 밖 — 마지막 값 유지
                await asyncio.sleep(60.0)
                continue
            try:
                price = await self._gw.get_fx_futures_price(code)
                if price is not None:
                    self._apply_fx_price(price)
                failures = 0
            except Exception:  # noqa: BLE001
                failures += 1
                if failures == 1:  # 반복 실패는 첫 번째만 기록
                    log.warning("원달러선물 시세(t2111) 조회 실패 — 재시도 계속",
                                exc_info=True)
            await asyncio.sleep(30.0)

    # --- 괴리 보드 (DESIGN §6.1 — 모니터·전략 공용) ---

    def stock_futures_theory(self, underlying: Underlying) -> float | None:
        """주식선물 이론가 = 기초 주식 현재가 × (1 + 3.5% × 잔존일/365).

        기초가는 **통합(uni, NXT 포함) 우선, 없으면 KRX** — 엑셀(RTD)과 동일 기준.
        (ETF 이론가의 기초는 KRX 전용 유지 — 거래소 iNAV 기준과 일치시키기 위함.)
        """
        from datetime import date

        base = self.stock_last(underlying)
        ym = self.futures_expiry.get(underlying)
        if base is None or ym is None:
            return None
        return carry_theory(
            base, days_to_expiry(ym, "EQ", date.today()), self._carry.stock_futures
        )

    def _best_quote(
        self, underlying: Underlying, instrument: Instrument
    ) -> tuple[float | None, float | None]:
        """통합(uni)·KRX·NXT 중 최우선호가 (매도가, 매수가) — 실제 체결 가능한 호가."""
        candidates = [
            q for m in ("uni", "krx", "nxt")
            if (q := self.quotes.get((underlying, instrument, m))) is not None
        ]
        if not candidates:
            return None, None
        return (min(q.ask for q in candidates), max(q.bid for q in candidates))

    def stock_last(self, underlying: Underlying) -> float | None:
        """기초 주식 현재가 — 통합(uni, NXT 포함) 우선, 없으면 KRX. 엑셀(RTD 현재가)과 동일."""
        return (self.trades.get((underlying, Instrument.KR_STOCK, "uni"))
                or self.trades.get((underlying, Instrument.KR_STOCK, "krx")))

    def _hl_disp(self, underlying: Underlying) -> SideDisp:
        """HL 호가를 환율이론가로 원화 환산 → 국내 주식 현재가(통합 우선) 대비 괴리."""
        quote = self.quotes.get((underlying, Instrument.HL_PERP, "hl"))
        fx = self.usdkrw_theory
        base = self.stock_last(underlying)
        if quote is None or fx is None:
            return side_disp(None, None, base)
        return side_disp(quote.ask * fx, quote.bid * fx, base)

    def disparity_board(self) -> dict[tuple[Underlying, Instrument], PairBoard]:
        """HL vs 국내 상대(주식선물/ETF)별 괴리·진입/청산 스프레드 (DESIGN §6.1)."""
        board: dict[tuple[Underlying, Instrument], PairBoard] = {}
        for u in Underlying:
            hl = self._hl_disp(u)
            # HL 현재가(체결) 괴리 — 엑셀 시세!AD열(메인 I22)
            hl_px = self.trades.get((u, Instrument.HL_PERP, "hl"))
            fx = self.usdkrw_theory
            hl_last = disp(
                hl_px * fx if hl_px is not None and fx is not None else None,
                self.stock_last(u),
            )
            targets: list[tuple[Instrument, float | None]] = []
            if u in self.futures_symbols:
                targets.append(
                    (Instrument.KR_STOCK_FUTURE, self.stock_futures_theory(u))
                )
            if u in self.etf_symbols:
                targets.append((Instrument.KR_ETF, self.etf_theory_price(u)))
            for instrument, base in targets:
                ask, bid = self._best_quote(u, instrument)
                kr = side_disp(ask, bid, base)
                kr_last_px = (self.trades.get((u, instrument, "uni"))
                              or self.trades.get((u, instrument, "krx")))
                spread = pair_spread(hl, kr)
                fee = (self._fees.stock_future
                       if instrument is Instrument.KR_STOCK_FUTURE else self._fees.etf)
                board[(u, instrument)] = PairBoard(
                    hl=hl, kr=kr, spread=spread,
                    hl_last=hl_last, kr_last=disp(kr_last_px, base),
                    net_entry=net_entry(spread, fee),
                    net_exit=net_exit(spread),
                )
        return board

    async def _seed_prices(self) -> None:
        """장중 체결이 오기 전(개장 전·애프터·한산 종목) 현재가 초기값 — 스냅샷 1회.

        합성 체결(market="krx")로 흘려서 모니터·이론가가 같은 경로로 받는다.
        실시간 체결이 이미 온 종목은 덮지 않는다. (현대차처럼 체결이 뜸한 종목의
        이론가 기초가 확보 — 운영 실측에서 나온 보강)
        """
        import logging

        try:
            snapshot = await self._gw.get_price_snapshots()
        except Exception:  # noqa: BLE001 - 초기값 없이도 실시간은 정상
            logging.getLogger("kp_arb.bootstrap").warning(
                "초기 가격 스냅샷 실패 — 실시간 체결만 사용", exc_info=True
            )
            return
        for (u, inst), price in snapshot.items():
            key = (u, inst, "krx")
            if key in self.trades:
                continue  # 실시간이 먼저 왔으면 그쪽 우선
            self.trades[key] = price
            tick = TradeTick(underlying=u, instrument=inst, price=price,
                             ts=0.0, market="krx")
            for handler in self.on_trade:
                handler(tick)

    async def _load_etf_refs(self) -> None:
        """ETF 이론가 고정 입력 조회(백그라운드 1회) — 실패해도 시스템은 계속."""
        import logging

        try:
            self.etf_theory = dict(await self._gw.get_etf_refs())
        except Exception:  # noqa: BLE001 - 이론가 없이도 나머지는 정상
            logging.getLogger("kp_arb.bootstrap").warning(
                "ETF 이론가 입력 조회 실패 — 이론가 없이 계속", exc_info=True
            )

    def etf_theory_price(self, underlying: Underlying) -> float | None:
        """ETF 이론가(ETF 이론가.md §1) — 모니터·전략 공용. 시간대는 세션으로 판단.

        - 정규장/장전: 전일NAV × (1 + 배율 × 기초 KRX 등락률[drate, §2]) — KRX 기준(§4-1)
        - 그 외(애프터·시간외): 당일종가NAV × (1 + 배율 × 애프터 등락률) —
          애프터 현재가는 통합(uni, NXT) 체결, 당일종가는 KRX 마지막 체결.
        """
        from .domain.enums import SessionPhase

        inputs = self.etf_theory.get(underlying)
        rate_krx = self.stock_change_pct.get((underlying, "krx"))
        phase = self.session.phase_for(underlying)
        if phase is SessionPhase.PRE_OPEN:
            # 동시호가: 체결이 없으므로 기초 **예상등락률**(UYS) 우선 (문서 §1 동시이론가)
            exp_rate = self.stock_exp_change_pct.get(underlying)
            return theory_regular(inputs, exp_rate if exp_rate is not None else rate_krx)
        if phase is SessionPhase.REGULAR:
            return theory_regular(inputs, rate_krx)
        base_close = self.trades.get((underlying, Instrument.KR_STOCK, "krx"))
        base_after = self.trades.get((underlying, Instrument.KR_STOCK, "uni"))
        return theory_after(inputs, rate_krx, base_close, base_after)

    async def _startup_queries(self) -> None:
        """시동 일괄 조회를 순서대로 — ETF 이론가 입력 → 초기 가격 → 환율 폴링."""
        await self._load_etf_refs()
        await self._seed_prices()
        await self._fx_loop()

    @staticmethod
    async def _guarded_ws(name: str, run: Coroutine[Any, Any, None]) -> None:
        """WS 하나가 죽어도 전체를 멈추지 않는다 — 예: KP_MODE=live인데 선물 키가
        모의뿐이면 선물 WS만 실패(토큰 불일치). 경고만 남기고 그 채널 없이 계속."""
        import logging

        try:
            await run
        except Exception:  # noqa: BLE001 - 채널 단위 격리
            logging.getLogger("kp_arb.bootstrap").exception(
                "%s WS 중단 — 해당 채널 없이 계속", name
            )

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
    near_month = select_near_month(await gateway.fetch_futures_master())
    futures_symbols = {u: sh for u, (sh, _) in near_month.items()}
    futures_expiry = {u: ym for u, (_, ym) in near_month.items()}
    # 원달러선물 최근월물 (환율이론가용, DESIGN §6.1) — 실패해도 시동 계속
    fx_futures = None
    try:
        from datetime import datetime

        fx_futures = select_usd_futures(
            await gateway.fetch_commodity_master(), datetime.now()
        )
    except Exception:  # noqa: BLE001
        pass  # 환율이론가 없이 계속 (HL 괴리만 빈값)
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
        futures_expiry=futures_expiry,
        fx_futures=fx_futures,
        carry_rates=config.carry_rates,
        fees=config.fees,
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
