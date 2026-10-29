"""동시호가 주문 — 순수 로직(I/O 없음). 계약: DESIGN-auction-order.md(ver2, 2026-10-22).

장마감 동시호가 끝 무렵에 ① 스프레드로 적어 둔 주식선물 예약주문 행(매수 8·매도 8) 가운데 그
순간 예상체결가로 체결될 것만 골라 발주(프로세스1), ② 체결 예정 순 수량만큼 원달러선물을 미리
헤지(프로세스2), ③ 15:45 체결 확정 뒤 HL 반대 포지션을 기준가 ± 오프셋 4건으로 발주하고 정정·취소는
버튼(프로세스3).
여기엔 계산·판정만 있다 — 타이머·발주·통보는 엔진(auction_order_engine), 입력·표시는 화면.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .domain.enums import Instrument, Side
from .fx_auction import hedge_price, hedge_qty, hedge_side
from .hl_merge import merge_tick_size
from .hl_price import hl_price_step
from .ticks import ceil_to_tick, floor_to_tick, tick_for

ROWS_PER_SIDE = 8                 # 매수 8·매도 8(ver2 P24)
HL_ROWS: tuple[str, ...] = ("a", "b", "c", "d")  # HL 4건(ver2 G32~G35)
HL_SPLIT_DECIMALS = 2             # HL 4분할 소수 2자리(ver2 P61, 사용자 10-22 답 6)
# HL 각 건의 최소 수량 규칙은 없다(사용자 2026-10-26): 주식선물 1계약 체결이 최소라 그때 4분할한
# 값이 구조상 하한이고, 그보다 작아질 수 없다. 소수 자리에서 0이 되는 건만 만들지 않는다.
SOURCE = "동시호가"               # OrderIntent.source(주문 리스트 출처 구분)

DEFAULT_START_BEFORE_S = 5.0      # 프로세스 개시 = 동시호가 종료 x초 전(ver2 B15)
DEFAULT_LS_HMS, DEFAULT_LS_MS, DEFAULT_LS_ITV = "15:44:55", 100, 200   # ver2 G21·I21·J21
DEFAULT_FX_HMS, DEFAULT_FX_MS, DEFAULT_FX_ITV = "15:44:58", 200, 200   # ver2 G22·I22·J22
DEFAULT_TICK, DEFAULT_HEDGE_PCT, DEFAULT_FX_UNIT = 20, 100, 10          # ver2 F26~K27


# --------------------------------------------------------------------------- 상태(저장)

@dataclass
class Row:
    """예약주문 한 행 — 스프레드(소수)·수량(주식선물 계약수)·켬. 켬 상태도 저장(§6)."""

    spread: float | None = None
    qty: int = 0
    on: bool = False

    def valid(self) -> bool:
        return self.spread is not None and self.qty > 0


def _rows() -> list[Row]:
    return [Row() for _ in range(ROWS_PER_SIDE)]


@dataclass
class AuctionSettings:
    """종목별 설정(§6) — 전부 사용자 입력. ms = 초 아래 시각, 인터벌 = 건 사이 간격(ms)."""

    start_before_s: float = DEFAULT_START_BEFORE_S
    ls_hms: str = DEFAULT_LS_HMS
    ls_ms: int = DEFAULT_LS_MS
    ls_itv_ms: int = DEFAULT_LS_ITV
    fx_hms: str = DEFAULT_FX_HMS
    fx_ms: int = DEFAULT_FX_MS
    fx_itv_ms: int = DEFAULT_FX_ITV
    tick: int = DEFAULT_TICK            # 환 주문가 ±x틱(0.1원 틱) — 음수면 반대쪽(2026-10-27)
    hedge_pct: int = DEFAULT_HEDGE_PCT  # 헤지비율(%)
    fx_unit: int = DEFAULT_FX_UNIT      # 환 1회주문수량(계약)
    hl_off: dict[str, float] = field(default_factory=lambda: dict.fromkeys(HL_ROWS, 0.0))
    hl_pt: dict[str, float] = field(default_factory=lambda: dict.fromkeys(HL_ROWS, 0.0))


@dataclass
class AuctionBook:
    """종목 하나의 저장 상태 — 16행·월물·설정·HL 행↔주문번호. 예약실행(2·3)은 저장하지 않는다."""

    buy: list[Row] = field(default_factory=_rows)
    sell: list[Row] = field(default_factory=_rows)
    month: str = "near"                 # "near" | "next"
    settings: AuctionSettings = field(default_factory=AuctionSettings)
    hl_orders: dict[str, str] = field(default_factory=dict)  # 행 a~d → HL 주문번호(재시동 복원)

    def rows_of(self, side: Side) -> list[Row]:
        return self.buy if side is Side.BUY else self.sell


@dataclass
class AuctionScreen:
    """동시호가 주문 전체 — 종목별 상태(core_state.json). 키 = Underlying.value."""

    books: dict[str, AuctionBook] = field(default_factory=dict)

    def book(self, key: str) -> AuctionBook:
        return self.books.setdefault(key, AuctionBook())


# --------------------------------------------------------------------------- 시각

def hms_to_sec(hms: str) -> int | None:
    """"HH:MM:SS" → 하루 중 초. 형식이 틀리면 None."""
    parts = hms.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        return None
    return h * 3600 + m * 60 + s


def order_moment(hms: str, ms: int) -> float | None:
    """주문시각(하루 중 초, 소수) = HH:MM:SS + ms, **PC 시계 그대로**. 서버 시각과의 차이(10-07 실측
    0.5초+)는 코어가 보정하지 않고 사용자가 주문시각에 넣는다(사용자 2026-10-26 "그 시각은 사용자가
    알아서 한다")."""
    base = hms_to_sec(hms)
    if base is None:
        return None
    return base + ms / 1000.0


def sec_of_day(wall: float, tz_offset_s: int = 9 * 3600) -> float:
    """epoch(초) → 하루 중 초(KST). 엔진이 time.time()으로 부른다."""
    return (wall + tz_offset_s) % 86400.0


# --------------------------------------------------------------------------- 프로세스1

def hl_mid(bid: float | None, ask: float | None) -> float | None:
    """HL bbo 중간값(ver2 P20). 둘 다 있어야."""
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def expected_order_price(sf_theory: float | None, fx_expected: float | None,
                         hl_px: float | None, s: float | None, spread: float | None,
                         side: Side) -> float | None:
    """역산 예상주문가(§2, 사용자 10-22 답 2 "체결쏴 역산가 방식") = SF이론가 × (1 + (환×HL − S)/S
    − s), 주식선물 호가단위에 **매수 내림·매도 올림**(그 가격에 잡히면 스프레드가 보장되는 쪽).
    재료가 하나라도 없으면 None(두 예상체결가가 다 들어온 뒤에만 계산 — §0a-1)."""
    if (sf_theory is None or fx_expected is None or hl_px is None or s is None
            or spread is None or sf_theory <= 0 or s <= 0 or fx_expected <= 0 or hl_px <= 0):
        return None
    hl_disp = (fx_expected * hl_px - s) / s
    raw = sf_theory * (1.0 + hl_disp - spread)
    if raw <= 0:
        return None
    tick = tick_for(Instrument.KR_STOCK_FUTURE, raw)
    return floor_to_tick(raw, tick) if side is Side.BUY else ceil_to_tick(raw, tick)


def monitor_spread(sf_expected: float | None, sf_theory: float | None,
                   fx_expected: float | None, hl_px: float | None,
                   s: float | None) -> float | None:
    """모니터 스프레드(§1 식에 P = 주식선물 **예상체결가**, 사용자 2026-10-26) =
    (환×HL − S)/S − (예상체결가 − SF이론가)/SF이론가. HL은 방향별 1호가(정방향매도 = HL 매수1호가,
    역방향매수 = HL 매도1호가 — 체결쏴 모니터와 같은 쪽). 재료가 없으면 None."""
    if (sf_expected is None or sf_theory is None or fx_expected is None or hl_px is None
            or s is None or sf_theory <= 0 or s <= 0 or hl_px <= 0 or fx_expected <= 0):
        return None
    return (fx_expected * hl_px - s) / s - (sf_expected - sf_theory) / sf_theory


def row_fires(side: Side, order_px: float, sf_expected: float) -> bool:
    """행 판정(§3): 매수 = 예상주문가 > 예상체결가, 매도 = 예상주문가 < 예상체결가. 같으면 안 냄."""
    return order_px > sf_expected if side is Side.BUY else order_px < sf_expected


def fire_order(book: AuctionBook) -> list[tuple[Side, int]]:
    """발주 순서(§3, ver2 P25) — 켜진 행을 **위에서 아래로**(화면 순: 매도 1~8, 매수 1~8 —
    호가창처럼 매도 위·매수 아래, 사용자 2026-10-29). 수량·스프레드가 없는 행은 켜져 있어도
    뺀다(화면이 켜기를 막지만 저장 복원 뒤를 대비)."""
    out: list[tuple[Side, int]] = []
    for side in (Side.SELL, Side.BUY):
        for i, row in enumerate(book.rows_of(side)):
            if row.on and row.valid():
                out.append((side, i))
    return out


# --------------------------------------------------------------------------- 프로세스2

@dataclass(frozen=True)
class PlacedOrder:
    """프로세스1이 낸(접수된) 주식선물 주문 — 프로세스2 수량 셈의 재료."""

    side: Side
    price: float
    qty: int


def fillable_qty(orders: list[PlacedOrder], sf_expected: float) -> tuple[int, int]:
    """그 시점 예상체결가로 **체결될** 수량(ver2 R33) — (매수 합, 매도 합).
    매수 = 예상체결가 < 매수 주문가인 건, 매도 = 예상체결가 > 매도 주문가인 건. 같으면 안 센다."""
    buy = sum(o.qty for o in orders if o.side is Side.BUY and sf_expected < o.price)
    sell = sum(o.qty for o in orders if o.side is Side.SELL and sf_expected > o.price)
    return buy, sell


def net_exposure(buy_qty: int, sell_qty: int) -> tuple[Side, int] | None:
    """매수·매도가 같이 있으면 차이(순 수량)만, 방향은 큰 쪽(ver2 R34·P40 자전 방지). 0이면 None."""
    net = buy_qty - sell_qty
    if net == 0:
        return None
    return (Side.BUY, net) if net > 0 else (Side.SELL, -net)


def fx_hedge_plan(sf_side: Side, contracts: int, sf_expected: float, fx_expected: float,
                  tick: int, hedge_pct: int, unit: int) -> tuple[Side, float, list[int]]:
    """원달러선물 헤지(§4) — (방향, 주문가, 분할 수량 목록). 방향은 주식선물의 반대, 수량은 기존
    `fx_auction.hedge_qty`에 현재가 대신 **예상체결가**, 가격은 예상체결가 ± x틱(매수 +, 매도 −),
    1회주문수량씩 나누고 나머지는 **맨 마지막**(ver2 P36·P37: 85계약·1회 20 → 20×4 + 5)."""
    fx_side = hedge_side(sf_side)
    total = hedge_qty(contracts, sf_expected, fx_expected, hedge_pct / 100.0)
    price = hedge_price(fx_expected, tick, fx_side)
    return fx_side, price, split_qty(total, unit)


def split_qty(total: int, unit: int) -> list[int]:
    """총량을 1회주문수량씩 — 나머지는 마지막 건. unit이 0 이하면 한 건."""
    if total <= 0:
        return []
    if unit <= 0 or unit >= total:
        return [total]
    full, rest = divmod(total, unit)
    return [unit] * full + ([rest] if rest else [])


# --------------------------------------------------------------------------- 프로세스3

def hl_side(sf_side: Side) -> Side:
    """HL 헤지 방향 = 주식선물 체결의 반대(선물 매수 체결 → HL 매도)."""
    return Side.SELL if sf_side is Side.BUY else Side.BUY


def hl_grid_step(price: float, n_sig_figs: int | None, mantissa: int | None,
                 sz_decimals: int | None) -> float:
    """기준가 격자 한 칸 — 윗줄 호가단위(머지) 콤보가 있으면 그 틱(merge_tick_size), 없으면 HL
    가격 격자 한 칸(hl_price_step)."""
    if n_sig_figs is not None or mantissa is not None:
        return merge_tick_size(price, n_sig_figs, mantissa)
    return hl_price_step(price, sz_decimals)


def snap_nearest(price: float, step: float) -> float:
    """격자에 **가장 가까운** 값, 가운데면 **높은 쪽**(ver2 Q52)."""
    if step <= 0:
        return price
    q = (Decimal(repr(price)) / Decimal(repr(step))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(q * Decimal(repr(step)))


def hl_base_price(bid: float | None, ask: float | None, n_sig_figs: int | None,
                  mantissa: int | None, sz_decimals: int | None) -> float | None:
    """프로세스3 기준가(§5) = (매도1 + 매수1)/2에 가장 가까운 호가(격자), 둘이면 높은 쪽."""
    mid = hl_mid(bid, ask)
    if mid is None:
        return None
    return snap_nearest(mid, hl_grid_step(mid, n_sig_figs, mantissa, sz_decimals))


def hl_fill_summary(fills: Iterable[tuple[float, float]]) -> tuple[float, float | None]:
    """HL 4건(정정으로 바뀐 주문 포함)의 (체결수량 합, 평균체결가 — 수량 가중)(§5 체결 표시,
    사용자 2026-10-29). 입력은 주문별 (체결수량, 그 주문의 평균체결가). 체결이 없으면 (0, None).
    수량 0 이하인 항목은 뺀다."""
    qty = 0.0
    amount = 0.0
    for q, px in fills:
        if q <= 0:
            continue
        qty += q
        amount += q * px
    if qty <= 0:
        return 0.0, None
    return qty, amount / qty


def hl_order_price(base: float, offset: float, side: Side, sz_decimals: int | None) -> float:
    """HL 건 가격(§5) = 매수 `기준가 + 오프셋` / 매도 `기준가 − 오프셋`(USD, 양·음, 비면 0) — HL
    격자 한 칸에 가장 가까운 값으로. 매도는 입력값에 −1(사용자 2026-10-28 — 양수 = 상대호가 쪽,
    음수 = 반대쪽, 정정PT와 같은 규칙)."""
    px = base + offset if side is Side.BUY else base - offset
    if px <= 0:
        return px
    return snap_nearest(px, hl_price_step(px, sz_decimals))


def hl_split(total: float, sz_decimals: int | None, n: int = len(HL_ROWS),
             decimals: int = HL_SPLIT_DECIMALS) -> list[float]:
    """HL 총량을 n건 균등 분할(소수 decimals 자리, 종목 szDecimals 한도 안) — 나머지는 **a에**.
    최소 수량 규칙은 없다(사용자 2026-10-26 — 1계약 체결의 4분할이 구조상 하한). 다만 소수 자리에서
    0이 되는 건은 만들지 않는다(그만큼 건수를 줄임). 총량이 한 자리도 안 되면 빈 목록."""
    if total <= 0:
        return []
    dec = decimals if sz_decimals is None else min(decimals, sz_decimals)
    quantum = Decimal(1).scaleb(-dec)
    tot = Decimal(repr(total)).quantize(quantum, rounding="ROUND_FLOOR")
    if tot <= 0:
        return []
    count = min(n, int(tot / quantum))  # 한 건이 0이 되지 않는 최대 건수
    each = (tot / count).quantize(quantum, rounding="ROUND_FLOOR")
    parts = [each] * count
    parts[0] += tot - each * count  # 나머지는 a
    return [float(p) for p in parts]


def hl_amend_price(price: float, pt: float, side: Side, sz_decimals: int | None) -> float:
    """정정가(§5 정정PT) — 잔량을 상대호가 방향으로: 매수면 가격 ↑, 매도면 ↓, 격자에 맞춤. PT가
    음수면 반대쪽(사용자 2026-10-28, 시험용 — 체결 안 되게)."""
    px = price + pt if side is Side.BUY else price - pt
    return snap_nearest(px, hl_price_step(px, sz_decimals)) if px > 0 else px


# --------------------------------------------------------------------------- 저장/복원

def _row_from(raw: object) -> Row:
    if not isinstance(raw, dict):
        return Row()
    sp = raw.get("spread")
    spread: float | None
    try:
        spread = None if sp is None or sp == "" else float(sp)
    except (TypeError, ValueError):
        spread = None
    try:
        qty = max(0, int(float(raw.get("qty") or 0)))
    except (TypeError, ValueError):
        qty = 0
    return Row(spread=spread, qty=qty, on=bool(raw.get("on", False)))


def settings_from_dict(raw: object) -> AuctionSettings:
    """저장값 → 설정. 모르는 키·틀린 값은 기본값(화면이 형식을 막지만 손으로 고친 파일 대비)."""
    st = AuctionSettings()
    if not isinstance(raw, dict):
        return st
    for name in ("ls_hms", "fx_hms"):
        v = raw.get(name)
        if isinstance(v, str) and hms_to_sec(v) is not None:
            setattr(st, name, v)
    for name in ("ls_ms", "ls_itv_ms", "fx_ms", "fx_itv_ms", "tick", "hedge_pct", "fx_unit"):
        try:
            setattr(st, name, int(float(raw.get(name, getattr(st, name)))))
        except (TypeError, ValueError):
            pass
    try:
        st.start_before_s = float(raw.get("start_before_s", st.start_before_s))
    except (TypeError, ValueError):
        pass
    for name in ("hl_off", "hl_pt"):
        d = raw.get(name)
        if isinstance(d, dict):
            out = dict.fromkeys(HL_ROWS, 0.0)
            for k in HL_ROWS:
                try:
                    out[k] = float(d.get(k, 0.0) or 0.0)
                except (TypeError, ValueError):
                    pass
            setattr(st, name, out)
    return st


def book_from_dict(raw: object) -> AuctionBook:
    b = AuctionBook()
    if not isinstance(raw, dict):
        return b
    for side_key in ("buy", "sell"):
        rows = raw.get(side_key)
        if isinstance(rows, list):
            got = [_row_from(r) for r in rows[:ROWS_PER_SIDE]]
            got += [Row() for _ in range(ROWS_PER_SIDE - len(got))]
            setattr(b, side_key, got)
    month = raw.get("month")
    b.month = month if month in ("near", "next") else "near"
    b.settings = settings_from_dict(raw.get("settings"))
    orders = raw.get("hl_orders")
    if isinstance(orders, dict):
        b.hl_orders = {k: str(v) for k, v in orders.items() if k in HL_ROWS and v}
    return b


def screen_from_dict(screen: AuctionScreen, raw: object) -> None:
    if not isinstance(raw, dict):
        return
    books = raw.get("books")
    if isinstance(books, dict):
        for key, val in books.items():
            screen.books[str(key)] = book_from_dict(val)


def apply_rows(book: AuctionBook, body: dict[str, Any]) -> list[str]:
    """화면 명령(행 설정) → 행 반영. body: {"buy": [{spread, qty, on}, ...], "sell": [...]}(일부만
    와도 됨). 거부 사유 목록(비면 OK). 켠 행에 스프레드·수량이 없으면 거부(§2 켜기 조건)."""
    errors: list[str] = []
    for side_key, label in (("buy", "매수"), ("sell", "매도")):
        rows = body.get(side_key)
        if not isinstance(rows, list):
            continue
        parsed = [_row_from(r) for r in rows[:ROWS_PER_SIDE]]
        for i, r in enumerate(parsed):
            if r.on and not r.valid():
                errors.append(f"{label} {i + 1}행: 켜려면 주문Sprd·수량이 있어야 합니다")
        if not errors:
            target = getattr(book, side_key)
            for i, r in enumerate(parsed):
                target[i] = r
    return errors


def math_isclose(a: float, b: float) -> bool:
    """테스트·엔진 공용 — 가격 비교(부동소수 잡음)."""
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9)
