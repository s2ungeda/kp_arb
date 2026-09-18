"""자동M 코어 결선 — 순수 상태변화(auto_m)를 실시세·발주·체결통보에 붙인다 (②단계).

- 100ms마다 **종목별로**(삼성·하이닉스… 각각 독립, 사용자 확정 2026-09-08) 정방향 3세트의
  진입·청산을 각각 판정(``evaluate``)하고, 나온 Action을 실제 주문(LiveSystem.place/cancel)으로
  옮긴다. 선주문 = 국내 SF 지정가(maker, source "자동M"), 후주문 = HL 지정가(Gtc, 상대 1호가
  ± HP 여유, 체결 계약 × 10 — IOC·FOK 안 씀).
- 체결·취소·거부는 OrderBook 통보(on_fill_applied·on_change)로 받아 ``on_*``에 넣는다.
- 중지(체결차·후주문 거부)는 에러 알람 카운터(error_seq)를 올려 메인창이 소리를 내고,
  화면은 상태(HALTED)를 보고 세트 행을 검게 칠한다(exec §10).
- 시장 정지(exec §8)는 세션의 선물시장(5) 정지 오버레이로 본다. 풀리면 재개 딜레이.

시스템 의존은 ``_SystemLike``로 좁혀 두어 테스트는 가짜 시스템으로 돌린다(라이브 호출 없음).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .auto_m import (
    Action,
    AutoMBook,
    AutoMScreen,
    AutoMSet,
    AutoMSettings,
    Leg,
    LegStatus,
    Signals,
    evaluate,
    on_post_fill,
    on_post_partial_reject,
    on_post_recovered,
    on_post_reject,
    on_pre_ack,
    on_pre_cancel_failed,
    on_pre_cancelled,
    on_pre_fill,
    on_pre_reject,
    order_qty,
    release_halt,
    set_running,
)
from .auto_m import _halt_set as halt_set
from .disparity import disp, est_price
from .domain.enums import Block, Instrument, OrderType, Side, Underlying, Venue
from .domain.models import InstrumentInfo, OrderIntent, Quote
from .gateways.ls import OrderGoneError
from .gateways.ls_rest import RestTimeoutError
from .hl_merge import merge_tick_options
from .hl_price import hl_round_price
from .logs import attach_daily_file
from .ticks import tick_for

if TYPE_CHECKING:
    from .order_book import OrderBook, TrackedOrder
    from .strategy_core import CoreState


def reject_reason_text(exc_text: str, limit: int = 90) -> str:
    """거부 예외 문구를 상태줄용으로 — LS 거부 "CFOAT00100 rejected (02752): 증거금부족…"은
    "LS 02752 증거금부족…"으로, 그 외는 그대로. 너무 길면 자른다. (순수 함수)"""
    import re

    m = re.match(r"^\w+ rejected \((\w+)\): (.*)$", exc_text.strip())
    text = f"LS {m.group(1)} {m.group(2)}" if m else exc_text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


TICK_S = 0.1
SOURCE = "자동M"


class _SystemLike(Protocol):
    """엔진이 쓰는 LiveSystem의 일부 — 테스트용 가짜가 같은 모양을 흉내 낸다."""

    order_book: OrderBook
    quotes: dict[tuple[Underlying, Instrument, str], Quote]
    trades: dict[tuple[Underlying, Instrument, str], float]  # 현재가(장 밖엔 호가 없고 이것만)
    instruments: dict[tuple[Underlying, Instrument], InstrumentInfo]  # HL szDecimals(가격 격자)
    error_seq: int

    def pair_signal(self, u: Underlying, instrument: Instrument, entry_qty: int,
                    exit_qty: int) -> tuple[float | None, float | None]: ...
    def stock_futures_theory(self, underlying: Underlying,
                             instrument: Instrument = ...) -> float | None: ...
    def stock_last(self, underlying: Underlying) -> float | None: ...
    def usdkrw_effective(self, now: datetime | None = None) -> tuple[float | None, str]: ...
    def fx_entry_rate(self, side: Side) -> float | None: ...
    def futures_halted(self) -> bool: ...
    def stock_halted(self) -> bool: ...
    async def place(self, intent: OrderIntent, *, cloid: str | None = None) -> str: ...
    async def cancel(self, order_id: str) -> None: ...
    async def query_credit_loans(self, underlying: Underlying) -> Sequence[tuple[str, float]]: ...
    def new_hl_cloid(self) -> str | None: ...
    on_hl_identified: list[Callable[[str, str], None]]  # (cloid, oid) — 응답 전 식별 통지


@dataclass(frozen=True)
class _OrderRef:
    underlying: Underlying
    index: int
    block: Block
    leg: str  # "pre" | "post"
    reverse: bool = False  # 역방향 세트의 주문(§7A·§7B)


class AutoMEngine:
    """자동M 실행 엔진 — CoreState.autom(종목별 세트·공통설정)과 LiveSystem을 묶는다."""

    def __init__(self, state: CoreState, system: _SystemLike,
                 log_dir: Path | None = None,
                 save: Callable[[], None] | None = None,
                 product: str = "sf") -> None:
        self._state = state
        self._system = system
        # 상품(exec §7C, 2026-09-17): "sf" 주식선물 / "stock" 주식 — 엔진 인스턴스를 상품마다 따로
        # 두고 자기 상품의 종목 상태만 돈다(설정·시장 정지·로그 파일·꼬리표도 상품별)
        self.product = product
        # 상태 저장 훅(core_state.json) — RT·체결차·순잔고는 체결로 바뀌므로 명령 때만 저장하면
        # 재시동 때 잃는다. 체결·취소·중지 뒤마다 저장(사용자 2026-09-07: 재접속 때 물고 옴).
        self._save = save
        self._log = logging.getLogger("kp_arb.autom")  # 굵직한 줄 → 코어 로그
        self._log_dir = log_dir
        self._orders: dict[str, _OrderRef] = {}
        # 등록 전에 온 우리 주문 체결(발주 응답 즉시체결) — oid → [(주문, 수량, 가격)], 등록 때 반영
        self._orphan_fills: dict[str, list[tuple[TrackedOrder, float, float]]] = {}
        # 후주문 cloid → 세트(발주 응답 대기 중). 코어가 통보로 oid를 식별하면 그 oid로 등록
        # (결정 27)
        self._pending_refs: dict[str, _OrderRef] = {}
        # 발주 실패로 처리한 후주문 cloid → (세트, 수량, 시각). 코어가 유예 안에 살아 있는 주문으로
        # 식별하면 재연결(결정 30). 코어 유예(30초)보다 길게 두고 지나면 버린다.
        self._failed_refs: dict[str, tuple[_OrderRef, float, float]] = {}
        self._seen_status: dict[str, str] = {}
        self._mono = 0.0  # 마지막 tick의 단조 시계(테스트 주입 가능)
        # 종목별 상세 로그(logs/autom_<종목>_날짜.log) — 판정 근거·상태 전이·체결 반영
        # (사용자 2026-09-04). 판정·상태는 바뀔 때만 한 줄.
        self._logged_reason: dict[tuple[Underlying, int, Block, bool], str] = {}
        self._logged_status: dict[tuple[Underlying, int, Block, bool], str] = {}
        # 지연 계측(2026-09-14): 선주문 체결 반영 시각(perf_counter) → 후주문 준비 줄에 경과 ms
        self._fill_perf: dict[tuple[Underlying, int, Block, bool], float] = {}
        self._persist_ms = 0.0  # 마지막 상태 저장에 걸린 ms(후주문 준비 줄에 함께)
        self._persist_pending = False  # 저장 예약됨(루프 다음 차례에 한 번)
        self._halt_since: float | None = None
        self._resumed_mono: float | None = None
        # 종목 VI(변동성완화장치, exec §8) — 주식 종목 상태만. 발동 시각·해제 시각(재개 딜레이)
        self._vi_since: dict[Underlying, float] = {}
        self._vi_resumed: dict[Underlying, float] = {}
        # 신용융자 (대출일, 수량) 캐시(종목별, 2026-09-18) — 상환 선주문마다 조회하면 CSPAQ12300
        # 초당 한도에 걸린다(재발주 왕복). 20초 재사용, 이 종목의 주식 선주문 체결 뒤엔 다시 조회
        self._loan_cache: dict[Underlying, tuple[float, list[tuple[str, float]]]] = {}
        self._bg: set[asyncio.Task[None]] = set()
        system.order_book.on_fill_applied.append(self._on_fill_applied)
        system.order_book.on_change.append(self._on_book_change)
        system.on_hl_identified.append(self._on_hl_identified)
        self._log_restored_halts()

    def _log_restored_halts(self) -> None:
        """재시동 복원 뒤 체결차가 남은 세트를 로그에 남긴다 — 중지는 대기로 복원되므로(사용자
        2026-09-17) 정리할 차이가 있는 세트는 장부로만 알 수 있다."""
        for u, book in self.screen.books_of(self.product):
            for reverse, index, s in book.all_sets():
                if abs(s.fill_diff) > 1e-9:
                    self.ulog(u).warning("복원 %s: 체결차 남음 | %s — 정리 뒤 '체결차 Clear'",
                                         self._tag(u, index, Block.ENTRY, reverse),
                                         self._ledger(s))

    # ----------------------------------------------------------------- 편의 ---
    @property
    def screen(self) -> AutoMScreen:
        return self._state.autom

    def _book(self, u: Underlying) -> AutoMBook:
        return self.screen.book(u, self.product)

    @property
    def _settings(self) -> AutoMSettings:
        """이 엔진 상품의 체결쏴 공통설정(주식선물/주식 따로)."""
        return self.screen.settings_for(self.product)

    def _set(self, u: Underlying, index: int, reverse: bool = False) -> AutoMSet:
        """종목 상태의 세트 — 정방향(sets) 또는 역방향(rev_sets, §7A·§7B)."""
        return self._book(u).sets_of(reverse)[index]

    @staticmethod
    def _counterpart(book: AutoMBook) -> Instrument:
        return book.counterpart

    @staticmethod
    def _markets(book: AutoMBook) -> tuple[str, ...]:
        """국내 시세를 고를 시장 순서 — 주식은 종목 상태의 거래소(KRX/NXT) 하나만(사용자 2026-09-17:
        주식 API는 통합 미지원), 주식선물은 통합·KRX·NXT 순."""
        return (book.market,) if book.product == "stock" else ("uni", "krx", "nxt")

    def _stock_last_for(self, u: Underlying, book: AutoMBook) -> float | None:
        """주식 현재가 — 주식 종목 상태는 그 거래소 체결가, 주식선물은 통합(uni) 우선 현재가."""
        if book.product == "stock":
            trades = getattr(self._system, "trades", {})
            last = trades.get((u, Instrument.KR_STOCK, book.market))
            return float(last) if last else None
        return self._system.stock_last(u)

    def ulog(self, u: Underlying) -> logging.Logger:
        """종목의 상세 로거 — logs/autom_<종목>_날짜.log (자정 롤오버, 코어 로그와 분리)."""
        suffix = "" if self.product == "sf" else f"_{self.product}"  # 주식: autom_<종목>_stock_
        return attach_daily_file(f"kp_arb.autom.{u.value}{suffix}", f"autom_{u.value}{suffix}",
                                 self._log_dir)

    @property
    def _tag_letter(self) -> str:
        return "주" if self.product == "stock" else "선"

    @staticmethod
    def _set_tag(index: int, block: Block, reverse: bool = False,
                 product: str = "선") -> str:
        """주문 꼬리표(짧은 세트 표기) — 주문 리스트 '세트' 칸·필터용(사용자 2026-09-17):
        주식선물 "선정3진"·"선역4청", 주식 "주정3진"·"주역4청"(상품·방향·세트·진입/청산)."""
        return (f"{product}{'역' if reverse else '정'}{index + 1}"
                f"{'진' if block is Block.ENTRY else '청'}")

    def _tag(self, u: Underlying, index: int, block: Block, reverse: bool = False) -> str:
        # 방향 표기(사용자 2026-09-08: 진입/청산만으론 정/역 구분이 안 됨) — 세트의 방향 값으로.
        # 주식 엔진은 앞에 "주식 "(로그 파일이 따로라 짧게)
        direction = "역방향" if reverse else "정방향"
        head = "주식 " if self.product == "stock" else ""
        return f"{head}{direction} {index + 1}세트 {'진입' if block is Block.ENTRY else '청산'}"

    def _trace(self, u: Underlying, index: int, block: Block, leg: Leg,
               reverse: bool = False) -> None:
        """판정 결과·상태가 바뀐 때만 한 줄 — 100ms마다 다 남기면 하루 수십만 줄."""
        key = (u, index, block, reverse)
        tag = self._tag(u, index, block, reverse)
        if leg.block_reason and leg.block_reason != self._logged_reason.get(key):
            self._logged_reason[key] = leg.block_reason
            # G5 미달(조건 안 맞아 기다리는 평상시)은 파일에 안 남긴다 — 하루 종일 쌓여 로그가
            # 넘침(사용자 2026-09-10). '유지 역산가'(걸어 둔 선주문을 그대로 두는 판정)도 안 남긴다
            # (사용자 2026-09-18). 통과·역산가 변경·G6·취소 등 나머지 근거는 그대로.
            if not leg.block_reason.startswith(("G5 미달", "유지 역산가")):
                self.ulog(u).info("판정 %s: %s", tag, leg.block_reason)
        status = leg.status.value
        if status != self._logged_status.get(key):
            prev = self._logged_status.get(key, "-")
            self._logged_status[key] = status
            self.ulog(u).info("상태 %s: %s → %s", tag, prev, status)

    # ------------------------------------------------------------ 판정 루프 ---
    async def run(self) -> None:
        while True:
            try:
                self.tick(datetime.now(), time.monotonic())
            except Exception:  # noqa: BLE001 - 한 틱의 오류로 죽지 않는다
                self._log.exception("자동M 판정 틱 오류 — 계속")
            await asyncio.sleep(TICK_S)

    def tick(self, now: datetime, mono: float) -> None:
        """전 종목·세트·진입/청산 1회 판정(now/mono 주입 — 테스트 가능)."""
        self._mono = mono
        # 시장 정지 — 주식선물은 선물시장, 주식은 주식시장 오버레이(사용자 확정 2026-09-17)
        halted = (self._system.stock_halted() if self.product == "stock"
                  else self._system.futures_halted())
        if halted:
            self._halt_since = mono
            self._resumed_mono = None
        elif self._halt_since is not None:
            self._halt_since = None
            self._resumed_mono = mono
        for u, book in self.screen.books_of(self.product):
            # 종목 VI(exec §8, 사용자 2026-09-17): 주식은 그 종목 VI 발동 중에도 정지로 본다 —
            # 단일가 전환이라 호가에 걸어 두는 선주문이 무의미. 해제 뒤 재개 딜레이는 시장 정지와
            # 같다.
            vi_on = self.product == "stock" and not halted and self._vi_halted(u)
            self._track_vi(u, vi_on, mono)
            for reverse, index, s in book.all_sets():
                for block in (Block.ENTRY, Block.EXIT):
                    leg = s.leg(block)
                    if not leg.running and leg.status is LegStatus.IDLE:
                        continue
                    sig = self.build_signals(u, book, s, block, now, mono, halted or vi_on)
                    self._apply(u, index, block, evaluate(s, block, sig, self._settings, u),
                                reverse)
                    self._trace(u, index, block, leg, reverse)

    def _vi_halted(self, u: Underlying) -> bool:
        """코어의 종목 VI 상태(LS 실시간 VI_) — 없는 시스템(테스트·옛 코어)은 False."""
        fn = getattr(self._system, "stock_vi", None)
        return bool(fn(u)) if callable(fn) else False

    def _track_vi(self, u: Underlying, vi_on: bool, mono: float) -> None:
        """종목 VI 발동·해제 시각 추적 + 종목 로그(상태가 바뀔 때 1회)."""
        if vi_on:
            if u not in self._vi_since:
                self._vi_since[u] = mono
                self.ulog(u).warning("VI 발동 — 주식 선주문 중단(해제 뒤 재개 딜레이)")
            self._vi_resumed.pop(u, None)
        elif u in self._vi_since:
            del self._vi_since[u]
            self._vi_resumed[u] = mono
            self.ulog(u).info("VI 해제 — 재개 딜레이 후 복귀")

    def _resumed_for(self, u: Underlying) -> float | None:
        """재개 딜레이 기준 시각 — 시장 정지 해제와 종목 VI 해제 중 나중 것."""
        r, v = self._resumed_mono, self._vi_resumed.get(u)
        return v if r is None or (v is not None and v > r) else r

    def build_signals(self, u: Underlying, book: AutoMBook, s: AutoMSet, block: Block,
                      now: datetime, mono: float, halted: bool) -> Signals:
        """세트의 진입(또는 청산) 하나의 판정 입력 — 수량은 이번에 낼 계약수(없으면 걸어둔 수량)."""
        inst = self._counterpart(book)
        qty = (order_qty(block, s.per_qty, s.target_qty, s.rt, s.reverse)
               or s.leg(block).pre_qty or 1)
        sf_entry, sf_exit = self._system.pair_signal(u, inst, qty, qty)
        # S괴리는 매수·매도 쪽 둘 다 — 정방향 진입은 매수 쪽(entry), 역방향 진입은 매도 쪽(exit)
        ratio = book.hl_ratio  # 주식선물 10 / 주식 1
        s_entry, s_exit = self._system.pair_signal(u, Instrument.KR_STOCK, qty * ratio,
                                                   qty * ratio)
        hl = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        fx, _src = self._system.usdkrw_effective(now)
        stock = self._stock_last_for(u, book)
        hl_bid_d = hl_ask_d = None
        est_bid = est_ask = None
        if hl is not None and fx is not None and stock:
            est_bid = est_price(hl.bids or [(hl.bid, hl.bid_qty or 1.0)], qty * ratio)
            est_ask = est_price(hl.asks or [(hl.ask, hl.ask_qty or 1.0)], qty * ratio)
            hl_bid_d = disp(est_bid * fx if est_bid else None, stock)
            hl_ask_d = disp(est_ask * fx if est_ask else None, stock)
        sf_quote = next((self._system.quotes.get((u, inst, m)) for m in self._markets(book)
                         if self._system.quotes.get((u, inst, m)) is not None), None)
        asks: tuple[tuple[float, float], ...] = ()
        bids: tuple[tuple[float, float], ...] = ()
        if sf_quote is not None:
            asks = tuple(sf_quote.asks or [(sf_quote.ask, sf_quote.ask_qty or 1.0)])
            bids = tuple(sf_quote.bids or [(sf_quote.bid, sf_quote.bid_qty or 1.0)])
        return Signals(
            now=now, mono=mono,
            sf_spread_entry=sf_entry, s_spread_entry=s_entry, sf_spread_exit=sf_exit,
            hl_disp_bid=hl_bid_d, hl_disp_ask=hl_ask_d,
            sf_theory=self._system.stock_futures_theory(u, inst), stock_last=stock,
            sf_asks=asks, sf_bids=bids, s_spread_exit=s_exit, market_halted=halted,
            resumed_mono=self._resumed_for(u), fx=fx, product=book.product,
            hl_bid1=hl.bid if hl is not None else None,
            hl_ask1=hl.ask if hl is not None else None,
            hl_est_bid=est_bid, hl_est_ask=est_ask)

    # ------------------------------------------------------------ 행동 실행 ---
    def _apply(self, u: Underlying, index: int, block: Block, actions: list[Action],
               reverse: bool = False) -> None:
        for act in actions:
            if act.kind != "notify":  # 행동 전부 종목 로그에(발주·취소·후주문·중지)
                self.ulog(u).info("행동 %s: %s %s %s %s %s",
                                  self._tag(u, index, block, reverse),
                                  act.kind, act.side.value if act.side else "",
                                  act.qty or "", act.price or "", act.reason or act.order_id or "")
            if act.kind == "place_pre":
                self._spawn(self._place_pre(u, index, block, act, reverse))
            elif act.kind == "cancel_pre" and act.order_id:
                self._spawn(self._cancel_pre(u, index, block, act.order_id, act.reason, reverse))
            elif act.kind == "place_post":
                self._spawn(self._place_post(u, index, block, act, reverse))
            elif act.kind == "halt":
                s = self._set(u, index, reverse)
                self._log.error("[자동M] %s %s 중지 — %s | %s",
                                u.value, self._tag(u, index, block, reverse), act.reason,
                                self._ledger(s))
                self._system.error_seq += 1  # 메인창 에러 알람 소리(공통설정)
            elif act.kind == "notify":
                self._log.warning("[자동M] %s %s — %s",
                                  u.value, self._tag(u, index, block, reverse), act.reason)
            elif act.kind == "alarm":  # 중지는 아니지만 사람이 봐야 함(취소실패, exec ㅂ3)
                self._log.error("[자동M] %s %s — %s",
                                u.value, self._tag(u, index, block, reverse), act.reason)
                self._system.error_seq += 1

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _place_pre(self, u: Underlying, index: int, block: Block, act: Action,
                         reverse: bool = False) -> None:
        book = self._book(u)
        s, inst = book.sets_of(reverse)[index], self._counterpart(book)
        assert act.side is not None and act.price is not None
        from .auto_m import credit_code_for

        stock = book.product == "stock"
        loan_date = ""
        if stock and s.credit and block is Block.EXIT:
            # 신용 상환은 대출일이 필수(실측 2026-09-18 LS 01486) — 잔고 조회의 대출일 중 고름.
            # 수량은 1회주문수량 그대로(사용자 2026-09-18: 신용 세트는 1주씩 낸다 — 한 대출일 잔량을
            # 넘는 수량은 LS가 거부해 거부내역에 남는다)
            loan_date = await self._pick_loan_date(u, act.qty)
        intent = OrderIntent(venue=Venue.LS, underlying=u, instrument=inst, side=act.side,
                             qty=act.qty, order_type=OrderType.LIMIT, price=act.price,
                             source=SOURCE,
                             tag=self._set_tag(index, block, reverse, self._tag_letter),
                             # 주식(exec §7C): 신용 세트 코드(진입 003·청산 101), 고른 거래소
                             credit_code=credit_code_for(block, s.credit) if stock else "000",
                             market=book.market if stock else "", loan_date=loan_date)
        try:
            oid = await self._system.place(intent)
        except RestTimeoutError as exc:
            # 응답 없음은 거부가 아니다 — LS가 접수했을 수 있어 재발주하면 두 장(실증 09-16 #3326).
            # 쌍이 맞는 게 최우선이라 **바로 세트 중지**(사용자 확정 2026-09-16). 사람이 LS
            # 미체결·잔고를 확인해 정리한 뒤 해제.
            reason = (f"선주문 응답 없음(타임아웃) — LS에 접수됐을 수 있음, 미체결·잔고 확인 뒤 "
                      f"해제: {reject_reason_text(str(exc))}")
            self._log.error("[자동M] %s 선주문 응답 없음 %s → 세트 중지 — %s",
                            u.value, self._tag(u, index, block, reverse), exc)
            self._apply(u, index, block, halt_set(s, block, reason), reverse)
            s.leg(block).last_reject_at = time.strftime("%H:%M:%S")
            return
        except Exception as exc:  # noqa: BLE001 - 거부/오류 → 딜레이 뒤 재시도(exec ㄴ5)
            self._log.warning("[자동M] %s 선주문 실패 %s — %s",
                              u.value, self._tag(u, index, block, reverse), exc)
            self._apply(u, index, block, on_pre_reject(
                s, block, time.monotonic(), self._settings,
                reason=reject_reason_text(str(exc))), reverse)
            s.leg(block).last_reject_at = time.strftime("%H:%M:%S")  # 상태줄 표시용 시각
            # 연속 거부로 실행이 꺼지면 판정 루프가 이 줄을 건너뛰어 "→ idle" 상태 줄이 안 남았다
            # (실측 2026-09-17 14:40: 다음 켬 때 "pre_resting → armed"로 보여 안 꺼진 줄 알기 쉬움)
            self._trace(u, index, block, s.leg(block), reverse)
            return
        self._register(oid, _OrderRef(u, index, block, "pre", reverse))  # 상품은 엔진 단위
        late = on_pre_ack(s, block, oid, mono=self._mono)  # 발주 중 꺼졌/중지됐으면 취소 행동
        if late:
            self._log.warning("[자동M] %s 선주문 %s #%s — 발주 응답 전 실행 꺼짐/중지 → 즉시 취소",
                              u.value, self._tag(u, index, block, reverse), oid)
            self._apply(u, index, block, late, reverse)
        self._log.info("[자동M] %s 선주문 %s %s %d @ %g → #%s",
                       u.value, self._tag(u, index, block, reverse), act.side.value, act.qty,
                       act.price, oid)

    async def _pick_loan_date(self, u: Underlying, qty: int) -> str:
        """상환 선주문에 실을 대출일 — 신용융자 잔고(대출일별 수량) 중 **오래된 것부터** 수량이 되는
        첫 대출일, 없으면 수량이 가장 많은 대출일. 잔고가 없으면 빈칸(LS가 거부 → 거부내역에 남음).
        조회가 실패하면 마지막 조회값을 그대로 쓴다."""
        now = time.monotonic()
        cached = self._loan_cache.get(u)
        loans = cached[1] if cached is not None else []
        if cached is None or now - cached[0] > 20.0:
            try:
                loans = sorted(await self._system.query_credit_loans(u))
                self._loan_cache[u] = (now, loans)
            except Exception as exc:  # noqa: BLE001 - 조회 실패면 옛 값으로(없으면 빈칸)
                self._log.warning("[자동M] %s 신용융자 잔고 조회 실패 — 대출일 %s: %s", u.value,
                                  "마지막 조회값 사용" if loans else "없음", exc)
        if not loans:
            self._log.warning("[자동M] %s 신용융자 잔고 없음 — 상환 선주문 대출일 빈칸", u.value)
            return ""
        for loan, avail in loans:  # 오래된 대출일부터 수량이 되는 것
            if avail + 1e-9 >= qty:
                return loan
        return max(loans, key=lambda x: x[1])[0]

    async def _cancel_pre(self, u: Underlying, index: int, block: Block,
                          order_id: str, reason: str, reverse: bool = False) -> None:
        self._log.info("[자동M] %s 선주문 취소 %s #%s %s",
                       u.value, self._tag(u, index, block, reverse), order_id, reason)
        # LS 초당 한도(CFOAT00300 2회)에 걸리면 잠깐 뒤 다시 — 실측 2026-09-08: 한 번 실패한 채
        # 두면 "취소 대기" 표시만 남아 재시도도 종료 취소도 안 됐다. 끝내 실패하면 표시를 되돌려
        # 다음 판정이 다시 보낸다(이미 체결/취소된 주문의 거부도 통보로 정리된다).
        for attempt in range(3):
            try:
                await self._system.cancel(order_id)
                return
            except OrderGoneError as exc:
                # 이미 체결/취소된 주문(LS 01433/03416, 장부 잔량 0) — 실패가 아니라 경합.
                # 재시도하면 같은 거부만 반복(운영 실측 2026-09-14: 3회 재시도 → 03416·02897 20줄).
                # 상태는 곧 오는 체결/취소 통보가 정리하니 표시도 되돌리지 않는다.
                self._log.info("[자동M] 취소 불필요 #%s — 이미 체결/취소됨(통보로 정리): %s",
                               order_id, exc)
                return
            except Exception as exc:  # noqa: BLE001 - 한도·통신 오류 등
                self._log.warning("[자동M] 취소 실패 #%s (%d/3) — %s", order_id, attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(0.6)
        s = self._set(u, index, reverse)
        if s.leg(block).pre_order_id == order_id:
            on_pre_cancel_failed(s, block)

    async def _place_post(self, u: Underlying, index: int, block: Block, act: Action,
                          reverse: bool = False) -> None:
        s = self._set(u, index, reverse)
        assert act.side is not None
        t_task = time.perf_counter()
        t_fill = self._fill_perf.pop((u, index, block, reverse), None)
        # 후주문도 지정가(Gtc)만(사용자 확정 2026-09-04) — 상대 1호가 ± HP 여유로 taker처럼 잡는다.
        hl = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        if hl is None or not hl.bid or not hl.ask:
            self._log.error("[자동M] %s 후주문 불가 %s — HL 호가 없음",
                            u.value, self._tag(u, index, block, reverse))
            self._apply(u, index, block, on_post_reject(
                s, block, "HL 호가 없음", qty=act.qty, mono=time.monotonic(),
                settings=self._settings), reverse)
            return
        raw_price = self._settings.post_price(act.side, hl.bid, hl.ask)
        # HL 가격 격자(유효숫자 5·소수 6−szDecimals)에 맞춘다 — 안 맞으면 통째로 거부(실측 09-07)
        info = self._system.instruments.get((u, Instrument.HL_PERP))
        price = hl_round_price(raw_price, act.side, info.sz_decimals if info else None)
        intent = OrderIntent(venue=Venue.HYPERLIQUID, underlying=u, instrument=Instrument.HL_PERP,
                             side=act.side, qty=act.qty, order_type=OrderType.LIMIT,
                             price=price, source=SOURCE,
                             tag=self._set_tag(index, block, reverse, self._tag_letter))
        # cloid를 먼저 세트에 묶어 둔다 — 응답보다 먼저 온 통보로 코어가 oid를 식별하면
        # _on_hl_identified가 그 oid로 등록한다(결정 27). 응답이 먼저면 아래 _register.
        ref = _OrderRef(u, index, block, "post", reverse)
        cloid = self._system.new_hl_cloid()
        if cloid:
            self._pending_refs[cloid] = ref
        if t_fill is not None:
            # 지연 계측(2026-09-14, 운영 ①→② 5~42ms의 내역): 체결 반영 → 이 태스크 시작(루프 대기)
            # → 전송 직전(호가·가격·의도·cloid 준비). 직전 상태 저장 시간도 참고로.
            now = time.perf_counter()
            self.ulog(u).info(
                "후주문 준비 %s: 선체결 후 %.1fms 태스크 시작 → %.1fms 전송 직전 "
                "(직전 저장 %.1fms)",
                self._tag(u, index, block, reverse), (t_task - t_fill) * 1000,
                (now - t_fill) * 1000, self._persist_ms)
        try:
            oid = await self._system.place(intent, cloid=cloid)
        except Exception as exc:  # noqa: BLE001 - 후주문 거부 → 체결차 누적, 한도 넘으면 중지(ㄹ2)
            if cloid:
                self._pending_refs.pop(cloid, None)
                # 응답 유실이면 주문이 살아 있을 수 있다 — 코어 유예 안에 식별되면 재연결(결정 30)
                self._prune_failed_refs()
                self._failed_refs[cloid] = (ref, float(act.qty), time.monotonic())
            self._log.error("[자동M] %s 후주문 실패 %s — %s",
                            u.value, self._tag(u, index, block, reverse), exc)
            self._apply(u, index, block, on_post_reject(
                s, block, str(exc)[:80], qty=act.qty, mono=time.monotonic(),
                settings=self._settings), reverse)
            return
        if cloid:
            self._pending_refs.pop(cloid, None)
        self._register(oid, ref)
        # 후주문은 취소하지 않는다(사용자 확정 2026-09-07) — 잔량이 걸려 있어도 후주문대기로 둔다.
        self._log.info("[자동M] %s 후주문 %s HL %s %d @ %g → #%s",
                       u.value, self._tag(u, index, block, reverse), act.side.value, act.qty,
                       price, oid)

    # ------------------------------------------------------------ 주문 통보 ---
    def _register(self, oid: str, ref: _OrderRef) -> None:
        """주문번호 ↔ 세트 연결. 발주 응답 안에 이미 실린 체결(HL 즉시체결)은 place()가 끝나기
        전에 훅으로 먼저 오므로 그때는 주인을 모른다 — 모아 뒀다가 여기서 되돌려 반영한다
        (실측 2026-09-07: 후주문 0.588 즉시체결이 세트에 안 잡혀 hl_net 0·HL 대기 1 남음)."""
        self._orders[oid] = ref
        for order, qty, price in self._orphan_fills.pop(oid, []):
            self._apply_fill(ref, order, qty, price)
        # 상태 통보(취소·잔량 버림 종료)도 발주 응답보다 먼저 올 수 있다 — 장부는 replay로 이미
        # 상태를 바꿨는데 그때는 주인을 몰라 _on_book_change가 지나쳤다(실측 2026-09-11 10:18:35:
        # HL이 잔량 4.037을 버리고 끝낸 통보가 응답보다 49ms 먼저 → 판이 'HL 4.037 대기'에서 안
        # 끝남). 등록 직후 한 번 더 훑어 이미 끝난 상태를 반영한다.
        self._on_book_change()

    FAILED_REF_TTL_S = 90.0  # 코어 유예(30초)+여유 — 이 뒤엔 코어도 식별하지 않으므로 버림

    def _prune_failed_refs(self) -> None:
        cutoff = time.monotonic() - self.FAILED_REF_TTL_S
        for key in [k for k, (_r, _q, at) in self._failed_refs.items() if at < cutoff]:
            del self._failed_refs[key]

    def _on_hl_identified(self, cloid: str, oid: str) -> None:
        """코어가 응답 전 통보(cloid)로 후주문 oid를 식별 — 응답을 기다리지 않고 세트에 연결.
        발주 실패로 처리했던 후주문이 살아 있는 것으로 밝혀지면(결정 30) 세트에 재연결하고 그 수량을
        후주문 대기에 도로 넣어, 뒤따르는 체결이 장부(HL 잔고·체결차)를 바로잡게 한다."""
        ref = self._pending_refs.pop(cloid, None)
        if ref is None:
            failed = self._failed_refs.pop(cloid, None)
            if failed is None:
                return  # 우리 후주문이 아니거나 이미 응답으로 등록됨
            ref, qty, _at = failed
            u = ref.underlying
            s = self._set(u, ref.index, ref.reverse)
            self._log.warning("[자동M] %s %s 실패 처리했던 후주문 #%s(cloid %s) 살아 있음 → 세트 "
                              "재연결, HL 대기 +%g (결정 30)", u.value,
                              self._tag(u, ref.index, ref.block, ref.reverse), oid, cloid, qty)
            self.ulog(u).warning(
                "통보 %s: 후주문 #%s 실패 처리 뒤 살아 있음 → 재연결, HL 대기 +%g | %s",
                self._tag(u, ref.index, ref.block, ref.reverse), oid, qty, self._ledger(s))
            self._apply(u, ref.index, ref.block, on_post_recovered(s, ref.block, qty),
                        ref.reverse)
            self._register(oid, ref)
            self._persist()
            return
        self._log.info("[자동M] %s 후주문 #%s 응답 전 식별(cloid %s) → 세트 연결",
                       ref.underlying.value, oid, cloid)
        self._register(oid, ref)

    def _on_fill_applied(self, order: TrackedOrder, qty: float, price: float,
                         _fill_id: str) -> None:
        ref = self._orders.get(order.order_id)
        if ref is None:
            if order.intent.source == SOURCE:  # 우리 주문인데 아직 등록 전 — 등록 때 되돌려 반영
                self._orphan_fills.setdefault(order.order_id, []).append((order, qty, price))
            return
        self._apply_fill(ref, order, qty, price)

    def _apply_fill(self, ref: _OrderRef, order: TrackedOrder, qty: float,
                    price: float) -> None:
        u = ref.underlying
        s = self._set(u, ref.index, ref.reverse)
        mono = time.monotonic()
        leg = s.leg(ref.block)
        if ref.leg == "pre":
            self._fill_perf[(u, ref.index, ref.block, ref.reverse)] = time.perf_counter()
            self._loan_cache.pop(u, None)  # 주식 체결로 신용융자 잔고가 바뀜 → 다음 상환 때 재조회
        # 체결 줄을 먼저 찍고 행동(후주문 발주·중지)을 적용한다 — 행동 줄이 체결 줄보다 앞에 찍혀
        # "체결 전에 판단했다"로 읽힌 실측(2026-09-10 10:45:47.536/537)을 막는다.
        tag = self._tag(u, ref.index, ref.block, ref.reverse)
        # 발주 때 보관한 est와 **지금 호가창으로 다시 계산한 est**를 나란히(사용자 2026-09-15) —
        # 선체결 순간·후체결 순간에 호가창이 발주 때와 얼마나 달라졌는지 바로 보이게.
        est_now = self._current_est(u, leg)
        est_pair = f"기준est {leg.pre_est:g} 현est {self._fmt_est(est_now)}" if leg.pre_est \
            else f"기준est - 현est {self._fmt_est(est_now)}"
        if ref.leg == "pre":
            acts = on_pre_fill(s, ref.block, int(round(qty)), price, mono)
            self.ulog(u).info("체결 %s: 선주문 #%s %g @ %g %s → 누적 %d/%d, HL 대기 %g | %s",
                              tag, order.order_id, qty, price, est_pair, leg.pre_filled,
                              leg.pre_qty, leg.post_pending, self._ledger(s))
            # 코어 로그에도 체결가·체결수량(사용자 2026-09-14: 코어 로그엔 발주가만 있고 체결 없음)
            self._log.info("[자동M] %s 선주문 체결 %s #%s %s %g @ %s %s (누적 %d/%d)",
                           u.value, tag, order.order_id, order.intent.side.value, qty,
                           f"{price:,.0f}", est_pair, leg.pre_filled, leg.pre_qty)
        else:
            # 환진입가: 원달러선물 1호가 → LS 현물환 → 없음(사용자 확정 2026-09-15). 없으면 그
            # 체결은 환 없이 쌓이고 경고 한 줄.
            fx = self._system.fx_entry_rate(order.intent.side)
            if fx is None:
                self._log.warning("[자동M] %s 후주문 체결 %s #%s — 환진입가 없음(원달러선물 호가·"
                                  "LS 현물환 모두 없음) → 이 체결은 환평균·Sprd에서 제외",
                                  u.value, tag, order.order_id)
            # Sprd 기준값(S현재가·SF이론가)은 **이 체결 시점** 값을 판 버퍼에 넣는다(사용자 확정
            # 2026-09-10 — 실시간을 쓰면 매매결과가 시세 따라 계속 바뀜). 로그에도 같은 값.
            stock = self._system.stock_last(u)
            theory = self._system.stock_futures_theory(u, self._counterpart(self._book(u)))
            acts = on_post_fill(s, ref.block, qty, price, fx, mono, self._settings,
                                stock_last=stock, sf_theory=theory)
            acc = leg.acc
            sprd = acc.sprd()
            # 선주문 발주 시점 est(후주문 방향) 대비 체결가 — 판정 때 본 값대로 잡혔나
            # (사용자 2026-09-14). 차이는 우리에게 유리하면 +: 매도는 체결가−est, 매수는 est−체결가.
            est_txt = (self._est_vs_fill(leg.pre_est, order.intent.side, price)
                       + f" 현est {self._fmt_est(est_now)}")
            self.ulog(u).info(
                "체결 %s: 후주문 #%s HL %g @ %g %s 환진입가 %s S현재가 %s SF이론가 %s → RT %d "
                "HL대기 %g | %s | 누적 HL %g SF %g 환평균 %s HL평균 %s SF평균 %s Sprd %s",
                tag, order.order_id, qty, price, est_txt, f"{fx:g}" if fx else "없음",
                stock, f"{theory:,.0f}" if theory else None, s.rt, leg.post_pending,
                self._ledger(s), acc.hl_qty, acc.sf_qty, acc.fx_avg(), acc.hl_avg(),
                acc.sf_avg(), f"{sprd * 100:.3f}%" if sprd is not None else "-(판 미완)")
            # 코어 로그에도 체결가·체결수량·누적·남은 대기(사용자 2026-09-14) — 발주가(@ 주문가)와
            # 구분되게 '체결'을 앞에.
            self._log.info("[자동M] %s 후주문 체결 %s #%s %s %g @ %g %s (누적 %g/%g, HL 대기 %g)",
                           u.value, tag, order.order_id, order.intent.side.value, qty, price,
                           est_txt, order.filled_qty, order.intent.qty, leg.post_pending)
        self._apply(u, ref.index, ref.block, acts, ref.reverse)
        self._trace(u, ref.index, ref.block, leg, ref.reverse)
        self._persist()  # RT·체결차·순잔고 바뀜 → core_state.json (실제 저장은 루프 다음 차례)

    def _current_est(self, u: Underlying, leg: Leg) -> float | None:
        """지금 HL 호가창으로 다시 계산한 est(후주문 방향, 이번 선주문 계약수 × 10) — 판정 때와
        같은 식(build_signals). 호가창이 없으면 None."""
        hl = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        if hl is None or not hl.bid or not hl.ask:
            return None
        qty = (leg.pre_qty or 1) * self._book(u).hl_ratio
        if leg.post_side is Side.SELL:
            return est_price(hl.bids or [(hl.bid, hl.bid_qty or 1.0)], qty)
        return est_price(hl.asks or [(hl.ask, hl.ask_qty or 1.0)], qty)

    @staticmethod
    def _fmt_est(est: float | None) -> str:
        return f"{est:g}" if est else "-"

    @staticmethod
    def _est_vs_fill(est: float | None, side: Side, price: float) -> str:
        """'기준est X 차이 ±d(±p%)' — 선주문 발주 시점 est 대비 후주문 체결가. 유리하면 +
        (HL 매도는 더 높게, 매수는 더 낮게 잡힌 것). est가 없던 판(호가창 없음)은 '기준est -'."""
        if not est:
            return "기준est -"
        diff = price - est if side is Side.SELL else est - price
        return f"기준est {est:g} 차이 {diff:+g}({diff / est * 100:+.3f}%)"

    @staticmethod
    def _ledger(s: AutoMSet) -> str:
        """세트 장부 원값 — 체결차의 출처를 로그에서 따라갈 수 있게(실측 2026-09-10)."""
        return f"장부 SF {s.sf_net} HL {s.hl_net:g} 체결차 {s.fill_diff:g}"

    def _persist(self) -> None:
        """코어 상태 저장 예약 — 실제 저장은 **이벤트 루프의 다음 차례**에 한 번(같은 이벤트 안의
        여러 요청은 하나로).

        2026-09-14: 저장(파일 읽기·비교·임시파일·세대 회전·교체)은 수 ms인데 체결 처리·통보 처리
        안에서 그 자리에서 하면 방금 예약한 후주문 발주 태스크가 그만큼 늦게 시작했다(①→② 지연).
        call_soon은 예약 순서대로 돌므로 먼저 예약된 후주문 태스크의 첫 스텝(전송까지)이 앞선다.
        루프 밖(동기 테스트)이면 그 자리에서 저장.
        """
        if self._save is None or self._persist_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._persist_now()
            return
        self._persist_pending = True
        loop.call_soon(self._persist_now)

    def _persist_now(self) -> None:
        """코어 상태 저장(세대 백업 포함) — 실패해도 판정을 멈추지 않는다."""
        self._persist_pending = False
        if self._save is None:
            return
        t0 = time.perf_counter()
        try:
            self._save()
        except Exception as exc:  # noqa: BLE001 - 저장 실패는 로그만
            self._log.warning("[자동M] 상태 저장 실패 — %s", exc)
        self._persist_ms = (time.perf_counter() - t0) * 1000

    def _on_book_change(self) -> None:
        """취소·거부는 상태 변화로 온다 — 추적 주문의 상태 전이를 한 번씩 처리."""
        for oid, ref in list(self._orders.items()):
            order = self._system.order_book.order(oid)
            if order is None:
                # 장부에서 사라진 추적 주문(재동기 유령 정리 등) — 그대로 두면 "걸려 있다"고
                # 믿고 없는 주문 취소를 되풀이한다(실측 2026-09-09 #20851). 선주문은 취소된 것으로
                # 정리해 다음 판으로, 후주문은 밖에서 끝난 것으로 보고 체결차 → 중지(사람 확인).
                self._recover_vanished(oid, ref)
                continue
            status = order.status.value
            if self._seen_status.get(oid) == status:
                continue
            self._seen_status[oid] = status
            u = ref.underlying
            s = self._set(u, ref.index, ref.reverse)
            mono = time.monotonic()
            self.ulog(u).info("통보 %s: %s주문 #%s 상태 %s (체결 %g/%g)",
                              self._tag(u, ref.index, ref.block, ref.reverse),
                              "선" if ref.leg == "pre" else "후", oid, status,
                              order.filled_qty, order.intent.qty)
            if ref.leg == "pre":
                if status == "cancelled":
                    on_pre_cancelled(s, ref.block, mono, self._settings)
                    self._forget(oid)
                elif status == "rejected":
                    self._apply(u, ref.index, ref.block,
                                on_pre_reject(s, ref.block, mono, self._settings,
                                              reason="LS 거부 통보(접수 뒤 거부)"), ref.reverse)
                    s.leg(ref.block).last_reject_at = time.strftime("%H:%M:%S")
                    self._trace(u, ref.index, ref.block, s.leg(ref.block), ref.reverse)
                    self._forget(oid)
                elif status == "filled":
                    self._forget(oid)
            elif status in ("cancelled", "rejected"):
                # 후주문이 밖에서 취소(사람·거래소)되거나 거부됨 — 엔진은 후주문을 취소하지 않는다
                # (사용자 확정 2026-09-07). 미체결분(소수 그대로)만큼 체결차 → 중지.
                unfilled = round(order.intent.qty - order.filled_qty, 6)
                if unfilled > 1e-9:
                    self._apply(u, ref.index, ref.block, on_post_partial_reject(
                        s, ref.block, unfilled, mono=mono, settings=self._settings),
                        ref.reverse)
                self._forget(oid)
            elif status == "filled":
                self._forget(oid)
            self._trace(u, ref.index, ref.block, s.leg(ref.block), ref.reverse)
            self._persist()  # 취소·거부로 바뀐 상태(체결차·중지) 저장

    def _recover_vanished(self, oid: str, ref: _OrderRef) -> None:
        """장부에서 사라진 추적 주문 정리(실측 2026-09-09 — 재동기가 선물 선주문 #20851을 유령으로
        지워 체결 통보가 미아가 되고 자동M은 'unknown order' 취소를 되풀이). 선주문은 취소 확인과
        같게 다음 판으로, 후주문은 밖에서 끝난 것으로 보고 미체결분 체결차 → 중지(사람이 확인)."""
        u = ref.underlying
        s = self._set(u, ref.index, ref.reverse)
        leg = s.leg(ref.block)
        tag = self._tag(u, ref.index, ref.block, ref.reverse)
        kind = "선" if ref.leg == "pre" else "후"
        self._log.warning("[자동M] %s %s %s주문 #%s 장부에서 사라짐(재동기 등) — 정리",
                          u.value, tag, kind, oid)
        self.ulog(u).warning("통보 %s: %s주문 #%s 장부에서 사라짐 — %s", tag, kind, oid,
                             "취소로 정리" if ref.leg == "pre" else "미체결분 체결차 → 중지")
        if ref.leg == "pre":
            if leg.pre_order_id == oid:
                on_pre_cancelled(s, ref.block, time.monotonic(), self._settings)
        elif leg.post_pending > 1e-9:
            self._apply(u, ref.index, ref.block, on_post_partial_reject(
                s, ref.block, leg.post_pending, mono=time.monotonic(),
                settings=self._settings), ref.reverse)
        self._forget(oid)
        self._trace(u, ref.index, ref.block, s.leg(ref.block), ref.reverse)
        self._persist()

    def _forget(self, oid: str) -> None:
        self._orders.pop(oid, None)
        self._seen_status.pop(oid, None)

    # ---------------------------------------------------------------- 명령 ---
    def set_running(self, u: Underlying, index: int, block: Block, value: bool,
                    reverse: bool = False) -> None:
        s = self._set(u, index, reverse)
        self.ulog(u).info(
            "명령 %s: 실행 %s | 목표 %d 1회 %d 전환 %ds 진입SF %s 진입S %s 청산 %s RT %d",
            self._tag(u, index, block, reverse), "켬" if value else "끔", s.target_qty,
            s.per_qty, s.switch_delay_s, s.en_sf, s.en_s, s.ex_sf, s.rt)
        self._apply(u, index, block, set_running(s, block, value, mono=self._mono), reverse)
        self._trace(u, index, block, s.leg(block), reverse)

    def release(self, u: Underlying, index: int, block: Block,
                reverse: bool = False) -> None:
        """중지 해제 — 상태만 대기로. 세트 장부(SF·HL 순잔고·체결차)는 그대로 두고, 사람이 정리한 뒤
        세트설정 "체결차 Clear"로 0을 만든다(사용자 확정 2026-09-10). 남아 있던 후주문 추적도 유지 —
        그 주문이 나중에 체결되면 장부에 반영된다."""
        s = self._set(u, index, reverse)
        self.ulog(u).info("명령 %s: 중지 해제(세트 단위) — %s (장부는 유지, Clear는 세트설정에서)",
                          self._tag(u, index, block, reverse), self._ledger(s))
        release_halt(s, block)
        for b in (Block.ENTRY, Block.EXIT):
            self._trace(u, index, b, s.leg(b), reverse)
        self._persist()

    def stop_all(self, u: Underlying | None = None) -> None:
        """실행 해제(창 닫기·안전종료) — 미체결 선주문 취소. u=None이면 전 종목."""
        targets = [u] if u is not None else [tu for tu, _b in self.screen.books_of(self.product)]
        for tu in targets:
            for reverse, index, s in self._book(tu).all_sets():
                for block in (Block.ENTRY, Block.EXIT):
                    if s.leg(block).running:
                        self.set_running(tu, index, block, False, reverse)

    async def shutdown(self, timeout_s: float = 3.0) -> None:
        """안전종료 — 전 종목 정지(미체결 선주문 취소 요청)하고 그 취소 요청이 끝날 때까지 기다린다.

        실측 2026-09-08: 0.3초만 기다리고 닫아 취소가 LS 한도에 걸린 선주문이 그대로 남았다.
        """
        self.stop_all()
        if self._bg:
            await asyncio.wait(list(self._bg), timeout=timeout_s)
        left = [oid for oid, ref in self._orders.items() if ref.leg == "pre"]
        if left:
            self._log.warning("[자동M] 종료 — 취소 확인 못 한 선주문 %s (LS에서 확인 필요)", left)
        if self._persist_pending:  # 예약만 된 저장이 루프 종료로 사라지지 않게 지금 쓴다
            self._persist_now()

    # ------------------------------------------------------------- 스냅샷 ---
    def live_snapshot(self) -> dict[str, Any]:
        """화면용 — 종목별 {세트 상태·RT·체결차·누적, 모니터 3칸, HL 호가단위}. 키 = 종목 상태 키
        (주식선물 "종목", 주식 "종목|stock")."""
        from .auto_m import book_key

        return {book_key(u, self.product): self._snapshot_for(u, book)
                for u, book in self.screen.books_of(self.product)}

    def _snapshot_for(self, u: Underlying, book: AutoMBook) -> dict[str, Any]:
        inst = self._counterpart(book)
        # 상단 모니터 3칸(exec §11.9): 기준수량 est 괴리. 정방향 진입 = HL 매수호가창 est,
        # 청산 = 매도호가창; 역방향은 반대(진입 = 매도호가창, 청산 = 매수호가창).
        q = max(1, book.ref_qty)
        ratio = book.hl_ratio
        sf_en, sf_ex = self._system.pair_signal(u, inst, q, q)
        s_en, s_ex = self._system.pair_signal(u, Instrument.KR_STOCK, q * ratio, q * ratio)
        monitor = {"fwd": {"en_sf": sf_en, "en_s": s_en, "ex_sf": sf_ex},
                   "rev": {"en_sf": sf_ex, "en_s": s_ex, "ex_sf": sf_en}}
        if book.product == "stock":  # exec §7C: (HL est×환율 − 1호가)/1호가, 기준수량 est
            from .auto_m import stock_monitor_value

            probe = book.sets[0]
            saved_qty = (probe.per_qty, probe.target_qty, probe.rt)
            probe.per_qty, probe.target_qty, probe.rt = q, q, 0  # 기준수량으로 est 수량
            try:
                sig = self.build_signals(u, book, probe, Block.ENTRY, datetime.now(), self._mono,
                                         False)
            finally:
                probe.per_qty, probe.target_qty, probe.rt = saved_qty
            monitor = {"fwd": {"en_sf": None, "en_s": stock_monitor_value(sig, Side.SELL),
                               "ex_sf": stock_monitor_value(sig, Side.BUY)}}
        fx_used, fx_src = self._system.usdkrw_effective()  # 지금 HL 환산에 쓰는 환율(화면 표시)
        out = [self._set_row(s) for s in book.sets]
        rev_out = [self._set_row(s) for s in book.rev_sets]  # 역방향 3세트(§7A·§7B)
        # HL 호가단위(틱) 옵션 — 일반주문창과 같은 표(가격 자릿수 기반, 코어 계산 §5.10)
        hl = self._system.quotes.get((u, Instrument.HL_PERP, "hl"))
        ref = (hl.ask or hl.bid) if hl is not None else None
        merge_ticks = ([{"tick": s, "n_sig_figs": nsf, "mantissa": mant}
                        for s, nsf, mant in merge_tick_options(float(ref))] if ref else [])
        active_fn = getattr(self._system, "hl_merge_active", None)
        active = active_fn(u) if callable(active_fn) else None
        # SF 시세 호가단위(지금 가격대) — 세트설정 기준배수 검사용(사용자 2026-09-15).
        # 시세 없으면 None(화면은 그 검사를 건너뛴다)
        quotes = self._system.quotes
        markets = self._markets(book)
        sf_q = next((quotes.get((u, inst, m)) for m in markets
                     if quotes.get((u, inst, m)) is not None), None)
        sf_ref = (sf_q.ask or sf_q.bid) if sf_q is not None else None
        if not sf_ref:  # 장 밖(호가 없음)엔 현재가(시동 초기값·마지막 체결)로 — 실측 09-15 저녁:
            # 호가만 보니 sf_tick이 비어 세트설정 기준배수 경고가 안 떴다
            trades = getattr(self._system, "trades", {})
            sf_ref = next((trades.get((u, inst, m)) for m in markets
                           if trades.get((u, inst, m))), None)
        sf_tick = tick_for(Instrument.KR_STOCK_FUTURE, float(sf_ref)) if sf_ref else None
        return {"sets": out, "rev_sets": rev_out, "any_running": book.any_running(),
                "monitor": monitor, "sf_tick": sf_tick,
                "fx": {"used": fx_used, "src": fx_src},  # 사용 환율(값, 출처 현물|선물이론)
                "ref_qty": book.ref_qty, "future_month": book.future_month,
                "market": book.market,  # 주식 거래소(KRX/NXT) — 화면 콤보 복원용
                "hl_merge_ticks": merge_ticks,
                "hl_merge_active": ({"n_sig_figs": active[0], "mantissa": active[1]}
                                    if active is not None else None)}

    @staticmethod
    def _set_row(s: AutoMSet) -> dict[str, Any]:
        """세트 하나의 화면용 행 — RT는 부호 그대로(역방향은 0 또는 음수, 사용자 2026-09-14)."""
        row: dict[str, Any] = {"rt": s.rt, "fill_diff": s.fill_diff, "reverse": s.reverse}
        for name, leg in (("entry", s.entry), ("exit", s.exit)):
            row[name] = {
                "running": leg.running, "status": leg.status.value,
                "halt_reason": leg.halt_reason, "pre_order_id": leg.pre_order_id,
                "pre_price": leg.pre_price, "pre_qty": leg.pre_qty,
                "pre_filled": leg.pre_filled, "post_pending": leg.post_pending,
                # 취소 재전송 한도 초과 → 상태줄 "취소실패 #번호 n회"(exec ㅂ3)
                "cancel_failed": leg.cancel_alarmed, "cancel_tries": leg.cancel_tries,
                # 마지막 선주문 거부(시각·사유·횟수) → 상태줄(사용자 2026-09-15)
                "reject": leg.last_reject, "reject_at": leg.last_reject_at,
                "hl_qty": leg.acc.hl_qty, "sf_qty": leg.acc.sf_qty,
                # 매매결과 표시는 짝이 맞은(적은 쪽) 체결량 기준(사용자 확정 2026-09-08)
                "matched_hl": leg.acc.matched_hl(), "matched_sf": leg.acc.matched_sf(),
                # 매매결과 -HP/+SF 칸 = HL·SF 평균 체결가(수량이 아님, 사용자 2026-09-11)
                "hl_avg": leg.acc.hl_avg(), "sf_avg": leg.acc.sf_avg(),
                # Sprd는 체결 시점 값들의 가중평균이라 판이 끝나면 고정(2026-09-10)
                "fx_avg": leg.acc.fx_avg(), "sprd": leg.acc.sprd(),
                # 마지막으로 끝난 한 판(스냅샷, 사용자 목업 2026-09-16) — 화면 오른쪽 아래 블록.
                # seq로 그 방향에서 가장 최근 판을 고른다
                "last_round": ({
                    "seq": leg.last_round_seq, "hl_avg": leg.last_round.hl_avg(),
                    "sf_avg": leg.last_round.sf_avg(), "fx_avg": leg.last_round.fx_avg(),
                    "sprd": leg.last_round.sprd(),
                } if leg.last_round is not None else None),
            }
        return row
