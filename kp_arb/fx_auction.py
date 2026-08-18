"""원달러선물 동시호가 대응주문 — 순수 로직 (DESIGN-fx-auction.md §4).

주식선물 신규주문 → 원달러선물 헤지 주문의 **방향·가격·수량**과 **시간창 판정**만 계산한다.
I/O·주문 실행은 코어(감시 태스크)와 게이트웨이 몫. 여기는 부작용 없는 순수 함수.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Any

from .domain.enums import Side, Underlying

FX_TICK = 0.1          # 원달러선물 호가단위(원)
FX_CONTRACT_USD = 10_000  # 원달러선물 1계약 = US$10,000
STOCK_FUT_MULT = 10    # 주식선물 1계약 = 10주


def _hms_to_sec(s: str) -> int | None:
    """'HH:MM' 또는 'HH:MM:SS' → 자정 이후 초. 형식 이상은 None."""
    parts = s.strip().split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    h, m = nums[0], nums[1]
    sec = nums[2] if len(parts) == 3 else 0
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60):
        return None
    return h * 3600 + m * 60 + sec


def in_auction_window(now_hms: str, windows: Iterable[tuple[str, str]]) -> bool:
    """now(HH:MM:SS)가 주어진 시간창 [시작,종료] 중 하나에 드는가(경계 포함).

    windows: [(장전시작, 장전종료), (마감시작, 마감종료)] 같은 (시작,종료) 목록.
    형식이 이상한 값(빈칸 등)은 그 창만 건너뛴다.
    """
    now = _hms_to_sec(now_hms)
    if now is None:
        return False
    for start_s, end_s in windows:
        start, end = _hms_to_sec(start_s), _hms_to_sec(end_s)
        if start is None or end is None:
            continue
        if start <= now <= end:
            return True
    return False


def hedge_side(stock_side: Side) -> Side:
    """주식선물 방향 → 원달러선물 대응 방향(반대). 매수→매도 / 매도→매수."""
    return Side.SELL if stock_side is Side.BUY else Side.BUY


def hedge_price(fx_price: float, tick_count: int, fx_side: Side,
                tick_size: float = FX_TICK) -> float:
    """대응 주문가 — 매수는 현재가 +주문틱, 매도는 −주문틱. 주문틱 = 틱개수 × 틱크기.

    0.1 그리드로 스냅(부동소수 잡음 제거)."""
    offset = tick_count * tick_size
    px = fx_price + offset if fx_side is Side.BUY else fx_price - offset
    steps = round(px / tick_size)
    return round(steps * tick_size, 4)


def hedge_qty(contracts: int, stock_price: float, fx_price: float,
              hedge_ratio: float, *, multiplier: int = STOCK_FUT_MULT,
              fx_contract_usd: float = FX_CONTRACT_USD) -> int:
    """대응 계약수 = 반내림( 계약수 × 승수 × 주식선물주문가 ÷ 현재가 ÷ 계약USD × 헤지비율 ).

    hedge_ratio는 비율(예 50% → 0.5). 헤지비율을 반내림 안에 넣어 정수 계약. 0 이하는 0.
    """
    if fx_price <= 0 or contracts <= 0 or hedge_ratio <= 0:
        return 0
    raw = contracts * multiplier * stock_price / fx_price / fx_contract_usd * hedge_ratio
    return max(0, math.floor(raw))


def compute_hedge(stock_side: Side, contracts: int, stock_price: float,
                  fx_price: float, tick_count: int, hedge_ratio: float,
                  ) -> tuple[Side, float, int]:
    """주식선물 신규주문 → 원달러선물 대응주문 (방향, 주문가, 계약수). 수량 0이면 발주 안 함.

    hedge_ratio는 비율(0.5 = 50%). fx_price는 화면 입력 현재가(고정).
    """
    fx_side = hedge_side(stock_side)
    px = hedge_price(fx_price, tick_count, fx_side)
    qty = hedge_qty(contracts, stock_price, fx_price, hedge_ratio)
    return fx_side, px, qty


@dataclass(frozen=True)
class FuturesAck:
    """LS 선물 접수(O01)에서 뽑은 대응주문 계산용 값."""

    order_id: str
    code: str      # 선물 종목코드 (fnoIsuno, 예 'A1169000'=삼성 / 'A5069000'=하이닉스)
    side: Side
    qty: int       # 계약수 (ordqty)
    price: float   # 주문가 (ordprc)


def parse_futures_ack(body: dict[str, Any]) -> FuturesAck | None:
    """LS 선물 접수(O01) body → FuturesAck. 필드 없거나 이상하면 None.

    실측 키(2026-08-18): fnoIsuno(종목코드)·bnstp(2매수/1매도)·ordqty·ordprc·ordno.
    신규/정정 구분은 호출측이 OrderEvent.org_order_id(=orgordno, 신규면 None)로 판정한다.
    """
    try:
        code = str(body["fnoIsuno"]).strip()
        bnstp = str(body["bnstp"]).strip()
        qty = int(str(body["ordqty"]).strip())
        price = float(str(body["ordprc"]).strip())
        order_id = str(body["ordno"]).strip()
    except (KeyError, ValueError, TypeError):
        return None
    if not code or bnstp not in ("1", "2") or qty <= 0:
        return None
    side = Side.BUY if bnstp == "2" else Side.SELL
    return FuturesAck(order_id=order_id, code=code, side=side, qty=qty, price=price)


@dataclass(frozen=True)
class FxAuctionSettings:
    """화면에서 설정하는 대응주문 인자."""

    windows: tuple[tuple[str, str], ...]  # (시작,종료) 구간들 (장전·마감)
    fx_code: str                          # 원달러선물 종목코드 (콤보 선택)
    price: float                          # 현재가 (고정)
    tick: int                             # 주문틱 개수
    hedge_ratio: float                    # 헤지비율 (0.5 = 50%)


@dataclass(frozen=True)
class HedgeAction:
    """대응 발주 지시 — 실제 발주는 상위(LiveSystem)가 place_fx_futures로 수행."""

    fx_code: str
    side: Side
    qty: int
    price: float
    source_order_id: str  # 원인이 된 주식선물 주문번호


class FxAuctionController:
    """동시호가 대응주문 감시 — 상태 + 신규주문 → 대응 결정(순수). 실제 발주는 상위가 한다.

    resolve_underlying: 선물 종목코드 → Underlying|None (대상 판정용, LiveSystem이 주입)
    now: () -> 'HH:MM:SS' (시간창 판정용)
    targets: 감시할 Underlying 집합(삼성·하이닉스)
    """

    def __init__(self, resolve_underlying: Callable[[str], Underlying | None],
                 now: Callable[[], str], targets: Collection[Underlying]) -> None:
        self._resolve = resolve_underlying
        self._now = now
        self._targets = frozenset(targets)
        self.settings: FxAuctionSettings | None = None
        self.running = False
        self._seen: set[str] = set()  # 대응한 주식선물 주문번호(중복 방지)

    def start(self, settings: FxAuctionSettings) -> None:
        self.settings = settings
        self._seen.clear()
        self.running = True

    def stop(self) -> None:
        self.running = False

    def decide(self, *, kind: str, org_order_id: str | None,
               body: dict[str, Any]) -> HedgeAction | None:
        """주식선물 접수 → 대응주문(HedgeAction) or None. 순수(발주 안 함).

        조건: 실행중 · 신규(ack + 원주문번호 없음) · 대상종목 · 시간창 · 미중복 · 수량>0.
        """
        s = self.settings
        if not self.running or s is None:
            return None
        if kind != "ack" or org_order_id is not None:  # 신규만(정정·취소 제외)
            return None
        ack = parse_futures_ack(body)
        if ack is None:
            return None
        u = self._resolve(ack.code)
        if u is None or u not in self._targets:  # 대상 2종목만
            return None
        if not in_auction_window(self._now(), s.windows):  # 동시호가 시간창
            return None
        if ack.order_id in self._seen:  # 같은 주문 중복 대응 방지
            return None
        self._seen.add(ack.order_id)
        fx_side, fx_px, qty = compute_hedge(
            ack.side, ack.qty, ack.price, s.price, s.tick, s.hedge_ratio)
        if qty <= 0:  # 반내림 0 — 발주 안 함
            return None
        return HedgeAction(fx_code=s.fx_code, side=fx_side, qty=qty,
                           price=fx_px, source_order_id=ack.order_id)
