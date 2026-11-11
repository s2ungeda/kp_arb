"""토탈잔고 감시 — 배수 선을 넘으면 텔레그램. 계약 = DESIGN-balance-watch.md(사용자 2026-11-11).

토탈잔고 = Σ(설정 종목) |주식·ETF 잔고(주)| + |주식선물 계약(근+차근)| × 10 + |HL 포지션|.
원달러선물은 종목이 아니라 자연히 빠진다. 값의 출처는 장부(OrderBook) 포지션 — 실조회 없음.

기준값 = 코어 시동 직후 값(저장 안 함). 비교잔고배수 S를 넘으면(T ≥ B+S 또는 T ≤ B−S) 보내고,
새 기준은 **마지막으로 넘은 선**(B ± n·S). 계산(``balance_rows``·``BalanceWatcher``)·글
(``format_*``)은 순수, ``balance_watch_loop``만 I/O(로그·텔레그램).
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from .auto_m import HL_PER_SF
from .domain.enums import Instrument, Underlying
from .domain.models import Position

log = logging.getLogger("kp_arb.balance")

BALANCE_WATCH_INTERVAL_S = 1.0  # 장부 재계산 주기(순수 계산, I/O 없음)

_NAMES = {Underlying.SAMSUNG: "삼성", Underlying.SK_HYNIX: "하이닉스", Underlying.HYUNDAI: "현대차"}


@dataclass(frozen=True)
class BalanceRow:
    """종목 하나의 부호 있는 포지션(롱 +, 숏 −). 토탈은 절대값으로 더한다."""

    underlying: Underlying
    stock: float  # LS 주식·ETF(주)
    sf: float     # 주식선물 근+차근(계약)
    hl: float     # HL(계약 = 주)

    @property
    def total(self) -> float:
        return abs(self.stock) + abs(self.sf) * HL_PER_SF + abs(self.hl)

    @property
    def is_empty(self) -> bool:
        return self.stock == 0 and self.sf == 0 and self.hl == 0


def balance_rows(positions: Iterable[Position],
                 underlyings: Sequence[Underlying]) -> list[BalanceRow]:
    """장부 포지션 → 설정 종목(순서 유지)마다 BalanceRow. 설정에 없는 종목·원달러선물은 무시."""
    stock: dict[Underlying, float] = {}
    sf: dict[Underlying, float] = {}
    hl: dict[Underlying, float] = {}
    wanted = set(underlyings)
    for p in positions:
        if p.underlying not in wanted:
            continue
        q = p.signed_qty
        if p.instrument in (Instrument.KR_STOCK, Instrument.KR_ETF):
            stock[p.underlying] = stock.get(p.underlying, 0.0) + q
        elif p.instrument in (Instrument.KR_STOCK_FUTURE, Instrument.KR_STOCK_FUTURE_NEXT):
            sf[p.underlying] = sf.get(p.underlying, 0.0) + q
        elif p.instrument is Instrument.HL_PERP:
            hl[p.underlying] = hl.get(p.underlying, 0.0) + q
    return [BalanceRow(u, stock.get(u, 0.0), sf.get(u, 0.0), hl.get(u, 0.0))
            for u in underlyings]


def total_balance(rows: Iterable[BalanceRow]) -> float:
    return sum(r.total for r in rows)


@dataclass(frozen=True)
class Crossing:
    """선을 넘은 사건 — 옛 기준·새 기준·그때 현재값·배수."""

    old_baseline: float
    new_baseline: float
    total: float
    step: float

    @property
    def change(self) -> float:
        """현재값 − 옛 기준(부호 있음)."""
        return self.total - self.old_baseline


class BalanceWatcher:
    """기준값을 들고 선 넘김을 판정한다. ``baseline`` None = 아직 시동 값이 없음."""

    def __init__(self, baseline: float | None = None) -> None:
        self.baseline = baseline
        self.total: float | None = None  # 마지막으로 계산한 토탈(/state 표시용)

    def check(self, total: float, step: float) -> Crossing | None:
        """T ≥ B+S → 기준 B+n·S / T ≤ B−S → 기준 B−n·S (n = 넘은 선의 수). 그 사이면 None."""
        self.total = total
        b = self.baseline
        if b is None or step <= 0:
            return None
        if total >= b + step:
            n = math.floor((total - b) / step)
            new = b + n * step
        elif total <= b - step:
            n = math.floor((b - total) / step)
            new = b - n * step
        else:
            return None
        self.baseline = new
        return Crossing(b, new, total, step)


def _q(x: float, *, signed: bool = False) -> str:
    text = f"{x:+,.0f}" if signed else f"{x:,.0f}"
    return "0" if text in ("+0", "-0") else text


def format_telegram(c: Crossing, rows: Iterable[BalanceRow], when: str) -> str:
    """텔레그램 본문(상세) — 현재값·변화·시각 / 기준 변화·배수·다음 선 / 종목별 부호 있는 포지션."""
    lines = [f"토탈잔고 {_q(c.total)} ({_q(c.change, signed=True)})  {when}",
             f"기준 {_q(c.old_baseline)} → {_q(c.new_baseline)}, 배수 {_q(c.step)}, "
             f"다음 알림 {_q(c.new_baseline + c.step)}↑ / "
             f"{_q(max(c.new_baseline - c.step, 0.0))}↓"]
    for r in rows:
        if r.is_empty:
            continue
        name = _NAMES.get(r.underlying, r.underlying.value)
        lines.append(f"{name} 주식 {_q(r.stock, signed=True)} / 선물 {_q(r.sf, signed=True)}"
                     f"×{HL_PER_SF} / HL {_q(r.hl, signed=True)}")
    return "\n".join(lines)


def format_main_line(baseline: float | None, total: float | None) -> str:
    """메인 창 한 줄(사용자 2026-11-11 "이렇게만"): ``기준 300  잔고 : 500``. 값 없으면 '-'."""
    b = "-" if baseline is None else _q(baseline)
    t = "-" if total is None else _q(total)
    return f"기준 {b}  잔고 : {t}"


async def balance_watch_loop(
    positions_of: Callable[[], Iterable[Position]],
    underlyings: Sequence[Underlying],
    settings_of: Callable[[], tuple[bool, float]],
    watcher: BalanceWatcher,
    notify: Callable[[str], bool],
    *,
    interval_s: float = BALANCE_WATCH_INTERVAL_S,
    clock: Callable[[], str] = lambda: time.strftime("%Y-%m-%d %H:%M:%S"),
) -> None:
    """코어 태스크 — interval_s마다 장부로 토탈잔고를 재고, 첫 값을 기준으로 잡고(시동 값),
    켜져 있으면 선 넘김을 판정해 로그 + 텔레그램(``notify``는 블로킹이라 스레드로)."""
    import asyncio

    while True:
        try:
            rows = balance_rows(positions_of(), underlyings)
            total = total_balance(rows)
            if watcher.baseline is None:
                watcher.baseline = total
                watcher.total = total
                log.info("잔고 감시 기준값 %s (시동 토탈잔고)", _q(total))
            enabled, step = settings_of()
            crossing = watcher.check(total, step if enabled else 0.0)
            if crossing is not None:
                text = format_telegram(crossing, rows, clock())
                log.info("잔고 감시 선 넘김 — %s", text.replace("\n", " | "))
                sent = await asyncio.to_thread(notify, text)
                if not sent:
                    log.warning("잔고 감시 텔레그램 안 보냄(미설정·실패)")
        except Exception:  # noqa: BLE001 - 감시 실패가 코어를 멈추면 안 된다
            log.exception("잔고 감시 실패 — 다음 주기에 다시")
        await asyncio.sleep(interval_s)
