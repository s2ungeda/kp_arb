"""쌍 대조 로그 — 국내 포지션(주식·주식선물) ↔ HL 포지션 ↔ 세트 장부를 주기적으로 코어 로그에.

exec 결정 83(사용자 2026-10-16): 잔고 쌍이 안 맞아 일반주문으로 HL을 직접 샀는데, 로그에 포지션
숫자가 없어 어느 세트가 언제부터 어긋났는지 짚을 수 없었다. 1분마다 재고, 숫자가 바뀌었거나 10분이
지났으면 종목마다 한 줄. 계산(``pair_rows``)·글(``format_row``)·언제 남길지(``PairLogger.tick``)는
순수, 루프(``pair_check_loop``)만 I/O.

줄 뜻: ``실제 차이 = 주식(주) + 선물(계약)×10 + HL(계약)`` — 헤지가 맞으면 0. ``장부``는 세트들이
잡은 국내·HL 순잔고 합(체결차 합 = 세트가 아는 차이). ``장부 밖 차이 = 실제 차이 − 장부 체결차`` —
세트가 모르는 차이(일반주문·HTS·전날 잔차). 0이 아니면 그 시각 전후를 보면 된다.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from .auto_m import HL_PER_SF, AutoMBook, parse_book_key
from .domain.enums import Instrument, Underlying
from .domain.models import Position

PAIR_CHECK_INTERVAL_S = 60.0    # 재는 주기
PAIR_CHECK_HEARTBEAT_S = 600.0  # 변화 없어도 이 시간마다 한 줄(살아 있다는 표시·시각 기준점)

log = logging.getLogger("kp_arb.pair")


@dataclass(frozen=True)
class PairRow:
    """종목 하나의 쌍 대조 값. 포지션은 롱 +, 숏 −. 장부 국내는 HL 계약 단위로 환산(주식 1,
    SF 10)."""

    underlying: Underlying
    stock: float      # LS 주식·ETF 보유(주)
    sf: float         # 주식선물 근+차근(계약)
    hl: float         # HL 포지션(계약)
    book_dom: float   # 세트 장부 국내 순잔고 합(HL 계약 단위)
    book_hl: float    # 세트 장부 HL 순잔고 합
    book_diff: float  # 세트 체결차 합

    @property
    def actual_diff(self) -> float:
        return self.stock + self.sf * HL_PER_SF + self.hl

    @property
    def unexplained(self) -> float:
        """세트 장부로 설명되지 않는 차이 — 일반주문·HTS·전날 잔차 등."""
        return self.actual_diff - self.book_diff


def pair_rows(positions: Iterable[Position], books: Mapping[str, AutoMBook]) -> list[PairRow]:
    """포지션(장부 실측) + 세트 장부 → 종목별 PairRow(값이 전부 0인 종목은 뺀다). 순수."""
    stock: dict[Underlying, float] = {}
    sf: dict[Underlying, float] = {}
    hl: dict[Underlying, float] = {}
    for p in positions:
        q = p.signed_qty
        if p.instrument in (Instrument.KR_STOCK, Instrument.KR_ETF):
            stock[p.underlying] = stock.get(p.underlying, 0.0) + q
        elif p.instrument in (Instrument.KR_STOCK_FUTURE, Instrument.KR_STOCK_FUTURE_NEXT):
            sf[p.underlying] = sf.get(p.underlying, 0.0) + q
        elif p.instrument is Instrument.HL_PERP:
            hl[p.underlying] = hl.get(p.underlying, 0.0) + q
    bd: dict[Underlying, float] = {}
    bh: dict[Underlying, float] = {}
    bdiff: dict[Underlying, float] = {}
    for key, book in books.items():
        parsed = parse_book_key(key)
        if parsed is None:
            continue
        u = parsed[0]
        for s in [*book.sets, *book.rev_sets]:
            bd[u] = bd.get(u, 0.0) + s.sf_net * s.hl_ratio
            bh[u] = bh.get(u, 0.0) + s.hl_net
            bdiff[u] = bdiff.get(u, 0.0) + s.fill_diff
    out: list[PairRow] = []
    for u in sorted({*stock, *sf, *hl, *bd, *bh, *bdiff}, key=lambda x: x.value):
        row = PairRow(u, stock.get(u, 0.0), sf.get(u, 0.0), hl.get(u, 0.0),
                      bd.get(u, 0.0), bh.get(u, 0.0), bdiff.get(u, 0.0))
        if any(abs(v) > 1e-9 for v in (row.stock, row.sf, row.hl, row.book_dom, row.book_hl,
                                       row.book_diff)):
            out.append(row)
    return out


def _n(v: float) -> str:
    """부호 붙인 숫자 — 정수면 소수 없이, 아니면 소수 3자리까지(끝 0 제거)."""
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):+d}"
    return f"{v:+.3f}".rstrip("0").rstrip(".")


def format_row(r: PairRow) -> str:
    sf_units = _n(r.sf * HL_PER_SF)
    return (f"[쌍대조] {r.underlying.value} 주식 {_n(r.stock)} 선물 {_n(r.sf)}(={sf_units}) "
            f"HL {_n(r.hl)} → 실제 차이 {_n(r.actual_diff)} | 장부 국내 {_n(r.book_dom)} "
            f"HL {_n(r.book_hl)} 체결차 {_n(r.book_diff)} | 장부 밖 차이 {_n(r.unexplained)}")


_Key = tuple[tuple[str, float, float, float, float, float, float], ...]


def _key(rows: Iterable[PairRow]) -> _Key:
    return tuple((r.underlying.value, round(r.stock, 3), round(r.sf, 3), round(r.hl, 3),
                  round(r.book_dom, 3), round(r.book_hl, 3), round(r.book_diff, 3)) for r in rows)


class PairLogger:
    """언제 남길지(순수): 값이 바뀌었거나 heartbeat_s가 지났으면 줄 목록, 아니면 빈 목록."""

    def __init__(self, heartbeat_s: float = PAIR_CHECK_HEARTBEAT_S) -> None:
        self._heartbeat = heartbeat_s
        self._last_key: _Key | None = None
        self._last_at = -math.inf

    def tick(self, rows: list[PairRow], now: float) -> list[str]:
        key = _key(rows)
        if key == self._last_key and now - self._last_at < self._heartbeat:
            return []
        self._last_key, self._last_at = key, now
        if not rows:
            return ["[쌍대조] 포지션·세트 장부 모두 0"]
        return [format_row(r) for r in rows]


async def pair_check_loop(
    positions_of: Callable[[], Iterable[Position]], books_of: Callable[[], Mapping[str, AutoMBook]],
    *, interval_s: float = PAIR_CHECK_INTERVAL_S, heartbeat_s: float = PAIR_CHECK_HEARTBEAT_S,
) -> None:
    """코어 태스크 — interval_s마다 재고 PairLogger가 주는 줄을 INFO로. 예외는 로그만(루프 유지)."""
    import asyncio

    logger = PairLogger(heartbeat_s)
    while True:
        try:
            for line in logger.tick(pair_rows(positions_of(), books_of()), time.monotonic()):
                log.info(line)
        except Exception:  # noqa: BLE001 - 대조 실패가 코어를 멈추면 안 된다
            log.exception("쌍 대조 실패 — 다음 주기에 다시")
        await asyncio.sleep(interval_s)
