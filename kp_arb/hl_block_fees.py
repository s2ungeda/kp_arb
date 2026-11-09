"""HL 블록 안 주문의 우선순위 수수료 분포 — 순수 로직(I/O 없음). 도구는 tools/hl_block_fees.py.

HL은 블록체인이라 블록 안 순서가 "도착 시각 + f(우선순위 수수료)"로 정렬된다(공식 문서
for-developers/api/priority-fees, 2026-11-09 확인). 주문 action의 ``grouping``이 ``{"p": n}`` 이면
우선순위 수수료를 낸 것이고 **p ÷ 10,000 = bp**(p 80,000 = 8bp, IOC 지연 단축 상한). 실측 2026-11-04
08:00 개장 18블록: IOC의 10%가 1~8bp, 1%가 8bp 이상(최대 11.6bp); ALO(메이커)의 절반은 p 1~99(사실상
0원)로 같은 가격 400ms 안 줄 서기용.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

P_PER_BP = 10_000  # grouping p → bp
BUCKETS = ("없음(na)", "p=0", "p 1~99(≈0, 줄서기)", "<1bp", "1~8bp", "≥8bp")


def bp_of(p: int) -> float:
    """우선순위 p → bp."""
    return p / P_PER_BP


def fee_bucket(p: int | None) -> str:
    """p 값을 표의 구간 이름으로."""
    if p is None:
        return BUCKETS[0]
    if p == 0:
        return BUCKETS[1]
    if p < 100:
        return BUCKETS[2]
    if p < P_PER_BP:
        return BUCKETS[3]
    if p < 8 * P_PER_BP:
        return BUCKETS[4]
    return BUCKETS[5]


def priority_of(action: dict[str, Any]) -> int | None:
    """주문 action의 우선순위 p(없으면 None) — grouping이 ``{"p": n}`` 꼴일 때만."""
    g = action.get("grouping")
    if isinstance(g, dict) and isinstance(g.get("p"), int):
        return int(g["p"])
    return None


def tif_of(order: dict[str, Any]) -> str:
    """주문 1건의 종류 — Gtc/Ioc/Alo/FrontendMarket, 조건부는 trigger."""
    t = order.get("t") or {}
    if isinstance(t, dict) and "limit" in t:
        return str((t["limit"] or {}).get("tif", "?"))
    return "trigger"


@dataclass
class BlockSummary:
    """블록 하나의 주문 통계."""

    height: int
    time_ms: int
    n_tx: int = 0
    n_orders: int = 0            # 주문 action 수(묶음은 1)
    n_cancels: int = 0
    by_tif: dict[str, Counter[str]] = field(default_factory=dict)   # tif → 구간별 주문 건수
    max_p: dict[str, int] = field(default_factory=dict)             # tif → 최대 p
    mine: list[tuple[int, dict[str, Any]]] = field(default_factory=list)  # (블록 안 위치, action)
    # 자산(a)별 수수료 낸 테이커(Ioc) 건수 — 어느 종목에서 순서 싸움이 났나
    paid_ioc_by_asset: Counter[int] = field(default_factory=Counter)
    # 주소별 행동 — 종류별 주문 건수·취소 수·낸 수수료 최대 p (LP형/테이커형 가르기용)
    by_user: dict[str, UserStats] = field(default_factory=dict)


@dataclass
class UserStats:
    """지갑 주소 하나의 구간 행동."""

    orders: Counter[str] = field(default_factory=Counter)  # tif → 주문 건수
    cancels: int = 0
    paid: int = 0        # 수수료(p ≥ 100) 낸 주문 건수
    max_p: int = -1

    @property
    def n_orders(self) -> int:
        return sum(self.orders.values())

    def kind(self) -> str:
        """행동 분류 — 메이커형(ALO 80%↑), 테이커형(IOC 80%↑), 섞임."""
        n = self.n_orders
        if n == 0:
            return "취소만"
        if self.orders.get("Alo", 0) >= 0.8 * n:
            return "메이커형(LP)"
        if self.orders.get("Ioc", 0) >= 0.8 * n:
            return "테이커형"
        return "섞임"


def summarize_block(height: int, block: dict[str, Any], me: str | None = None,
                    assets: set[int] | None = None) -> BlockSummary:
    """탐색기 blockDetails 응답(``blockTime``·``txs``) → BlockSummary. me는 내 주소(소문자 비교).
    assets가 있으면 그 자산 번호(주문의 ``a``)의 주문만 센다(취소는 자산을 알 수 없어 전부 센다).
    블록 전체 tx 수와 내 주문 위치는 필터와 무관."""
    txs = block.get("txs") or []
    s = BlockSummary(height=height, time_ms=int(block.get("blockTime") or 0), n_tx=len(txs))
    me_l = me.lower() if me else None
    for i, tx in enumerate(txs):
        action = tx.get("action") or {}
        kind = str(action.get("type", ""))
        user = str(tx.get("user", "")).lower()
        if me_l and user == me_l:
            s.mine.append((i, action))
        if kind.startswith("cancel"):
            s.n_cancels += 1
            if assets is None:  # 취소는 자산을 몰라 종목 필터 땐 주소별로 안 센다
                s.by_user.setdefault(user, UserStats()).cancels += 1
            continue
        if kind != "order":
            continue
        orders = [o for o in (action.get("orders") or [])
                  if assets is None or o.get("a") in assets]
        if not orders:
            continue
        s.n_orders += 1
        p = priority_of(action)
        us = s.by_user.setdefault(user, UserStats())
        if p is not None and p >= 100:
            us.paid += len(orders)
        if p is not None and p > us.max_p:
            us.max_p = p
        for o in orders:
            tif = tif_of(o)
            us.orders[tif] += 1
            s.by_tif.setdefault(tif, Counter())[fee_bucket(p)] += 1
            if p is not None and p > s.max_p.get(tif, -1):
                s.max_p[tif] = p
            if tif == "Ioc" and p is not None and p >= 100 and isinstance(o.get("a"), int):
                s.paid_ioc_by_asset[int(o["a"])] += 1
    return s


def merge_users(summaries: list[BlockSummary]) -> dict[str, UserStats]:
    """여러 블록의 주소별 행동 합치기."""
    out: dict[str, UserStats] = {}
    for s in summaries:
        for user, us in s.by_user.items():
            m = out.setdefault(user, UserStats())
            m.orders.update(us.orders)
            m.cancels += us.cancels
            m.paid += us.paid
            m.max_p = max(m.max_p, us.max_p)
    return out


def format_users(users: dict[str, UserStats], me: str | None = None, limit: int = 12) -> list[str]:
    """주문 많은 주소 순 한 줄씩 — 주소 앞 10자, 분류, 종류별 건수, 취소, 수수료 건수·최대 bp."""
    me_l = me.lower() if me else None
    rows = sorted(users.items(), key=lambda kv: -(kv[1].n_orders + kv[1].cancels))
    lines: list[str] = []
    for user, us in rows[:limit]:
        tifs = " ".join(f"{k} {v}" for k, v in us.orders.most_common())
        fee = (f" 수수료 {us.paid}건(최대 {bp_of(us.max_p):.2f}bp)" if us.paid else "")
        tag = " ← 나" if me_l and user == me_l else ""
        lines.append(f"{user[:10]}… {us.kind():9s} 주문 {us.n_orders:5,} [{tifs}] "
                     f"취소 {us.cancels:5,}{fee}{tag}")
    return lines


def top_paid_assets(summaries: list[BlockSummary], names: dict[int, str] | None = None,
                    limit: int = 8) -> list[str]:
    """수수료(p ≥ 100) 낸 Ioc 건수가 많은 자산 순 — "자산번호(이름) n건"."""
    total: Counter[int] = Counter()
    for s in summaries:
        total.update(s.paid_ioc_by_asset)
    out = []
    for a, n in total.most_common(limit):
        name = (names or {}).get(a)
        out.append(f"{a}({name}) {n}건" if name else f"{a} {n}건")
    return out


def asset_id(dex_index: int, index: int) -> int:
    """HL 자산 번호 — 빌더 dex는 100000 + dex번호×10000 + 메타 순번(xyz = 1 → 110000+),
    본 dex(0)는 메타 순번 그대로."""
    return index if dex_index == 0 else 100_000 + dex_index * 10_000 + index


def merge_summaries(
    summaries: list[BlockSummary],
) -> tuple[dict[str, Counter[str]], dict[str, int]]:
    """여러 블록의 tif별 구간 건수·최대 p 합치기."""
    by_tif: dict[str, Counter[str]] = {}
    max_p: dict[str, int] = {}
    for s in summaries:
        for tif, c in s.by_tif.items():
            by_tif.setdefault(tif, Counter()).update(c)
        for tif, p in s.max_p.items():
            if p > max_p.get(tif, -1):
                max_p[tif] = p
    return by_tif, max_p


def kst(ms: int) -> str:
    """epoch ms → 이 PC 시간대의 HH:MM:SS.mmm."""
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S.%f")[:-3]


def format_block_line(s: BlockSummary) -> str:
    tifs = {tif: sum(c.values()) for tif, c in s.by_tif.items()}
    paid = sum(v for c in s.by_tif.values() for k, v in c.items() if k not in BUCKETS[:2])
    mine = f"  ← 내 tx 위치 {[i for i, _ in s.mine]}" if s.mine else ""
    return (f"블록 {s.height} {kst(s.time_ms)} tx {s.n_tx:6,} 주문 {s.n_orders:5,}"
            f"(수수료 있음 {paid:5,}) 취소 {s.n_cancels:5,} {tifs}{mine}")


def format_summary(by_tif: dict[str, Counter[str]], max_p: dict[str, int]) -> list[str]:
    """tif별 한 줄 — 건수 많은 순, 구간은 BUCKETS 순."""
    lines: list[str] = []
    for tif, c in sorted(by_tif.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(c.values())
        parts = ", ".join(f"{b} {c[b]:,}({c[b] * 100 // tot}%)" for b in BUCKETS if c.get(b))
        mp = max_p.get(tif)
        mp_txt = f" / 최대 {bp_of(mp):.2f}bp" if mp is not None else ""
        lines.append(f"{tif:14s} {tot:7,}건: {parts}{mp_txt}")
    return lines


def find_block_at(target_ms: int, get_time: Callable[[int], int], ref_height: int,
                  ref_time_ms: int, ms_per_block: float = 70.7, max_calls: int = 60) -> int:
    """target_ms 이상인 첫 블록 높이 — get_time(높이)→blockTime 로 괄호를 넓힌 뒤 이진 탐색.
    get_time만 I/O(주입). 기준점(ref)과 평균 간격으로 첫 추정을 잡는다."""
    calls = 0
    guess = int(ref_height + (target_ms - ref_time_ms) / ms_per_block)
    lo = hi = max(guess, 1)
    t = get_time(lo)
    calls += 1
    step = max(int(1000 / ms_per_block), 1)  # 1초치부터 두 배씩
    if t < target_ms:
        while t < target_ms and calls < max_calls:
            lo, hi = hi, hi + step
            t = get_time(hi)
            calls += 1
            step *= 2
    else:
        while t >= target_ms and calls < max_calls and lo > 1:
            hi, lo = lo, max(lo - step, 1)
            t = get_time(lo)
            calls += 1
            step *= 2
    while hi - lo > 1 and calls < max_calls:
        mid = (lo + hi) // 2
        if get_time(mid) < target_ms:
            lo = mid
        else:
            hi = mid
        calls += 1
    return hi
