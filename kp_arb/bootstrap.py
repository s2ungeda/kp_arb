"""부트스트랩 — 시스템 시동 절차 (DESIGN.md §5.9 운영 모델).

시동 순서:
1) 계좌·비밀 로드(keyring/env) → 계좌별 게이트웨이 생성
2) t8401 마스터로 **선물 최근월물 코드 자동 조회**(만기 롤오버 대응)
3) **최초 REST 스냅샷 1회**: 잔고(주식/선물)·포지션·미체결 → OrderBook 초기화
3-2) **구독 전 현재가 초기값**(주식·주식선물·원달러선물·현물환율) — 실시간이 안 와도 빈칸 방지
4) WS 결선: 시세(H1_/JIF)+체결통보(SC*/O01·C01·H01) → OrderBook·SessionService
   — 이후는 실시간 이벤트가 기본(체결 대기 폴링 없음)
같은 스냅샷(`refresh_snapshot`)은 온디맨드(추후 UI 조회 버튼)로 재호출 가능.

계좌 통보는 접속 토큰의 계좌 것만 오므로 **계좌별 WS 2개**(주식/선물옵션)를 쓴다(실측).
HL 게이트웨이는 슬롯만 예비(라이브 결선 시 추가).

수동 실행(모의 시동 확인): ``python -m kp_arb.bootstrap [초]``
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from . import order_log
from .config import CarryRates, FeeRates, LSAccounts
from .disparity import (
    PairBoard,
    disp,
    est_price,
    est_side_disp,
    maker_price_for_spread,
    net_entry,
    net_exit,
    pair_spread,
)
from .domain.enums import Account, Instrument, Side, Underlying, Venue
from .domain.models import InstrumentInfo, OrderIntent, Position, Quote
from .engine import ArbEngine
from .etf_theory import EtfTheoryInputs, theory_after, theory_regular
from .fx_auction import FxAuctionController, FxAuctionSettings, HedgeAction
from .gateways.base import HLGateway
from .gateways.hl import Mark
from .gateways.hl_ws import HLWebSocketClient, OrderUpdate
from .gateways.ls import LSApiGateway, OrderGoneError
from .gateways.ls_rest import RestError
from .gateways.ls_ws import (
    ExpectedPrice,
    Fill,
    LSWebSocketClient,
    OrderEvent,
    TradeTick,
)
from .limits import DailyFilled, DailyLimitExceeded, would_exceed_daily_limit
from .order_book import OrderBook, TrackedOrder
from .risk import RiskManager, RiskState
from .session import reference_instrument
from .session_service import FUTURES_MARKET, SessionService
from .strategy.base import Strategy
from .theory import (
    carry_theory,
    days_to_expiry,
    in_time_window,
    is_rolled,
    parse_hhmm,
    select_usd_futures_months,
)
from .ticks import ceil_to_tick, floor_to_tick, maker_cap, tick_for
from .ws_status import WsStatus

# HL 발주가 통신 오류로 끝난 뒤 cloid를 기억해 두는 유예(초) — 살아 있는 주문의 늦은 통보를 받아
# 주고, 끝에 orderStatus(cloid)로 한 번 더 확인한다(DESIGN §HL cloid ③, 검토 2026-09-11 §A).
HL_PENDING_GRACE_S = 30.0


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


def select_months(
    rows: list[dict[str, object]], count: int = 2, now: datetime | None = None,
) -> dict[Underlying, list[tuple[str, int]]]:
    """t8401 마스터 행에서 underlying별 **근·차근 월물**(만기 오름차순 count개) — [(코드, YYYYMM)].

    §5.11(2026-09-03): 롤 지난 월물(최종거래일 15:45 이후)은 제외한다 — 기존 select_near_month는
    이 필터가 없어 만기일 저녁 재시동 시 만기월물을 "근"으로 잡았다. 원달러선물
    select_usd_futures_months와 같은 모양. 순수 로직(now 주입).
    """
    moment = now if now is not None else datetime.now()
    base_to_underlying = {f"A{u.krx_code}": u for u in Underlying}
    found: dict[Underlying, dict[int, str]] = {}
    for row in rows:
        underlying = base_to_underlying.get(str(row.get("basecode", "")))
        parts = str(row.get("hname", "")).split()
        if underlying is None or len(parts) < 3 or parts[-2] != "F":
            continue  # 대상 아님 또는 스프레드(SP)
        yyyymm = parts[-1]
        if not (len(yyyymm) == 6 and yyyymm.isdigit()):
            continue
        ym = int(yyyymm)
        if is_rolled(ym, "EQ", moment):
            continue  # 만기 지난 월물
        found.setdefault(underlying, {}).setdefault(ym, str(row.get("shcode", "")))
    return {u: [(code, ym) for ym, code in sorted(months.items())][:count]
            for u, months in found.items()}


FX_SPOT_SILENT_S = 600.0  # 현물환율(CUR) 무수신 → 하나고시 대체 기준(사용자 확정 2026-09-04: 10분)


def fx_spot_backup_due(last_rx: float, now: float, silent_s: float = FX_SPOT_SILENT_S) -> bool:
    """하나은행 고시환율로 대체할 때인가 — CUR을 한 번도 못 받았거나(last_rx=0) silent_s 넘게
    조용할 때만. 마지막 CUR 체결가는 실제 거래가라 잠시 뜸해도 고시환율보다 낫다. 순수 로직."""
    if last_rx == 0.0:
        return True
    return now - last_rx > silent_s


def startup_symbol_error(
    expected: Iterable[Underlying],
    futures_symbols: Mapping[Underlying, str],
    fx_months: Sequence[tuple[str, int]],
    next_futures_symbols: Mapping[Underlying, str],
) -> str | None:
    """시동 '종목' 로드 판정 — 취급 종목마다 주식선물 **근·차근 월물이 모두** 있고, 원달러선물
    월물도 있어야 한다(없으면 선물 시세·FX 대응주문이 조용히 안 나감). 차근 누락도 실패
    (사용자 정정 2026-09-03, §5.11). 실패 작업명 또는 None. 순수 로직."""
    problems: list[str] = []
    missing = [u for u in expected if u not in futures_symbols]
    if missing:
        problems.append("주식선물 근월물 없음: " + ", ".join(u.value for u in missing))
    missing_next = [u for u in expected if u not in next_futures_symbols]
    if missing_next:
        problems.append("주식선물 차근월물 없음: " + ", ".join(u.value for u in missing_next))
    if not fx_months:
        problems.append("원달러선물 월물 없음")
    if not problems:
        return None
    return "종목(" + "; ".join(problems) + ")"


class HLAmendForbidden(RuntimeError):
    """HL 정정 금지 — 취소 후 신규로 대체한다.

    HL의 원자적 modify는 크로싱 시 원주문을 잃을 수 있어(취소는 되고 신규는 거부),
    어떤 경우에도 HL 주문은 정정하지 않는다(수동·peg·전략 전부). amend_price가 유일한
    정정 라우팅 지점이라 여기서 HL을 하드 거부하면 어느 경로로 와도 막힌다.
    """


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
        futures_expiry: dict[tuple[Underlying, Instrument], int] | None = None,
        next_futures_symbols: dict[Underlying, str] | None = None,
        fx_futures: tuple[str, int] | None = None,
        fx_months: list[tuple[str, int]] | None = None,
        carry_rates: CarryRates | None = None,
        fees: FeeRates | None = None,
        fx_spot_window: tuple[str, str] = ("07:00", "18:10"),
        board_ref_qty: int = 1,
    ) -> None:
        self._gw = gateway
        # 취급 종목코드 (공개 — UI/도구가 상품 가용성 판단에 사용. 예: 현대차 ETF 없음)
        self.futures_symbols = dict(futures_symbols or {})            # 주식선물 근월물 코드
        self.next_futures_symbols = dict(next_futures_symbols or {})  # 차근월물 코드(§5.11)
        self.etf_symbols = dict(etf_symbols or {})
        # 만기 YYYYMM (캐리 잔존일용) — (종목, 근|차근)별
        self.futures_expiry: dict[tuple[Underlying, Instrument], int] = dict(futures_expiry or {})
        # 원달러선물 (shcode, 만기YYYYMM) — 최근월물(환율이론가 기준, bootstrap_live 조회).
        self._fx_futures = fx_futures
        # 구독할 원달러선물 월물 전체(근·차근, §9.1) — 없으면 최근월물 하나로 폴백.
        self._fx_months = list(fx_months) if fx_months else (
            [fx_futures] if fx_futures is not None else [])
        if self._fx_futures is None and self._fx_months:
            self._fx_futures = self._fx_months[0]
        self.fx_futures_price: dict[str, float] = {}  # 월물코드 → 최근 현재가(근·차근)
        # 월물코드 → (매수1호가, 매도1호가) — 자동M 환진입가(§9a: 진입 −환은 매수1호가)
        self.fx_futures_quote: dict[str, tuple[float, float]] = {}
        # 캐리 이론가 연이자율·왕복 수수료 — config.yaml 조정 대상
        self._carry = carry_rates if carry_rates is not None else CarryRates()
        self._fees = fees if fees is not None else FeeRates()
        self._board_ref_qty = board_ref_qty  # CSV 분포 기록 기준 수량(화면 없을 때 est용)
        # 환율이론가(원달러선물 현물환산, DESIGN §6.1) — WS(FC9/DC0) 실시간 + 예비 조회 갱신.
        self.usdkrw_theory: float | None = None
        self.usdkrw_futures: float | None = None  # 원달러선물 현재가 원값(표시용)
        # 외환현물(주간 07:00~18:10 HL 환산용 — 엑셀 시세!N6/O6, 2026-08-21) + 사용 시간대
        self.usdkrw_spot: float | None = None
        self._fx_spot_ts = 0.0  # 마지막 LS 현물환율(CUR) 수신 시각 — Naver 백업 억제용
        self.usdkrw_spot_src: str | None = None  # 현물환율 출처 "LS"|"하나고시" — 상태줄 표시
        self._fx_spot_window = (parse_hhmm(fx_spot_window[0]), parse_hhmm(fx_spot_window[1]))
        self._hl = hl_gateway
        self._hl_ws = hl_ws
        # HL cloid → 발주 의도(응답 대기 중). 응답보다 먼저 온 orderUpdates의 cloid로 oid를 식별해
        # 즉시 장부에 등록한다(DESIGN §HL cloid ①). 식별 시 (cloid, oid)를 아래 훅에 알린다.
        self._hl_pending: dict[str, OrderIntent] = {}
        self.on_hl_identified: list[Callable[[str, str], None]] = []
        # 발주가 통신 오류로 끝났지만 주문이 들어갔을 수 있는 cloid → (의도, 만료 monotonic).
        # 유예 안에 그 cloid의 통보가 오면 살아 있는 주문으로 등록(§HL cloid ③, 검토 09-11 §A).
        self._hl_failed: dict[str, tuple[OrderIntent, float]] = {}
        self.hl_pending_grace_s = HL_PENDING_GRACE_S  # 테스트에서 줄인다
        self.order_book = order_book
        self.session = session
        self._stock_ws = stock_ws
        self._deriv_ws = deriv_ws
        # 최신 호가판 보관소 — 주식 10호가·선물 5호가 등 다단계 포함(Quote.bids/asks).
        # 키: (underlying, instrument, market["krx"|"nxt"|"hl"]). 모니터·페깅·전략이 공유.
        self.quotes: dict[tuple[Underlying, Instrument, str], Quote] = {}
        # 최신 체결가 보관소 — 시장별(krx/nxt/hl/uni). ETF 이론가의 기초가(KRX)에도 사용.
        self.trades: dict[tuple[Underlying, Instrument, str], float] = {}
        # HL 마크(+오라클)·펀딩률 최신값 — 수동주문창 잔고표(B). WS로 실시간 갱신.
        self.hl_mark: dict[Underlying, Mark] = {}
        self.hl_funding_rate: dict[Underlying, float] = {}  # 예정(다음) 펀딩률
        self.hl_funding_prev: dict[Underlying, float] = {}  # 직전 확정 펀딩률(REST 조회)
        # 배경 상시 태스크(괴리 CSV 등) — wait()가 기다리는 스트리밍 태스크와 분리, stop()이 취소.
        self._aux_tasks: list[asyncio.Task[None]] = []
        # HL 포지션 상세(마진·누적펀딩·청산가·레버리지) — clearinghouseState에서 refresh 때 채움.
        self.hl_detail: dict[Underlying, dict[str, Any]] = {}
        # 종목정보(틱·승수·szDecimals·maxLeverage·만기) — 시동 시 1회 조회·보관 (§5.10).
        self.instruments: dict[tuple[Underlying, Instrument], InstrumentInfo] = {}
        # 시동 초기화(종목→잔고→포지션→주문) 중 실패한 작업명 — 있으면 /state로 알려
        # 메인창이 "…재접속하세요" 팝업 후 종료시킨다. None=전부 정상.
        self.startup_load_error: str | None = None
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
        self.fills: deque[dict[str, Any]] = deque(maxlen=200)  # 체결내역(최신 우선, 주문리스트)
        self.cancels: deque[dict[str, Any]] = deque(maxlen=200)  # 취소내역(최신 우선)
        # 원달러선물 동시호가 대응주문(§9.1, DESIGN-fx-auction) — 감시 컨트롤러 + 발주 로그.
        self.fx_hedges: deque[dict[str, Any]] = deque(maxlen=200)
        self.fx_auction = FxAuctionController(
            resolve_underlying=self._resolve_fut_code, now=self._now_hms,
            targets={Underlying.SAMSUNG, Underlying.SK_HYNIX})
        self._hl_filled = DailyFilled()  # HL 당일 체결액(USDC) — 일일 한도용(DESIGN-settings §1)
        self.hl_daily_limit_usdc = 0.0   # HL 일일 한도(USDC, 0=무제한) — 코어가 설정에서 주입
        # 알람용 이벤트 카운터(메인창이 증가 감지 → 사운드) — DESIGN-settings §2·4.
        self.fill_seq = 0    # 체결마다 +1
        self.error_seq = 0   # 발주 거부·실패마다 +1
        # 체결내역·당일누적·seq는 **실제 체결 적용 시점**(order_book.on_fill_applied)에 1회씩.
        # 통보(on_fill) 시점이면 taker 즉시체결은 통보가 track보다 빨라 누락(실증 2026-08-20).
        order_book.on_fill_applied.append(self._record_fill)
        order_book.on_fill_applied.append(self._accumulate_hl_fill)
        order_book.on_fill_applied.append(lambda _o, _q, _p, _fid: self._bump_fill_seq())
        self._tasks: list[asyncio.Task[None]] = []
        self._bg: set[asyncio.Task[None]] = set()  # 재연결 재동기 등 백그라운드 작업(GC 방지)
        self._hl_refresh_pending = False  # 체결 후 HL 재조회 예약 여부(디바운스 코얼레싱)

    # --- 스냅샷 (최초 실행 + 온디맨드/UI 조회 버튼) ---

    async def refresh_snapshot(self, scope: set[Account | None] | None = None) -> None:
        """거래소 실제값(REST)으로 장부 재동기. scope=None이면 전체(시동·수동 '적'),
        지정하면 그 계좌(HL은 None)만 조회·갱신 — 재연결은 끊긴 시장만(사용자 2026-09-09)."""
        import logging

        positions: list[Position] = []
        balances: dict[Account, float] = {}
        open_orders: list[TrackedOrder] = []
        reconciled: set[Account | None] = set()  # 조회 성공한 계좌만 phantom 정리 대상
        ls_accounts = [a for a in (Account.KR_STOCK, Account.KR_DERIV)
                       if scope is None or a in scope]
        for account in ls_accounts:
            # 실계좌 환경 편차(선물 계좌 없음, 형식 거부 등)로 한 계좌 조회가
            # 실패해도 시동을 멈추지 않는다 — 해당 계좌만 빼고 계속.
            try:
                balances[account] = await self._gw.get_balance(account)
                positions.extend(await self._gw.get_positions(account))
                open_orders.extend(await self._gw.get_open_orders(account))
                # 실제 조회가 되는 계좌만 유령 정리 대상. 선물 미체결 TR은 미확인이라 빈 결과가
                # 오는데, 이를 "조회 성공"으로 보고 걸린 선주문(#20851)을 지웠다(실측 2026-09-09) —
                # 그 뒤 체결 통보가 미아가 되고 자동M은 없는 주문 취소를 되풀이했다.
                if self._gw.open_orders_supported(account):
                    reconciled.add(account)
            except RestError:
                logging.getLogger("kp_arb.bootstrap").warning(
                    "%s 계좌 스냅샷 실패 — 이 계좌 없이 계속", account.value, exc_info=True
                )
                balances.setdefault(account, 0.0)
        if self._hl is not None and (scope is None or None in scope):
            hl_pos, self.hl_detail = await self._hl.get_positions_and_details()  # 1회 조회
            positions.extend(hl_pos)
            open_orders.extend(await self._hl.get_open_orders())
            reconciled.add(None)  # HL 주문(계좌 없음) 조회 성공
            # 미보유 종목은 clearinghouse가 레버리지를 안 줘 캡션이 기본값(5x)에 멈춘다 —
            # activeAssetData로 계좌 설정 레버리지를 읽어 보정(보유 종목 값은 유지). §D
            for u, lev in (await self._hl.get_leverage_settings()).items():
                d = self.hl_detail.setdefault(u, {})
                d.setdefault("leverage", lev["leverage"])
                d.setdefault("leverage_cross", lev["leverage_cross"])
        # 조회 실패 계좌의 살아있는 미체결이 빈 스냅샷 탓에 지워지지 않도록 성공 계좌만 정리.
        # scope가 있으면 그 계좌의 포지션·잔고만 갈아 끼운다(다른 시장 장부는 그대로).
        self.order_book.load_snapshot(
            positions=positions, balances=balances, open_orders=open_orders,
            reconcile_accounts=reconciled, scope=scope,
        )

    def _record_fill(self, order: TrackedOrder, qty: float, price: float,
                     fill_id: str) -> None:
        """체결내역 보관(주문 리스트 표시용) — **실제 체결 적용 시점**에 1회.

        order_book.on_fill_applied에서 불린다 — 즉시체결(apply_place_fill)·대기체결 모두
        이 지점을 지나고, 흡수된 재통보는 안 지난다 → 정확히 1회, taker도 타이밍 경합 없음.
        """
        import time as _t

        it = order.intent
        self.fills.appendleft({
            "time": _t.strftime("%H:%M:%S"),        # 체결시각
            "order_id": order.order_id,             # 주문번호(주문리스트 표시)
            "accept_time": order.placed_at,         # 접수시각(원주문)
            "underlying": it.underlying.value, "instrument": it.instrument.value,
            "side": it.side.value,
            "qty": qty, "price": price,                     # 체결량·체결가
            "order_qty": it.qty, "order_price": it.price,   # 원주문 수량·주문가
            "source": it.source,                            # 출처(주문 리스트 '출처' 칸·필터)
        })

    def _record_cancel(self, order: TrackedOrder) -> None:
        """취소내역 보관(주문 리스트 '취소' 행) — 원주문 정보 기준(주문번호·주문가·주문수량·
        접수시각). 취소시각(time)은 보관하되 현재 열엔 미표시."""
        import time as _t

        it = order.intent
        self.cancels.appendleft({
            "time": _t.strftime("%H:%M:%S"),        # 취소시각(현재 열엔 미표시)
            "order_id": order.order_id,             # 주문번호
            "accept_time": order.placed_at,         # 접수시각
            "underlying": it.underlying.value, "instrument": it.instrument.value,
            "side": it.side.value, "qty": it.qty, "price": it.price,  # 주문수량·주문가
            "source": it.source,                    # 출처(주문 리스트 '출처' 칸·필터)
        })

    # --- 원달러선물 동시호가 대응주문 (§9.1, DESIGN-fx-auction) ---

    def _now_hms(self) -> str:
        import time
        return time.strftime("%H:%M:%S")

    def _today(self) -> str:
        import time
        return time.strftime("%Y%m%d")  # 로컬 자정 기준 — 날짜 바뀌면 당일 누적 리셋

    def hl_daily_filled_today(self) -> float:
        """HL 당일 체결액(USDC) — 스냅샷·표시용."""
        return self._hl_filled.total(self._today())

    def set_hl_daily_limit(self, usdc: float) -> None:
        """HL 일일 체결액 한도(USDC) 반영 — 코어가 설정 변경·시동 시 호출."""
        self.hl_daily_limit_usdc = max(0.0, usdc)

    def set_carry_rates(self, fx: float, eq: float) -> None:
        """이론가 연이자율 반영(환율=fx / 주식선물=eq) — 코어가 공통설정에서 주입.

        이후 환율이론가(usdkrw_theory)·주식선물 이론가가 이 값으로 계산된다.
        """
        self._carry = CarryRates(stock_futures=eq, fx=fx)
        # 환율이론가는 선물가 수신 때 계산해 둔 값이라, 금리만 바뀌면 다음 선물 틱까지 옛 금리로
        # 남는다(2026-09-03 실측: 설정 0.4%로 바꿔도 16시 이후 1.0% 값 유지, 엑셀과 0.40원 차이).
        # 저장된 최근월물 가격으로 즉시 다시 계산한다. (주식선물 이론가는 호출 때 계산하므로 무관)
        if self._fx_futures is not None:
            code = self._fx_futures[0]
            price = self.fx_futures_price.get(code)
            if price is not None:
                self._apply_fx_price(code, price)

    def _apply_fx_quote(self, code: str, bid: float, ask: float) -> None:
        self.fx_futures_quote[code] = (bid, ask)

    def futures_halted(self) -> bool:
        """선물시장(5) 정지 오버레이(사이드카·서킷) 여부 — 자동M 판정용(exec §8)."""
        return self.session.halt_for(FUTURES_MARKET) is not None

    def fx_entry_rate(self, side: Side) -> float | None:
        """자동M 환진입가(§9a) — 최근월물 원달러선물의 매수1호가(HL 매도, −환) / 매도1호가(HL 매수).
        호가가 없으면 현물환율(LS CUR, 없으면 하나고시 백업값), 그것도 없으면 선물 직전 체결가,
        그것도 없으면 None(사용자 확정 2026-09-14 — 옛 순서는 호가 → 직전 체결가)."""
        code = self._fx_futures[0] if self._fx_futures is not None else None
        quote = self.fx_futures_quote.get(code) if code is not None else None
        if quote is not None:
            return quote[0] if side is Side.SELL else quote[1]
        if self.usdkrw_spot is not None:
            return self.usdkrw_spot
        return self.fx_futures_price.get(code) if code is not None else None

    def set_fx_spot_window(self, start: str, end: str) -> None:
        """현물환율(CUR) 사용 시간대 반영("HH:MM") — 코어가 공통설정에서 주입(사용자 입력,
        2026-09-04). usdkrw_effective·백업 조회가 다음 호출부터 이 창을 쓴다.
        형식 오류는 ValueError."""
        self._fx_spot_window = (parse_hhmm(start), parse_hhmm(end))

    def _hl_order_notional(self, intent: OrderIntent) -> float:
        """HL 주문 금액(USDC) = |수량| × 가격. 시장가(가격 없음)는 마크가로 추정."""
        px = intent.price
        if px is None:
            mark = self.hl_mark.get(intent.underlying)
            px = mark.price if mark is not None else 0.0
        return abs(intent.qty) * (px or 0.0)

    def _accumulate_hl_fill(self, order: TrackedOrder, qty: float, price: float,
                            fill_id: str) -> None:
        """HL 체결이면 당일 체결액(USDC)에 |수량|×체결가 누적 — 실제 적용 시점 1회(한도용)."""
        if order.intent.venue is Venue.HYPERLIQUID:
            self._hl_filled.add(self._today(), abs(qty) * price)

    def _bump_fill_seq(self) -> None:
        self.fill_seq += 1  # 알람용 — 메인창이 증가를 감지해 체결 사운드 재생

    def bump_error(self) -> None:
        """발주 거부·실패 알림 — 메인창이 증가를 감지해 에러 사운드 재생(DESIGN-settings §2)."""
        self.error_seq += 1

    @property
    def futures_codes(self) -> dict[tuple[Underlying, Instrument], str]:
        """(종목, 근|차근) → 주식선물 코드 — 근·차근 두 표를 한 눈에(§5.11)."""
        out = {(u, Instrument.KR_STOCK_FUTURE): c for u, c in self.futures_symbols.items()}
        out.update({(u, Instrument.KR_STOCK_FUTURE_NEXT): c
                    for u, c in self.next_futures_symbols.items()})
        return out

    def _resolve_fut_code(self, code: str) -> Underlying | None:
        """주식선물 종목코드 → Underlying(감시 대상 판정). 근·차근 코드 모두 매칭 —
        동시호가 원달러 대응주문은 차근 신규주문도 헤지 대상(§5.11)."""
        for (u, _inst), sh in self.futures_codes.items():
            if sh == code:
                return u
        return None

    def fx_futures_codes(self) -> list[str]:
        """구독 중인 원달러선물 월물 코드(근·차근 순) — 화면 콤보용."""
        return [code for code, _ in self._fx_months]

    def start_fx_auction(self, settings: FxAuctionSettings) -> None:
        """대응주문 감시 시작(화면 실행)."""
        self.fx_auction.start(settings)

    def stop_fx_auction(self) -> None:
        """대응주문 감시 정지(화면 중지)."""
        self.fx_auction.stop()

    async def _place_fx_hedge(self, action: HedgeAction) -> None:
        """대응주문 실제 발주(KR_FX) + 화면용 로그. 발주 실패해도 감시는 계속."""
        import logging
        rec = {"time": self._now_hms(), "code": action.fx_code,
               "side": action.side.value, "qty": action.qty, "price": action.price,
               "src": action.source_order_id}
        try:
            oid = await self._gw.place_fx_futures(
                action.fx_code, action.side, action.qty, action.price)
        except Exception as exc:  # noqa: BLE001 - 발주 실패/거부는 로그만, 감시 지속
            # 거부 사유(rsp_cd/rsp_msg)를 한 줄에 인라인(트레이스백은 그 아래).
            logging.getLogger("kp_arb.fx_auction").exception(
                "원달러선물 대응주문 실패/거부 %s — %s", action, exc)
            self.fx_hedges.appendleft(
                {**rec, "order_id": "", "status": "실패", "err": str(exc)[:120]})
            return
        self.fx_hedges.appendleft({**rec, "order_id": oid, "status": "접수"})

    _HL_FILL_DEBOUNCE_S = 0.5  # 몰린 체결을 합쳐 조회 1회로 (사용자 확정)

    def _schedule_hl_refresh(self) -> None:
        """HL 체결 발생 → HL 파생값(마진·청산가·펀딩·포지션) 재조회 예약(디바운스).

        이미 예약돼 있으면 무시 — 체결이 폭주해도 창(0.5s)당 조회 1회로 합친다(코얼레싱).
        잔고·평단·PNL은 on_fill이 이미 즉시 갱신하고, 여기선 조회로만 얻는 값만 보정.
        """
        if self._hl is None or self._hl_refresh_pending:
            return
        self._hl_refresh_pending = True
        task = asyncio.create_task(self._refresh_hl_after_fill())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _refresh_hl_after_fill(self) -> None:
        import logging

        try:
            await asyncio.sleep(self._HL_FILL_DEBOUNCE_S)
        finally:
            self._hl_refresh_pending = False  # 조회 전에 풀어, 조회 중 온 체결은 재예약
        if self._hl is None:
            return
        try:
            hl_pos, self.hl_detail = await self._hl.get_positions_and_details()
            self.order_book.replace_positions(hl_pos, instrument=Instrument.HL_PERP)
        except Exception:  # noqa: BLE001 - 조회 실패가 수신 루프를 죽이지 않게
            logging.getLogger("kp_arb.core").warning(
                "체결 후 HL 재조회 실패 — 다음 체결/적에 재시도", exc_info=True)

    def _on_hl_order_update(self, upd: OrderUpdate) -> None:
        """HL 주문상태 변화 → OrderBook 실시간 정합. 취소·거부 계열만 반영(체결은 userFills).

        외부(홈페이지) 취소·자동취소(post-only/reduce-only/마진부족 등)도 즉시 반영돼,
        조회(get_open_orders) 없이 호가창 유령 주문표시가 안 생긴다.
        """
        self._identify_hl_order(upd)  # 응답 전 통보면 cloid로 먼저 등록(아래 취소 처리가 주인을 앎)
        if not upd.is_terminal_cancel:
            return  # open/triggered(살아있음)·filled(userFills 담당)은 여기서 처리 안 함
        order = (self.order_book.on_reject(upd.oid) if upd.is_rejected
                 else self.order_book.on_cancel(upd.oid))
        log = order_log.logger_for(Venue.HYPERLIQUID)
        if order is not None:
            if upd.ended_with_remainder:  # 거래소가 잔량을 버리고 끝냄(증거금 한도 등, 실측 09-07)
                log.warning("주문종료(filled, 잔량 %g 버림) #%s — 거래소가 끝냄, 미체결분 정리",
                            upd.sz, upd.oid)
            else:
                log.info("주문종료(%s) #%s — OrderBook 제거(실시간)", upd.status, upd.oid)
            self._record_cancel(order)  # 취소내역(주문 리스트 '취소' 행)
        else:
            # 아직 track 전(발주 응답보다 WS가 먼저) — OrderBook이 보관했다가 track 뒤 replay로 반영
            log.info("주문종료(%s, 잔량 %g) #%s — 추적 전 도착, 발주 처리 뒤 반영(또는 외부 주문)",
                     upd.status, upd.sz, upd.oid)

    def _identify_hl_order(self, upd: OrderUpdate) -> None:
        """응답 전 식별(DESIGN §HL cloid ①) — 통보의 cloid가 발주 응답을 기다리는 우리 주문이면
        oid를 즉시 장부에 등록하고, 훅(자동M 등)에 알린 뒤 먼저 온 체결(userFills)을 반영한다.
        실측 2026-09-11 10:18:35: 잔량 버림 종료 통보가 응답보다 49ms 먼저 와 자동M이 놓쳤다.
        발주 실패로 처리했던 cloid(유예 중)의 통보면 살아 있는 주문 — 경고·알람과 함께 등록(③)."""
        import time as _t

        if not upd.cloid or self.order_book.order(upd.oid) is not None:
            return
        intent = self._hl_pending.get(upd.cloid)
        if intent is not None:
            self._adopt_identified(upd.cloid, upd.oid, intent, late=False, how=upd.status)
            return
        failed = self._hl_failed.pop(upd.cloid, None)
        if failed is None:
            return
        intent, expiry = failed
        if _t.monotonic() > expiry:
            return  # 유예 지남 — 재동기(재연결·'적'·재시동)가 일반 주문으로 잡는다
        self._adopt_identified(upd.cloid, upd.oid, intent, late=True, how=upd.status)

    def _adopt_identified(self, cloid: str, oid: str, intent: OrderIntent, *,
                          late: bool, how: str) -> None:
        """cloid로 식별된 주문을 장부에 등록 — 취소 문맥 선등록, 훅 통지, 보관된 체결 재생."""
        assert self._hl is not None
        self.order_book.track(oid, intent)
        self._hl.note_identified(oid, intent)  # 응답 전 취소가 'unknown order_id'가 되지 않게
        log = order_log.logger_for(Venue.HYPERLIQUID)
        if late:
            log.warning("HL 발주 실패로 처리했던 주문 #%s(cloid %s)가 살아 있음(%s) — 장부에 등록. "
                        "자동M 장부(체결차)·체결 내역 확인 필요", oid, cloid, how)
            self.error_seq += 1  # 메인창 알람
        else:
            log.info("HL 주문 식별(응답 전) #%s ← cloid %s (%s)", oid, cloid, how)
        for cb in self.on_hl_identified:
            cb(cloid, oid)
        self.order_book.replay_pending(oid)

    def _hold_failed_hl(self, cloid: str, intent: OrderIntent) -> None:
        """발주 실패(통신 오류·조회 실패)한 cloid를 유예 목록에 두고, 유예 끝에 다시 조회한다."""
        import time as _t

        self._hl_failed[cloid] = (intent, _t.monotonic() + self.hl_pending_grace_s)
        order_log.logger_for(Venue.HYPERLIQUID).info(
            "HL 발주 실패 cloid %s — %.0f초 유예(통보가 오면 살아 있는 주문으로 등록, 끝에 재조회)",
            cloid, self.hl_pending_grace_s)
        task = asyncio.create_task(self._late_hl_recheck(cloid))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _late_hl_recheck(self, cloid: str) -> None:
        """유예 끝 — 통보가 안 왔으면 orderStatus(cloid)로 마지막 확인(웹소켓까지 끊겼던 경우)."""
        await asyncio.sleep(self.hl_pending_grace_s)
        failed = self._hl_failed.pop(cloid, None)
        if failed is None or self._hl is None:
            return  # 이미 통보로 식별됨
        intent, _ = failed
        try:
            oid = await self._hl.lookup_by_cloid(cloid)
        except Exception as exc:  # noqa: BLE001 - 조회 실패면 안 들어간 것으로 본다(재동기가 안전망)
            order_log.logger_for(Venue.HYPERLIQUID).warning(
                "HL 유예 끝 재조회 실패 cloid %s — %s", cloid, exc)
            return
        if oid is None:
            order_log.logger_for(Venue.HYPERLIQUID).info(
                "HL 유예 끝 cloid %s — 통보·조회 모두 없음(주문 안 들어간 것으로 봄)", cloid)
            return
        if self.order_book.order(oid) is None:
            self._adopt_identified(cloid, oid, intent, late=True, how="유예 끝 orderStatus 조회")

    def _on_ws_reconnect(self, label: str) -> None:
        """WS 재연결 후(동기 콜백) — 끊긴 동안 놓친 체결/외부거래를 반영하러 OrderBook을
        거래소 실제값으로 재동기(백그라운드). 수신 루프를 막지 않게 태스크로 던진다. Phase 8-4."""
        import logging

        logging.getLogger("kp_arb.core").warning(
            "%s WS 재연결 — 그 시장만 포지션/잔고 재동기", label)
        task = asyncio.create_task(self._resync_after_reconnect(label))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # 재연결 채널 → 재동기 범위(계좌; HL은 None). 끊긴 시장만 다시 조회한다 — HL 재연결이
    # LS 장부까지 갈아 끼우다 선물 선주문을 지웠던 사고(2026-09-09) 방지.
    _RECONNECT_SCOPE: dict[str, set[Account | None]] = {
        "주식": {Account.KR_STOCK}, "선물": {Account.KR_DERIV}, "HL": {None}}

    async def _resync_after_reconnect(self, label: str) -> None:
        import asyncio as _asyncio
        import logging

        from . import alert

        try:
            await self.refresh_snapshot(scope=self._RECONNECT_SCOPE.get(label))
        except Exception:  # noqa: BLE001 - 재동기 실패가 수신 루프를 죽이지 않게
            logging.getLogger("kp_arb.core").warning(
                "%s 재연결 재동기 실패", label, exc_info=True)
            return
        # 알림은 블로킹(HTTP)이라 별도 스레드로 — 미설정이면 조용히 무시.
        await _asyncio.to_thread(alert.notify, f"{label} WS 재연결·재동기 완료", "warn")

    async def price_snapshots(self) -> dict[tuple[Underlying, Instrument], float]:
        """취급 전 종목 현재가 1회 조회(창 오픈 시 초기 표시용 — 마감 후엔 종가)."""
        return await self._gw.get_price_snapshots()

    # --- 주문 (등록까지 한 번에 — 이후 상태는 이벤트로만) ---

    def new_hl_cloid(self) -> str | None:
        """HL 클라이언트 주문번호 — 호출자가 발주 전에 만들어 두고 place(cloid=)로 넘긴다."""
        return self._hl.new_cloid() if self._hl is not None else None

    async def place(self, intent: OrderIntent, *, cloid: str | None = None) -> str:
        """venue 라우팅 주문 + OrderBook 등록. 이후 상태는 이벤트로만.

        HL은 발주 즉시체결(크로싱)이면 응답에 이미 체결이 실려온다 — userFills를 놓쳐도
        미체결로 남지 않게 그 체결을 바로 반영한다(중복은 OrderBook 초과체결 가드가 무시).
        HL cloid(없으면 여기서 생성)는 응답 전 통보로 주문번호를 식별하는 데 쓴다(§HL cloid).
        """
        if intent.venue is Venue.LS:
            order_id = await self._gw.place_order(intent)
            self.order_book.track(order_id, intent)
            self.order_book.replay_pending(order_id)  # track 전에 온 이벤트 반영(역전 대비)
            return order_id
        if self._hl is None:
            raise RuntimeError("HL gateway not configured")
        # HL 일일 체결액 한도(DESIGN-settings §1) — 모든 HL 주문(수동·전략) 공통 하드블록.
        notional = self._hl_order_notional(intent)
        filled = self._hl_filled.total(self._today())
        if would_exceed_daily_limit(filled, notional, self.hl_daily_limit_usdc):
            raise DailyLimitExceeded(
                f"HL 일일 한도 초과 — 당일 {filled:,.0f} + 주문 {notional:,.0f} "
                f"> 한도 {self.hl_daily_limit_usdc:,.0f} USDC")
        if cloid is None:
            cloid = self._hl.new_cloid()
        if cloid:
            self._hl_pending[cloid] = intent
        try:
            order_id = await self._hl.place_order(intent, cloid=cloid)
        except Exception:
            if cloid:
                self._hl_pending.pop(cloid, None)
                # 거부가 확실한 경우도 섞여 있지만 구분이 어렵다 — 유예 안에 통보가 오면 그때 안다.
                self._hold_failed_hl(cloid, intent)
            raise
        if cloid:
            self._hl_pending.pop(cloid, None)
        order = self.order_book.order(order_id)
        if order is None:
            order = self.order_book.track(order_id, intent)
        else:  # 응답보다 먼저 온 통보(cloid)로 이미 등록·체결 반영됨 — 그대로 잇는다
            order_log.logger_for(Venue.HYPERLIQUID).info(
                "HL 발주 응답 #%s — 응답 전 식별로 이미 등록(체결 %g)", order_id, order.filled_qty)
        place_fill = self._hl.pop_place_fill()  # 발주 즉시체결 (수량, 평균가) | None
        if place_fill is not None:
            sz, px = place_fill
            # 응답 전 식별로 이미 반영된 체결(userFills)은 뺀 차이만 선반영 — 이중 반영 방지
            delta = sz - order.filled_qty
            if delta > 1e-9:
                fill = Fill(fill_id=f"place-{order_id}", order_id=order_id,
                            qty=delta, price=px, ts=0.0)
                # 주문 체결처리 + 포지션만 반영(미체결 잔류 방지). apply_place_fill은 선반영
                # 수량을 기록해 뒤이어 오는 userFills 재통보를 그 수량만큼 흡수한다(부분 즉시
                # 체결도 이중 반영 안 됨). 체결내역 기록·엔진 통지는 userFills가 전담.
                self.order_book.apply_place_fill(fill)
            self._schedule_hl_refresh()
        # apply_place_fill **뒤**에 replay — track 전에 온 이벤트(체결·취소 등) 반영. 겹친
        # 체결은 provisional_filled가 흡수해 이중 반영 없음(주문 역전 대비, LS·HL 공용).
        self.order_book.replay_pending(order_id)
        return order_id

    async def amend_price(
        self, order_id: str, price: float, *,
        reduce_only: bool = False, post_only: bool = False,
    ) -> str:
        """가격 정정 (LS 전용) — 새 주문번호를 등록하고 원주문은 취소 처리.

        **HL은 어떤 경우에도 정정 금지**(원자적 modify가 크로싱 시 원주문 소실) —
        HL 주문이 오면 HLAmendForbidden으로 거부한다. HL은 취소 후 신규로 대체한다
        (peg는 그렇게 동작, 수동은 화면·코어에서도 차단). 이 메서드가 유일한 정정 라우팅
        지점이라 여기서 막으면 어느 경로로 와도 HL modify가 나가지 않는다.
        LS는 CSPAT00701/CFOAT00200. 수량은 **잔량 기준** — 부분체결 후 원수량 정정 거부(01442).
        """
        order = self.order_book.order(order_id)
        if order is None:
            raise ValueError(f"unknown order {order_id}")
        if order.intent.venue is not Venue.LS:  # HL 등 — 정정 금지, 취소 후 신규로
            raise HLAmendForbidden(f"HL 정정 금지 — 취소 후 신규 (order {order_id})")
        qty = order.remaining_qty
        if qty <= 0:
            raise OrderGoneError(f"order {order_id} has no remaining qty")
        try:
            new_id = await self._gw.amend_order(order_id, qty=qty, price=price)
        except Exception as exc:  # 정정 거부/오류도 거래소별 파일에 남긴다(실린 옵션 포함)
            order_log.order_amend_rejected(
                order.intent.venue, order_id, exc, qty=qty, price=price,
                reduce_only=reduce_only, post_only=post_only)
            raise
        new_intent = order.intent.model_copy(
            update={"price": price, "qty": qty,
                    "reduce_only": reduce_only, "post_only": post_only})
        self.order_book.track(new_id, new_intent)
        self.order_book.replay_pending(new_id)  # 새 주문번호로 track 전에 온 이벤트 반영
        if new_id != order_id:
            self.order_book.on_cancel(order_id)  # 원주문은 정정으로 소멸
        return new_id

    async def cancel(self, order_id: str) -> None:
        """venue 라우팅 취소. HL은 취소 통보 채널이 없어 로컬 상태도 갱신."""
        order = self.order_book.order(order_id)
        if order is None:
            raise ValueError(f"unknown order {order_id}")
        if order.intent.venue is Venue.LS:
            # 취소수량은 장부의 남은 수량 — 원주문 수량이면 부분체결 뒤 01443 거부(실측 09-09)
            await self._gw.cancel_order(order_id, qty=order.remaining_qty)  # 상태는 통보로 전이
        else:
            assert self._hl is not None
            await self._hl.cancel_order(order_id)
            self.order_book.on_cancel(order_id)

    async def update_leverage(
        self, underlying: Underlying, leverage: int, *, is_cross: bool
    ) -> None:
        """HL 레버리지·마진모드 변경 — 수동주문창 §1-3. 성공 후 상세 재조회로 캡션 갱신."""
        if self._hl is None:
            raise RuntimeError("HL gateway not configured")
        await self._hl.update_leverage(underlying, leverage, is_cross=is_cross)
        self.hl_detail = await self._hl.get_position_details()  # 새 값 반영(포지션 있으면)

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
            import logging

            # 오타면 발주가 막힌 채 이유를 모른다 — 반드시 남긴다(DEAD 유지는 그대로).
            logging.getLogger("kp_arb.bootstrap").warning(
                "KP_SESSION_INIT=%r 알 수 없는 값 — 무시(세션 DEAD 유지, 발주 차단)", raw)

    def _mark_ws_dead(self, name: str) -> None:
        """WS 채널이 예외로 영구 중단됨 → 시동 실패와 같은 등급으로 /state에 실어 팝업.

        시세·체결통보가 끊긴 채 코어가 계속 돌면 판단·장부가 조용히 틀어진다(사용자 확정
        2026-09-03). 먼저 기록된 실패가 있으면 덮지 않는다.
        """
        if self.startup_load_error is None:
            self.startup_load_error = f"WS 채널({name}) 중단"

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
            # [임시 진단] fx-auction 감시 흐름 확인 — 선물 접수 수신·판정 근거를 한 줄로.
            if event.kind == "ack" and str(event.body.get("trcode1", "")).startswith("FO"):
                import logging as _lg

                from .fx_auction import parse_futures_ack as _pfa
                _s = self.fx_auction.settings
                _lg.getLogger("kp_arb.fx_auction").info(
                    "선물접수 code=%s→%s org=%r running=%s now=%s ack=%s settings=%s",
                    event.body.get("fnoIsuno"),
                    self._resolve_fut_code(str(event.body.get("fnoIsuno", ""))),
                    event.org_order_id, self.fx_auction.running, self._now_hms(),
                    _pfa(event.body),
                    (_s.windows, _s.price, _s.tick, _s.hedge_ratio) if _s else None)
            # 원달러선물 동시호가 대응 — 주식선물 신규주문이면 대응주문 발주(실행중일 때만).
            action = self.fx_auction.decide(
                kind=event.kind, org_order_id=event.org_order_id, body=event.body)
            if action is not None:
                import logging as _lg2
                _lg2.getLogger("kp_arb.fx_auction").info("대응 발주 결정 %s", action)
                task = asyncio.create_task(self._place_fx_hedge(action))
                self._bg.add(task)
                task.add_done_callback(self._bg.discard)

        for underlying in Underlying:
            self._stock_ws.subscribe_quotes(underlying)
            self._stock_ws.subscribe_trades(underlying)  # 현재가(S3_) + 예상체결(YS3)
        if self.futures_symbols:
            self._stock_ws.subscribe_futures_quotes(self.futures_symbols)
        if self.next_futures_symbols:  # 차근월물 호가·체결·예상체결(§5.11)
            self._stock_ws.subscribe_futures_quotes(
                self.next_futures_symbols, instrument=Instrument.KR_STOCK_FUTURE_NEXT)
        self._stock_ws.subscribe_market_status()
        self._stock_ws.subscribe_stock_fills()
        if self._fx_months:
            # 원달러선물 체결 실시간 — 주간 FC9 + 야간 DC0, 근·차근 월물 모두(§9.1). 최근월물만
            # 환율이론가로 쓰고(차근은 저장만), 예비는 _fx_loop 30초 조회. 구 FC0는 2026-06-23
            # 폐지돼 8/7~9/3 거부(10002)됐었음 — 선물계좌 토큰 WS(파생 그룹)로 등록한다.
            fx_ws = self._deriv_ws if self._deriv_ws is not None else self._stock_ws
            for code, _ in self._fx_months:
                fx_ws.subscribe_fx(code)
            fx_ws.on_fx_price.append(self._apply_fx_price)
            fx_ws.on_fx_quote.append(self._apply_fx_quote)  # 1호가 — 자동M 환진입가
        # 원달러 현물환율(CUR) 실시간 — 주간 HL 환산 본선(엑셀 LS현물CUR). Naver는 백업.
        self._stock_ws.subscribe_fx_spot()
        self._stock_ws.on_fx_spot.append(self._apply_fx_spot)
        self._stock_ws.on_quote.append(fan_quote)
        self._stock_ws.on_trade.append(fan_trade)
        self._stock_ws.on_expected.append(fan_expected)
        self._stock_ws.on_market_status.append(self.session.on_market_status)
        self._stock_ws.on_fill.append(apply_fill)
        self._stock_ws.on_order_event.append(apply_event)
        self._stock_ws.on_reconnect.append(lambda: self._on_ws_reconnect("주식"))
        if self._deriv_ws is not None:
            self._deriv_ws.subscribe_futures_fills()
            self._deriv_ws.on_fill.append(apply_fill)
            self._deriv_ws.on_order_event.append(apply_event)
            self._deriv_ws.on_reconnect.append(lambda: self._on_ws_reconnect("선물"))
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
            def store_mark(mark: Mark) -> None:  # 마크+오라클 저장(B — 잔고표)
                self.hl_mark[mark.underlying] = mark

            def store_funding(underlying: Underlying, rate: float) -> None:  # 펀딩률 저장(B)
                self.hl_funding_rate[underlying] = rate

            self._hl_ws.on_mark.append(fan_mark)
            self._hl_ws.on_mark.append(store_mark)
            self._hl_ws.on_quote.append(fan_quote)
            self._hl_ws.on_trade.append(fan_trade)
            self._hl_ws.on_funding.append(fan_funding)
            self._hl_ws.on_funding.append(store_funding)
            self._hl_ws.on_fill.append(apply_fill)  # HL 체결 → OrderBook (oid로 매칭)
            # 체결 후 마진·청산가·펀딩·(외부)포지션은 조회로만 얻음 → 디바운스 재조회(§실시간 A)
            self._hl_ws.on_fill.append(lambda _f: self._schedule_hl_refresh())
            self._hl_ws.on_order_update.append(self._on_hl_order_update)  # 취소 등 실시간
            self._hl_ws.on_reconnect.append(lambda: self._on_ws_reconnect("HL"))

    async def load_instruments(self) -> None:
        """종목정보 조회·보관 — 시동 1회 (§5.10). HL 조회 실패는 **예외로 올린다**
        (시동 필수 로드 — 사용자 확정 2026-09-03: 소수자릿수 없이는 HL 주문 수량이 어긋남).

        HL: metaAndAssetCtxs → szDecimals·maxLeverage·code (포지션 없어도 앎).
        주식선물: 코드·만기(t8401 기조회) + 승수 10 / 주식: 승수 1.
        """
        hl_meta: dict[Underlying, dict[str, Any]] = {}
        if self._hl is not None:
            hl_meta = dict(await self._hl.get_instrument_meta())
        for u in Underlying:
            m = hl_meta.get(u, {})
            self.instruments[(u, Instrument.HL_PERP)] = InstrumentInfo(
                underlying=u, instrument=Instrument.HL_PERP,
                code=str(m.get("code", "")), multiplier=1.0,
                sz_decimals=m.get("sz_decimals"), max_leverage=m.get("max_leverage"))
            for (fu, inst), code in self.futures_codes.items():  # 근·차근(§5.11)
                if fu is u:
                    self.instruments[(u, inst)] = InstrumentInfo(
                        underlying=u, instrument=inst, code=code, multiplier=10.0,
                        expiry=self.futures_expiry.get((u, inst)))
            self.instruments[(u, Instrument.KR_STOCK)] = InstrumentInfo(
                underlying=u, instrument=Instrument.KR_STOCK, multiplier=1.0)

    async def _startup_init(self) -> None:
        """시동 초기화 — 종목→종목정보→잔고→포지션→주문 **순차**. 한 작업이라도 실패하면 그 작업명을
        ``self.startup_load_error``에 남기고 즉시 중단한다(예외는 던지지 않음 — 코어는 살아
        /state로 실패를 알리고, 메인창이 "…재접속하세요" 팝업 후 종료).

        온디맨드 재조회는 ``refresh_snapshot``(재연결 경로) — 이쪽은 시동 1회 전용.
        """
        import logging

        log = logging.getLogger("kp_arb.bootstrap")
        # 1) 종목 — 근월물 누락은 bootstrap_live가 미리 판정(startup_load_error 설정).
        if self.startup_load_error is not None:
            log.error("시동 로드 실패: %s", self.startup_load_error)
            return
        log.info("시동 로드 OK: 종목 (주식선물 근월물 %d종, 차근월물 %d종)",
                 len(self.futures_symbols), len(self.next_futures_symbols))
        # 1-2) 종목정보 — HL 소수자릿수·최대레버리지(없으면 HL 주문 수량 반올림이 어긋남).
        try:
            await self.load_instruments()
            log.info("시동 로드 OK: 종목정보 (HL %s)",
                     "조회됨" if self._hl is not None else "미사용")
        except Exception:  # noqa: BLE001 - 실패는 기록만 하고 중단(팝업 유도)
            self.startup_load_error = "종목정보(HL 소수자릿수·레버리지)"
            log.error("시동 로드 실패: 종목정보", exc_info=True)
            return

        accounts = (Account.KR_STOCK, Account.KR_DERIV)
        positions: list[Position] = []
        balances: dict[Account, float] = {}
        open_orders: list[TrackedOrder] = []
        # 유령 정리 대상은 미체결 조회가 실제로 된 계좌만(refresh_snapshot과 같은 규칙, 2026-09-09)
        reconciled: set[Account | None] = set()
        # 2) 잔고
        try:
            for account in accounts:
                balances[account] = await self._gw.get_balance(account)
            log.info("시동 로드 OK: 잔고 (%d계좌)", len(balances))
        except Exception:  # noqa: BLE001 - 실패는 기록만 하고 중단(팝업 유도)
            self.startup_load_error = "잔고"
            log.error("시동 로드 실패: 잔고", exc_info=True)
            return
        # 3) 포지션
        try:
            for account in accounts:
                positions.extend(await self._gw.get_positions(account))
            if self._hl is not None:
                hl_pos, self.hl_detail = await self._hl.get_positions_and_details()
                positions.extend(hl_pos)
                reconciled.add(None)  # HL(계좌 없음) 정리 대상
                for u, lev in (await self._hl.get_leverage_settings()).items():
                    d = self.hl_detail.setdefault(u, {})
                    d.setdefault("leverage", lev["leverage"])
                    d.setdefault("leverage_cross", lev["leverage_cross"])
            log.info("시동 로드 OK: 포지션 (%d건)", len(positions))
        except Exception:  # noqa: BLE001
            self.startup_load_error = "포지션"
            log.error("시동 로드 실패: 포지션", exc_info=True)
            return
        # 4) 주문(미체결)
        try:
            for account in accounts:
                open_orders.extend(await self._gw.get_open_orders(account))
                if self._gw.open_orders_supported(account):
                    reconciled.add(account)
            if self._hl is not None:
                open_orders.extend(await self._hl.get_open_orders())
            log.info("시동 로드 OK: 주문 (미체결 %d건)", len(open_orders))
        except Exception:  # noqa: BLE001
            self.startup_load_error = "주문"
            log.error("시동 로드 실패: 주문", exc_info=True)
            return

        self.order_book.load_snapshot(
            positions=positions, balances=balances, open_orders=open_orders,
            reconcile_accounts=reconciled,
        )
        log.info("시동 로드 완료 — 종목·잔고·포지션·주문 전부 OK")

    async def start(self) -> None:
        """최초 스냅샷 → 세션 초기값(옵션) → WS 결선 → 실시간 수신 시작(재연결 포함)."""
        await self._startup_init()  # 종목→종목정보→잔고→포지션→주문 순차(실패 시 load_error)
        if self.startup_load_error is not None:
            # 로드 실패 — WS 결선·상시 조회 생략(사용자가 곧 재접속). 서버는 /state로 실패 알림.
            return
        self._seed_session_from_env()
        await self._seed_initial_prices()  # 구독 **전** 현재가 초기값 — 실시간이 안 와도 빈칸 방지
        self._wire()
        # 시동 REST 조회들은 **순차 실행** — 동시에 나가면 서버 계정당 초당 한도에
        # 걸려 일부(t1901 등)가 실패한다(운영 실측). 환율 폴링은 그 뒤에 시작.
        self._tasks = [asyncio.create_task(self._startup_queries())]
        # 무한 배경 기록/조회 — wait()(스트리밍 소진 대기) 대상이 아니라 aux로 둔다.
        self._aux_tasks = [asyncio.create_task(self._spread_csv_loop()),
                           asyncio.create_task(self._prev_funding_loop())]
        # run 자체가 아니라 **팩토리(.run)**를 넘긴다 — 재시작 때 새 코루틴을 만들어야 하므로.
        self._tasks.append(asyncio.create_task(
            self._guarded_ws("주식", self._stock_ws.run, self._mark_ws_dead)))
        if self._deriv_ws is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._guarded_ws("선물", self._deriv_ws.run, self._mark_ws_dead))
            )
        if self._hl_ws is not None:
            self._tasks.append(asyncio.create_task(
                self._guarded_ws("HL", self._hl_ws.run, self._mark_ws_dead)))

    def usdkrw_effective(self, now: datetime | None = None) -> tuple[float | None, str]:
        """HL 환산에 실제 쓰는 환율과 출처: 주간 창(fx_spot_window) 안이고 외환현물이
        있으면 ("현물"), 아니면 환율이론가("선물이론"). (값, 출처) 반환."""
        moment = now if now is not None else datetime.now()
        if self.usdkrw_spot is not None and in_time_window(
            moment.time(), *self._fx_spot_window
        ):
            return self.usdkrw_spot, "현물"
        return self.usdkrw_theory, "선물이론"

    def _apply_fx_price(self, code: str, price: float) -> None:
        """원달러선물 현재가 수신 → 월물별 저장 + **최근월물만** 환율이론가(현물환산) 갱신.
        차근월물은 저장만 한다(§9.1 — 헤지 월물 선택용). WS(FC9/DC0)·예비 조회 공용."""
        from datetime import date

        if price <= 0:
            return
        self.fx_futures_price[code] = price  # 근·차근 모두 최신가 보관
        if self._fx_futures is None or code != self._fx_futures[0]:
            return  # 차근월물 등은 환율이론가에 안 먹인다(최근월물 기준 유지)
        _, ym = self._fx_futures
        days = days_to_expiry(ym, "USD", date.today())
        self.usdkrw_futures = price
        self.usdkrw_theory = carry_theory(price, days, self._carry.fx)

    def _apply_fx_spot(self, rate: float) -> None:
        """원달러 현물환율(CUR) 실시간 수신 → HL 환산 본선 환율. Naver 백업은 이걸로 억제."""
        import time as _t

        if rate <= 0:
            return
        if self.usdkrw_spot_src != "LS":  # 출처 전환(첫 수신·백업→LS 복귀)만 기록 — 틱마다 X
            import logging

            logging.getLogger("kp_arb.bootstrap").info(
                "LS 현물환율(CUR) 수신 시작 %.2f (이전 출처: %s)", rate,
                self.usdkrw_spot_src or "없음")
        self.usdkrw_spot = rate
        self.usdkrw_spot_src = "LS"
        self._fx_spot_ts = _t.monotonic()

    async def _fx_loop(self) -> None:
        """환율 예비 갱신 — 시동 직후 초기값 + 30초 간격 확인 조회(t2111).

        본선은 WS(주간 FC9 / 야간 DC0 — FC0는 2026-06 폐지) 실시간이고, 이 루프는
        WS가 조용할 때(체결 없음·미실측 필드 불일치)의 안전망이다. 주간(08~16시)만.
        """
        import logging
        from datetime import datetime

        if self._fx_futures is None:
            return
        code, _ = self._fx_futures
        log = logging.getLogger("kp_arb.bootstrap")
        failures = 0
        # 시동 초기값 조회(_seed_initial_prices)가 방금 t2111을 월물 수만큼 써서 초당 한도(2회)가
        # 찬 상태 — 바로 조회하면 RateLimitError 경고만 남긴다(2026-09-04 실측). 한 박자 쉬고 시작.
        await asyncio.sleep(2.0)
        while True:
            now = datetime.now()
            in_spot = in_time_window(now.time(), *self._fx_spot_window)
            if not in_spot and not 8 <= now.hour < 16:  # 세션 밖 — 마지막 값 유지
                await asyncio.sleep(60.0)
                continue
            try:
                if 8 <= now.hour < 16:  # 통화선물 주간장 — 이론가 예비 갱신
                    price = await self._gw.get_fx_futures_price(code)
                    if price is not None:
                        self._apply_fx_price(code, price)  # code=최근월물
                # 외환현물 시간대 — 본선은 LS 실시간(CUR). 한 번도 못 받았거나 10분+ 조용할 때만
                # 네이버 백업. (60초 기준은 개장 전후 1~2분 간격 체결에 계속 걸려 출처가
                # 널뛰었다 — 2026-09-04 실측.)
                import time as _t
                if in_spot and fx_spot_backup_due(self._fx_spot_ts, _t.monotonic()):
                    from .gateways.fx_spot import fetch_usdkrw_spot

                    spot = await fetch_usdkrw_spot()
                    if spot is not None:
                        if self.usdkrw_spot_src != "하나고시":  # 전환 시점만 기록
                            silent = _t.monotonic() - self._fx_spot_ts
                            log.warning(
                                "LS 현물환율(CUR) %s — 하나은행 고시환율로 대체 %.2f",
                                "미수신(시동 후)" if self._fx_spot_ts == 0.0
                                else f"{silent:.0f}초 무수신", spot)
                        self.usdkrw_spot = spot
                        self.usdkrw_spot_src = "하나고시"  # 네이버 경유 하나은행 고시 매매기준율
                failures = 0
            except Exception:  # noqa: BLE001
                failures += 1
                if failures == 1:  # 반복 실패는 첫 번째만 기록
                    log.warning("원달러선물 시세(t2111) 조회 실패 — 재시도 계속",
                                exc_info=True)
            await asyncio.sleep(30.0)

    # --- 괴리 보드 (DESIGN §6.1 — 모니터·전략 공용) ---

    def stock_futures_theory(
        self, underlying: Underlying,
        instrument: Instrument = Instrument.KR_STOCK_FUTURE,
    ) -> float | None:
        """주식선물 이론가 = 기초 주식 현재가 × (1 + r × 잔존일/365), r = 공통설정 주식선물 이자율
        (기본 3.0%), 배당 무시. 월물(근|차근)별 만기(둘째 목요일).

        기초가는 **통합(uni, NXT 포함) 우선, 없으면 KRX** — 엑셀(RTD)과 동일 기준.
        (ETF 이론가의 기초는 KRX 전용 유지 — 거래소 iNAV 기준과 일치시키기 위함.)
        """
        from datetime import date

        base = self.stock_last(underlying)
        ym = self.futures_expiry.get((underlying, instrument))
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

    @staticmethod
    def _levels(
        quote: Quote | None,
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Quote에서 (매수호가창, 매도호가창) — 다단계 없으면 1호가 단일 단계로 축약."""
        if quote is None:
            return [], []
        bids = list(quote.bids) if quote.bids else [(quote.bid, quote.bid_qty or 1.0)]
        asks = list(quote.asks) if quote.asks else [(quote.ask, quote.ask_qty or 1.0)]
        return bids, asks

    def _ls_depth_quote(
        self, underlying: Underlying, instrument: Instrument
    ) -> Quote | None:
        """국내 호가창 Quote — 통합(uni, NXT 포함) 우선 → KRX → NXT."""
        for m in ("uni", "krx", "nxt"):
            q = self.quotes.get((underlying, instrument, m))
            if q is not None:
                return q
        return None

    def set_hl_aggregation(
        self, u: Underlying, n_sig_figs: int | None, mantissa: int | None = None
    ) -> None:
        """HL 호가단위 머지 변경 — WS 구독 취소 후 재구독 (시세 화면 콤보에서 호출).

        머지 중엔 est-pr·호가창이 머지 호가 기준이 된다(1호가 표시는 bbo 원시 유지).
        """
        if self._hl_ws is not None:
            self._hl_ws.set_l2_aggregation(u, n_sig_figs, mantissa)

    def hl_merge_active(self, u: Underlying) -> tuple[int | None, int | None] | None:
        """HL 현재 적용 호가단위 머지(nSigFigs, mantissa) — 스냅샷용. HL WS 없으면 None."""
        return None if self._hl_ws is None else self._hl_ws.l2_aggregation(u)

    def ws_statuses(self) -> list[WsStatus]:
        """살아있는 WS 채널들의 현황(메인창 표·주문 안전차단 Phase 8-6용).

        LS 주식·LS 선물·HL 순. 없는 채널(키 미설정 등)은 건너뛴다.
        """
        clients = [self._stock_ws, self._deriv_ws, self._hl_ws]
        return [c.status for c in clients if c is not None]

    def pair_signal(
        self, u: Underlying, instrument: Instrument,
        entry_qty: int, exit_qty: int,
    ) -> tuple[float | None, float | None]:
        """est 기반 진입/청산 스프레드(소수) — 주문 화면 표시·판정 루프 공용 (§6.2).

        진입 = HL매수d(est) − 국내매수d(1호가 maker) / 청산 = HL매도d(est) − 국내매도d.
        est는 각 블록 수량(국내 단위)을 HL 계약으로 환산(주식 1:1, 선물 1:10)해 산정 —
        진입은 entry_qty, 청산은 exit_qty(1회주문수량 진입/청산 분리, 사용자 확정).
        국내 기준가: 선물 = 선물이론가, 주식 = 자기 현재가 (§6.1).
        """
        quote = self.quotes.get((u, Instrument.HL_PERP, "hl"))
        if quote is None:
            return None, None
        ratio = 10.0 if instrument.is_stock_future else 1.0
        est_bid = (est_price(quote.bids or [], entry_qty * ratio)
                   if entry_qty > 0 else None)
        est_ask = (est_price(quote.asks or [], exit_qty * ratio)
                   if exit_qty > 0 else None)
        fx, _ = self.usdkrw_effective()
        stock = self.stock_last(u)
        base = (self.stock_futures_theory(u, instrument)
                if instrument.is_stock_future else stock)
        if fx is None or stock is None or base is None:
            return None, None
        kr_ask, kr_bid = self._best_quote(u, instrument)
        hl_bid_d = disp(est_bid * fx if est_bid is not None else None, stock)
        hl_ask_d = disp(est_ask * fx if est_ask is not None else None, stock)
        kr_bid_d = disp(kr_bid, base)
        kr_ask_d = disp(kr_ask, base)
        entry = (hl_bid_d - kr_bid_d
                 if hl_bid_d is not None and kr_bid_d is not None else None)
        exit_ = (hl_ask_d - kr_ask_d
                 if hl_ask_d is not None and kr_ask_d is not None else None)
        return entry, exit_

    def est_pair_prices(
        self,
        u: Underlying,
        instrument: Instrument,
        kr_qty: int,
        entry_threshold: float,
        exit_threshold: float,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """est-pr(HL taker가)와 역산 LS maker 주문가 — 보드 표시용 (DESIGN §6.2-4).

        kr_qty는 **국내 수량**(주식 쌍=주, 선물 쌍=계약 — 주문 화면 1회주문수량과
        같은 의미). HL 환산: 주식 1:1, 선물 1계약=10계약 (§6.2-3).
        진입은 HL 매도라 매수 호가창, 청산은 매도 호가창.
        반환: (HL est 진입, HL est 청산 [USD], LS 주문가 진입, LS 주문가 청산 [원]).
        기준값은 소수(0.0006 = 0.06%).
        """
        quote = self.quotes.get((u, Instrument.HL_PERP, "hl"))
        if quote is None or kr_qty <= 0:
            return None, None, None, None
        hl_qty = float(kr_qty) * (
            10.0 if instrument.is_stock_future else 1.0)
        est_bid = est_price(quote.bids or [], hl_qty)   # 진입: HL 매도 → 매수호가
        est_ask = est_price(quote.asks or [], hl_qty)   # 청산: HL 매수 → 매도호가
        fx, _ = self.usdkrw_effective()
        stock = self.stock_last(u)
        base = (self.stock_futures_theory(u, instrument)
                if instrument.is_stock_future else stock)
        px_entry = px_exit = None
        if fx is not None and stock is not None and base is not None:
            hl_bid_d = disp(est_bid * fx if est_bid is not None else None, stock)
            hl_ask_d = disp(est_ask * fx if est_ask is not None else None, stock)
            kr_ask, kr_bid = self._best_quote(u, instrument)  # maker 보정용 1호가
            if hl_bid_d is not None:
                raw = maker_price_for_spread(base, hl_bid_d, entry_threshold)
                tick = tick_for(instrument, raw)
                px_entry = maker_cap(Side.BUY, floor_to_tick(raw, tick),
                                     kr_ask, kr_bid, tick)
            if hl_ask_d is not None:
                raw = maker_price_for_spread(base, hl_ask_d, exit_threshold)
                tick = tick_for(instrument, raw)
                px_exit = maker_cap(Side.SELL, ceil_to_tick(raw, tick),
                                    kr_ask, kr_bid, tick)
        return est_bid, est_ask, px_entry, px_exit

    def disparity_board(
        self, kr_qty: int
    ) -> dict[tuple[Underlying, Instrument], PairBoard]:
        """HL vs 국내 상대별 괴리·진입/청산 스프레드 (DESIGN §6.1). 신호는 **est-price 기반**.

        kr_qty = 발주 국내 수량(주식=주/선물=계약). HL 수량 = kr_qty × (SF 10 / 주식 1).
        진입/청산 = 그 수량만큼 양쪽 호가창을 쓸어담은 est가의 괴리차. 수량 0이면 스프레드 None.
        화면은 입력 수량, CSV 분포 기록은 config `disparity.ref_qty`를 넣는다.
        """
        board: dict[tuple[Underlying, Instrument], PairBoard] = {}
        fx, _ = self.usdkrw_effective()
        fees = {
            Instrument.KR_STOCK: self._fees.stock,
            Instrument.KR_STOCK_FUTURE: self._fees.stock_future,
            Instrument.KR_STOCK_FUTURE_NEXT: self._fees.stock_future,  # 차근 동일 수수료
            Instrument.KR_ETF: self._fees.etf,
        }
        for u in Underlying:
            stock = self.stock_last(u)
            hl_bids, hl_asks = self._levels(self.quotes.get((u, Instrument.HL_PERP, "hl")))
            # HL 현재가(체결) 괴리 — 엑셀 시세!AD열(메인 I22)
            hl_px = self.trades.get((u, Instrument.HL_PERP, "hl"))
            hl_last = disp(
                hl_px * fx if hl_px is not None and fx is not None else None, stock)
            # 주식 쌍의 기준가 = 자기 현재가 (HL disp가 이미 주식 현재가 대비 —
            # 옛 엑셀 현대차 행 AE62/AF62 패턴). 방향은 A(주식 매수+HL 숏) 전용(공매도 금지).
            targets: list[tuple[Instrument, float | None]] = [
                (Instrument.KR_STOCK, stock),
            ]
            if u in self.futures_symbols:
                targets.append((Instrument.KR_STOCK_FUTURE, self.stock_futures_theory(u)))
            if u in self.next_futures_symbols:  # 차근 행 — 진입·청산·est 모두 차근 기준(§5.11)
                targets.append((Instrument.KR_STOCK_FUTURE_NEXT, self.stock_futures_theory(
                    u, Instrument.KR_STOCK_FUTURE_NEXT)))
            if u in self.etf_symbols:
                targets.append((Instrument.KR_ETF, self.etf_theory_price(u)))
            for instrument, base in targets:
                mult = 10.0 if instrument.is_stock_future else 1.0
                hl = est_side_disp(hl_bids, hl_asks, kr_qty * mult, stock, fx)
                kr_bids, kr_asks = self._levels(self._ls_depth_quote(u, instrument))
                kr = est_side_disp(kr_bids, kr_asks, float(kr_qty), base)
                kr_last_px = (self.trades.get((u, instrument, "uni"))
                              or self.trades.get((u, instrument, "krx")))
                spread = pair_spread(hl, kr)
                board[(u, instrument)] = PairBoard(
                    hl=hl, kr=kr, spread=spread,
                    hl_last=hl_last, kr_last=disp(kr_last_px, base),
                    net_entry=net_entry(spread, fees[instrument]),
                    net_exit=net_exit(spread),
                )
        return board

    async def _seed_initial_prices(self) -> None:
        """구독 **전** 현재가 초기값 채우기 (사용자 원칙 2026-09-03).

        실시간은 장 종료 후 재시동·구독 거부·피드 장애로 안 올 수 있다. 그렇다고 현재가를
        0/None으로 두면 괴리·est·주문가가 전부 빈칸이 된다. 그래서 WS 결선 전에 REST로 마지막
        가격을 먼저 채운다: 주식·주식선물(t1102/t8402) → 원달러선물 근·차근(t2111, 시간대 무관 —
        밤 재시동도 마지막 체결가로 시작) → 현물환율(하나은행 고시, 실시간 CUR 오기 전 임시).
        실패는 로그만 남기고 계속(실시간이 오면 채워진다). 순차 호출 — 초당 한도 보호.
        """
        import logging

        log = logging.getLogger("kp_arb.bootstrap")
        await self._seed_prices()  # 주식·주식선물·ETF 현재가 → self.trades (실패 시 자체 경고)
        fx_filled = 0
        for code, _ym in self._fx_months:
            try:
                price = await self._gw.get_fx_futures_price(code)
            except Exception:  # noqa: BLE001 - 초기값 실패는 기록만(실시간·예비 조회가 채움)
                log.warning("시동 초기값: 원달러선물 %s 현재가(t2111) 조회 실패", code,
                            exc_info=True)
                continue
            if price is not None and price > 0:
                self._apply_fx_price(code, price)
                fx_filled += 1
        if self._fx_futures is not None and self.usdkrw_spot is None:  # 라이브 구성일 때만
            try:
                from .gateways.fx_spot import fetch_usdkrw_spot

                spot = await fetch_usdkrw_spot()
            except Exception:  # noqa: BLE001
                spot = None
            if spot is not None:
                self.usdkrw_spot = spot
                self.usdkrw_spot_src = "하나고시"
        log.info("시동 초기값 채움 — 현재가 %d종, 원달러선물 %d/%d월물, 현물환율 %s",
                 len(self.trades), fx_filled, len(self._fx_months),
                 self.usdkrw_spot_src or "없음")

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

    async def _load_etf_refs(self) -> bool:
        """ETF 이론가 고정 입력 조회 1회 시도. 전부 확보되면 True."""
        import logging

        try:
            self.etf_theory.update(await self._gw.get_etf_refs())
        except Exception:  # noqa: BLE001 - 이론가 없이도 나머지는 정상
            logging.getLogger("kp_arb.bootstrap").warning(
                "ETF 이론가 입력 조회 실패", exc_info=True
            )
        return not (set(self.etf_symbols) - set(self.etf_theory))

    async def _etf_refs_retry_loop(self) -> None:
        """미확보 ETF 이론가 입력을 30초 간격으로 재시도 — t1901 간헐 500 대응(실측).

        시동 1차 시도에서 실패한 종목이 있으면 하루 종일 이론가가 비는 대신,
        확보될 때까지(최대 약 10분) 다시 조회한다.
        """
        import logging

        for attempt in range(1, 21):
            await asyncio.sleep(30.0)
            if await self._load_etf_refs():
                logging.getLogger("kp_arb.bootstrap").info(
                    "ETF 이론가 입력 확보 완료 (재시도 %d회째)", attempt
                )
                return

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

    # 괴리 분포 CSV — 임계값 결정용. 모니터 화면이 아니라 코어가 상시 기록(화면 무관).
    def _spread_csv_rows(self, hhmmss: str) -> list[tuple[str, ...]]:
        """CSV 한 줄씩(진입/청산 중 하나라도 있는 쌍만) — 파일 I/O 없는 순수 조립."""
        kind = {Instrument.KR_STOCK: "S", Instrument.KR_STOCK_FUTURE: "SF",
                Instrument.KR_STOCK_FUTURE_NEXT: "SFN", Instrument.KR_ETF: "ETF"}

        def _pct(v: float | None) -> str:
            return f"{v * 100:.3f}" if v is not None else "-"

        board = self.disparity_board(self._board_ref_qty)  # 기준 수량(config) est 스프레드
        fx, _ = self.usdkrw_effective()  # 환산에 실제 쓴 환율(주간 현물/야간 이론)
        return [
            (hhmmss, u.value, kind[inst],
             _pct(p.hl.ask), _pct(p.hl.bid), _pct(p.kr.ask), _pct(p.kr.bid),
             _pct(p.spread.entry), _pct(p.spread.exit),
             f"{fx:.4f}" if fx is not None else "-",
             (f"{s:.0f}" if (s := self.stock_last(u)) is not None else "-"),
             _pct(p.hl_last), _pct(p.kr_last))
            for (u, inst), p in board.items()
            if p.spread.entry is not None or p.spread.exit is not None
        ]

    def _write_spread_csv(self) -> None:
        """진입/청산 괴리 1줄씩을 logs/spread_YYYYMMDD.csv에 덧붙인다(있는 쌍만)."""
        import csv
        import time
        from pathlib import Path

        lines = self._spread_csv_rows(time.strftime("%H:%M:%S"))
        if not lines:
            return
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        path = log_dir / f"spread_{time.strftime('%Y%m%d')}.csv"
        new_file = not path.exists()
        try:
            with path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if new_file:
                    writer.writerow(["time", "underlying", "pair",
                                     "hl_ask_d", "hl_bid_d", "kr_ask_d", "kr_bid_d",
                                     "entry", "exit", "usdkrw_used", "base_last",
                                     "hl_last_d", "kr_last_d"])
                writer.writerows(lines)
        except OSError:
            pass  # 엑셀 등이 파일을 잠근 상태 — 이번 기록만 건너뛰고 풀리면 재개

    async def _prev_funding_loop(self) -> None:
        """직전 확정 펀딩률을 15분 간격 REST 조회 — 모니터 '펀딩전' 표시용(코어 보관)."""
        hl = self._hl
        if hl is None or not hasattr(hl, "get_prev_funding"):
            return  # 목/HL 미연결 — 펀딩전 없음
        while True:
            for u in Underlying:
                try:
                    self.hl_funding_prev[u] = await hl.get_prev_funding(u)
                except Exception:  # noqa: BLE001 - 표시용, 실패해도 계속
                    pass
            await asyncio.sleep(900.0)  # 펀딩 주기(1시간)의 1/4

    async def _spread_csv_loop(self) -> None:
        """1초 간격 괴리 분포 기록. 어떤 예외도 루프를 멈추지 않는다."""
        while True:
            await asyncio.sleep(1.0)
            try:
                self._write_spread_csv()
            except Exception:  # noqa: BLE001 - 기록 실패로 코어 루프를 죽이지 않는다
                import logging
                logging.getLogger("kp_arb.core").exception("괴리 CSV 기록 실패")

    async def _startup_queries(self) -> None:
        """시동 일괄 조회를 순서대로 — ETF 이론가 입력 → 초기 가격 → 상시 루프.

        순차 실행은 서버 계정당 초당 한도 충돌 방지(실측). 이후 환율 예비 조회와
        미확보 ETF 재시도는 병행 루프로.
        """
        complete = await self._load_etf_refs()
        # 현재가 초기값은 start()가 구독 전에 이미 채웠다(_seed_initial_prices).
        if complete:
            await self._fx_loop()
        else:
            await asyncio.gather(self._fx_loop(), self._etf_refs_retry_loop())

    @staticmethod
    async def _guarded_ws(
        name: str, make_run: Callable[[], Coroutine[Any, Any, None]],
        on_dead: Callable[[str], None] | None = None,
    ) -> None:
        """WS 하나가 죽어도 전체를 멈추지 않고, **run()이 정상 반환하면 재시작(재연결)**한다.

        run() 내부는 예외 끊김만 재연결하고, **서버 graceful close(async for 정상 종료)는
        반환**한다 — HL은 배포·유휴로 graceful close가 흔해 그대로 두면 영구 끊김. 그 경우를
        여기서 재시작으로 이어붙인다(백오프 2초). **예외로 죽으면**(설정·인증 등) 원래대로 그
        채널만 포기한다(재시작 안 함 — 무한 재시도·폭주 방지). shutdown(취소)도 재시작 안 함.
        """
        import logging

        from .gateways.ls_ws import WSClosed

        log = logging.getLogger("kp_arb.bootstrap")
        while True:
            try:
                await make_run()
            except asyncio.CancelledError:
                raise  # 종료(shutdown) — 재시작 안 함
            except WSClosed:
                return  # 커넥터 명시 종료(테스트/셧다운) — 재시작 안 함
            except Exception:  # noqa: BLE001 - 채널 단위 격리(설정·인증 오류 등)
                log.exception("%s WS 중단 — 해당 채널 없이 계속", name)
                if on_dead is not None:
                    on_dead(name)  # 시세·체결통보가 끊긴 채 돌면 안 됨 → 팝업(재접속하세요)
                return
            # run() 정상 반환 = 서버 graceful close(끊김) → 재연결 위해 재시작
            log.warning("%s WS 끊김(정상종료) — 재연결", name)
            await asyncio.sleep(2.0)

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
            hl_margin_ratio=None,  # HL 마진비율은 안 쓴다(사용자 2026-09-09) — 옛 전략 루프 관문용
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
        for task in (*self._tasks, *self._aux_tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, *self._aux_tasks, return_exceptions=True)
        self._tasks = []
        self._aux_tasks = []


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
    import logging as _logging

    _blog = _logging.getLogger("kp_arb.bootstrap")
    # 주식선물 근·차근 월물 자동 조회(만기 롤오버 대응, §5.11) 후 게이트웨이 재조립.
    # 조회 실패·월물 누락은 아래 startup_symbol_error가 "종목" 로드 실패로 판정한다.
    try:
        _fut_rows = await gateway.fetch_futures_master()
        _blog.info("t8401 주식선물 마스터 %d행 수신", len(_fut_rows))  # 조회 원본(진단)
        months = select_months(_fut_rows, count=2)
    except Exception:  # noqa: BLE001 - 조회 실패는 종목 로드 실패(팝업)로 이어진다
        _blog.warning("주식선물 월물(t8401) 조회 실패", exc_info=True)
        months = {}
    futures_symbols: dict[Underlying, str] = {}
    next_futures_symbols: dict[Underlying, str] = {}
    futures_expiry: dict[tuple[Underlying, Instrument], int] = {}
    for u, picked in months.items():
        # 시동 시 종목별 월물 목록을 반드시 남긴다 — 차근 누락 원인(부분 응답·롤 직후) 확인용
        _blog.info("주식선물 월물 %s: %s", u.value,
                   ", ".join(f"{ym}={code}" for code, ym in picked))
        if picked:
            futures_symbols[u], futures_expiry[(u, Instrument.KR_STOCK_FUTURE)] = picked[0]
        if len(picked) > 1:
            next_futures_symbols[u] = picked[1][0]
            futures_expiry[(u, Instrument.KR_STOCK_FUTURE_NEXT)] = picked[1][1]
    if not futures_symbols:
        _blog.warning("주식선물 월물 0개 — 종목 로드 실패로 처리")
    # 원달러선물 근·차근 월물 (환율이론가=최근월물 + 헤지 월물선택용 §9.1) — 실패해도 시동 계속
    fx_futures = None
    fx_months: list[tuple[str, int]] = []
    try:
        from datetime import datetime

        fx_months = select_usd_futures_months(
            await gateway.fetch_commodity_master(), datetime.now(), count=2
        )
        fx_futures = fx_months[0] if fx_months else None
    except Exception:  # noqa: BLE001 - 환율이론가 없이 계속(HL 괴리만 빈값)이되, 실패는 남긴다
        _blog.warning("원달러선물 월물(commodity) 조회 실패 — 환율이론가 빈값", exc_info=True)
    if fx_months:
        _blog.info("원달러선물 월물 로드: %s", [c for c, _ in fx_months])
    else:
        _blog.warning("원달러선물 월물 0개 — 환율이론가·FX 대응주문 대상 없음")
    gateway = LSApiGateway.from_accounts(
        accounts,
        token_transport=token_tx,
        rest_transport=AiohttpRestTransport(session),
        base_url=LIVE_BASE_URL,
        futures_symbols=futures_symbols,
        etf_symbols=etf_symbols,
        next_futures_symbols=next_futures_symbols,
    )

    url = ls_ws_url(current_mode())

    async def ws_for(account: Account) -> LSWebSocketClient:
        cred = accounts.for_account(account)
        name = "LS 주식" if account == Account.KR_STOCK else "LS 선물"

        async def fresh_token() -> str:
            # 재연결 시마다 새 토큰 — LS 토큰 만료(약 1일) 후 재접속 거부 방지
            return (await token_tx.fetch_token(cred.appkey, cred.appsecret)).access_token

        return LSWebSocketClient(
            LSWebSocketConnector(url), token_provider=fresh_token,
            etf_symbols=etf_symbols,
            # 선물 WS는 계좌 통보(O01/C01/H01)만 받아 평소 데이터가 없다 — "시세 지연(무데이터)"
            # 헛경고(하루 10회+, 2026-09-03 실측)를 막기 위해 스트림 기대를 끈다.
            status=WsStatus(venue="LS", name=name,
                            kind="시세/주문" if account == Account.KR_STOCK else "주문통보",
                            expects_stream=account == Account.KR_STOCK),
            # 장시간 운영: 사실상 무제한 재연결 + 2초 대기 (한도는 연속 실패에만)
            max_reconnects=1_000_000, reconnect_backoff_s=2.0,
        )

    # HL 슬롯 — 비밀(HL_AGENT_KEY/HL_ACCOUNT_ADDRESS) 없으면 LS-only.
    hl_gateway = None
    hl_ws = None
    try:
        hl_gateway = HLSdkGateway.from_secrets(symbols=config.hl_symbols())
        hl_ws = HLWebSocketClient(
            HLWebSocketConnector(), symbols=config.hl_symbols(),
            max_reconnects=1_000_000, reconnect_backoff_s=2.0,
        )
        _hl_addr = str(default_secrets().get("HL_ACCOUNT_ADDRESS"))
        hl_ws.subscribe_user_fills(_hl_addr)
        hl_ws.subscribe_order_updates(_hl_addr)  # 취소·자동취소 실시간 → OrderBook 정합
    except ConfigError:
        pass

    system = LiveSystem(
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
        next_futures_symbols=next_futures_symbols,
        fx_futures=fx_futures,
        fx_months=fx_months,
        carry_rates=config.carry_rates,
        fees=config.fees,
        fx_spot_window=(config.fx_spot_window.start, config.fx_spot_window.end),
        board_ref_qty=config.disparity.ref_qty,
    )
    # 종목 로드 판정 — 취급 종목(config.symbols)마다 주식선물 근·차근 월물 + 원달러선물 월물이
    # 있어야 한다. 하나라도 빠지면 "종목" 로드 실패 → 시동 초기화가 멈추고 메인창이 팝업
    # (하이닉스 근월물 누락·원달러 월물 0개가 여기 걸린다 — 2026-09-03 사용자 확정, 차근 포함).
    symbol_error = startup_symbol_error(config.symbols, futures_symbols, fx_months,
                                        next_futures_symbols)
    if symbol_error is not None:
        system.startup_load_error = symbol_error
        _blog.error("시동 로드 실패: %s", symbol_error)
    return system


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
