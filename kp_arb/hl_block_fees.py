"""HL 블록 안 주문의 우선순위 수수료 분포 — 순수 로직(I/O 없음). 도구는 tools/hl_block_fees.py.

HL은 블록체인이라 블록 안 순서가 "도착 시각 + f(우선순위 수수료)"로 정렬된다(공식 문서
for-developers/api/priority-fees, 2026-11-09 확인). 주문 action의 ``grouping``이 ``{"p": n}`` 이면
우선순위 수수료를 낸 것이고 **p ÷ 10,000 = bp**(p 80,000 = 8bp, IOC 지연 단축 상한). 실측 2026-11-04
08:00 개장 18블록: IOC의 10%가 1~8bp, 1%가 8bp 이상(최대 11.6bp); ALO(메이커)의 절반은 p 1~99(사실상
0원)로 같은 가격 400ms 안 줄 서기용.
"""
from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

P_PER_BP = 10_000  # grouping p → bp
BUCKETS = ("없음(na)", "p=0", "p 1~99(≈0, 줄서기)", "<1bp", "1~8bp", "≥8bp")


POS_GROUPS = ("≥1bp", "<1bp", "무료")  # IOC 위치 비교 묶음(무료 = p 없음·0·1~99)


def pos_group(p: int | None) -> str:
    """p → 위치 비교 묶음. 1bp(=p 10,000)부터가 블록 안 순서를 실제로 바꾸는 크기(45ms/bp)."""
    if p is None or p < 100:
        return POS_GROUPS[2]
    return POS_GROUPS[0] if p >= P_PER_BP else POS_GROUPS[1]


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
class Competitor:
    """내 주문보다 앞에 선 같은 종목 주문 하나."""

    index: int          # 블록 안 위치
    asset: int
    side: str           # "B" | "A"
    tif: str
    p: int | None
    sz: float
    same_side: bool     # 내 주문과 같은 방향(= 같은 잔량을 뺏는 상대)


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
    # 내 tx — (블록 안 위치, action, tx 해시). 해시로 체결(userFills의 hash)과 잇는다
    mine: list[tuple[int, dict[str, Any], str]] = field(default_factory=list)
    # 자산(a)별 수수료 낸 테이커(Ioc) 건수 — 어느 종목에서 순서 싸움이 났나
    paid_ioc_by_asset: Counter[int] = field(default_factory=Counter)
    # 주소별 행동 — 종류별 주문 건수·취소 수·낸 수수료 최대 p (LP형/테이커형 가르기용)
    by_user: dict[str, UserStats] = field(default_factory=dict)
    # IOC의 블록 안 상대 위치(0=맨 앞 … 1=맨 뒤) — 수수료 묶음(POS_GROUPS)별. 수수료가 실제로 앞줄을
    # 사 주는지 보기 위함(사용자 2026-11-18 "수수료 높은 주문이 먼저 체결됐다고 볼 수 있나")
    ioc_pos: dict[str, list[float]] = field(default_factory=dict)
    # 내 주문 tx(블록 안 위치) → 그 앞에 선 같은 종목 주문들(경쟁 상황 — 개장매매 2번, 2026-11-18)
    mine_ahead: dict[int, list[Competitor]] = field(default_factory=dict)


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
            s.mine.append((i, action, str(tx.get("hash", ""))))
            if kind == "order":
                s.mine_ahead[i] = competitors_ahead(txs, i)
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
            if tif == "Ioc":
                s.ioc_pos.setdefault(pos_group(p), []).append(i / max(s.n_tx - 1, 1))
    return s


def competitors_ahead(txs: list[dict[str, Any]], my_index: int) -> list[Competitor]:
    """블록 tx 목록에서 my_index(내 order tx) 앞에 선, 내 주문과 같은 종목의 주문들(순수).
    내 tx 안의 주문이 여러 종목이면 그중 하나라도 같으면 센다. 같은 방향(same_side)은 그 종목의
    내 주문 방향과 같을 때."""
    my_action = (txs[my_index].get("action") or {}) if 0 <= my_index < len(txs) else {}
    my_side: dict[int, bool] = {}
    for o in my_action.get("orders") or []:
        if isinstance(o.get("a"), int):
            my_side[int(o["a"])] = bool(o.get("b"))
    if not my_side:
        return []
    out: list[Competitor] = []
    for j in range(my_index):
        action = txs[j].get("action") or {}
        if action.get("type") != "order":
            continue
        p = priority_of(action)
        for o in action.get("orders") or []:
            a = o.get("a")
            if not isinstance(a, int) or a not in my_side:
                continue
            try:
                sz = float(o.get("s") or 0)
            except (TypeError, ValueError):
                sz = 0.0
            out.append(Competitor(j, a, "B" if o.get("b") else "A", tif_of(o), p, sz,
                                  bool(o.get("b")) == my_side[a]))
    return out


def format_ahead(ahead: list[Competitor]) -> str:
    """내 주문 줄 아래 한 줄 — 앞선 같은 종목 주문의 수·수수료·같은 방향 IOC."""
    if not ahead:
        return "앞선 같은 종목 주문 없음(블록 안 그 종목 첫 주문)"
    ioc = [c for c in ahead if c.tif == "Ioc"]
    same = [c for c in ioc if c.same_side]
    paid = [c for c in same if c.p is not None and c.p >= 100]
    fees = ", ".join(f"{bp_of(c.p):g}bp×{c.sz:g}" for c in paid if c.p is not None)
    return (f"앞선 같은 종목 주문 {len(ahead)}건(IOC {len(ioc)}, ALO/GTC {len(ahead) - len(ioc)})"
            f" — 같은 방향 IOC {len(same)}건, 그중 수수료 낸 것 {len(paid)}건"
            + (f" [{fees}]" if fees else ""))


def ioc_positions(summaries: list[BlockSummary]) -> dict[str, tuple[int, float]]:
    """묶음별 (IOC 건수, 블록 안 평균 상대 위치 0~1). 건수 0인 묶음은 뺀다."""
    out: dict[str, tuple[int, float]] = {}
    for g in POS_GROUPS:
        vals = [v for s in summaries for v in s.ioc_pos.get(g, ())]
        if vals:
            out[g] = (len(vals), sum(vals) / len(vals))
    return out


def format_positions(pos: dict[str, tuple[int, float]]) -> str:
    """한 줄: "IOC 블록 안 평균 위치(0%=맨 앞): ≥1bp 31%(41건) / <1bp 45%(66건) / 무료 52%(…)"."""
    parts = [f"{g} {pos[g][1] * 100:.0f}%({pos[g][0]:,}건)" for g in POS_GROUPS if g in pos]
    return "IOC 블록 안 평균 위치(0%=맨 앞): " + (" / ".join(parts) if parts else "IOC 없음")


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


@dataclass
class FillSummary:
    """주문(tx 해시) 하나의 체인 체결 요약 — info userFillsByTime 조각들을 합친 것."""

    oid: int
    coin: str
    side: str          # "B" 매수 / "A" 매도
    qty: float
    avg_px: float
    pieces: int
    first_ms: int
    last_ms: int
    crossed: bool      # True = 테이커로 가져감
    fee: float
    fee_token: str


def summarize_fills(fills: list[dict[str, Any]]) -> dict[str, FillSummary]:
    """체결 조각 목록 → tx 해시별 요약(수량 가중 평균가·조각 수·시각·수수료). 순수."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in fills:
        h = str(f.get("hash", ""))
        if h:
            groups.setdefault(h, []).append(f)
    out: dict[str, FillSummary] = {}
    for h, fs in groups.items():
        qty = sum(float(f.get("sz", 0)) for f in fs)
        if qty <= 0:
            continue
        avg = sum(float(f.get("sz", 0)) * float(f.get("px", 0)) for f in fs) / qty
        times = [int(f.get("time", 0)) for f in fs]
        out[h] = FillSummary(
            oid=int(fs[0].get("oid", 0)), coin=str(fs[0].get("coin", "")),
            side=str(fs[0].get("side", "")), qty=qty, avg_px=avg, pieces=len(fs),
            first_ms=min(times), last_ms=max(times), crossed=bool(fs[0].get("crossed", False)),
            fee=sum(float(f.get("fee", 0) or 0) for f in fs),
            fee_token=str(fs[0].get("feeToken", "")))
    return out


def format_fill(fs: FillSummary) -> str:
    """내 주문 줄 아래 한 줄 — 체결 수량·평균가·조각·시각·테이커 여부·수수료."""
    side = "매수" if fs.side == "B" else "매도"
    when = (kst(fs.first_ms) if fs.first_ms == fs.last_ms
            else f"{kst(fs.first_ms)}~{kst(fs.last_ms)}")
    return (f"체결 {side} {fs.qty:g} @평균 {fs.avg_px:,.3f} ({fs.pieces}조각, {when}, "
            f"{'테이커' if fs.crossed else '메이커'}) 수수료 {fs.fee:,.4f} {fs.fee_token} "
            f"#{fs.oid}")


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
    mine = f"  ← 내 tx 위치 {[i for i, _a, _h in s.mine]}" if s.mine else ""
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


def weekdays_between(start: date, end: date) -> list[date]:
    """start~end(포함)의 평일 목록 — 여러 날 조사용(사용자 2026-11-18 "하루씩 보기 힘들다"). end가
    start보다 앞이면 빈 목록. 공휴일은 모른다(그날은 블록은 있어도 우리 종목 주문이 없을 뿐)."""
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass
class DayRow:
    """하루 조사 결과 한 줄(IOC만) — 날짜별 표용."""
    day: str                 # "YYYY-MM-DD" 또는 실패 사유를 담을 때도 날짜
    blocks: int
    ioc: Counter[str] = field(default_factory=Counter)   # BUCKETS별 IOC 건수
    max_p: int | None = None                             # IOC 최대 p
    error: str | None = None                             # 그날 조사 실패 사유
    pos: dict[str, tuple[int, float]] = field(default_factory=dict)  # 묶음별 (건수, 평균 위치)


def day_row(day: str, n_blocks: int, by_tif: dict[str, Counter[str]],
            max_p: dict[str, int], pos: dict[str, tuple[int, float]] | None = None) -> DayRow:
    """하루 합계(merge_summaries 결과 + ioc_positions) → 표 한 줄. IOC가 없으면 빈 Counter."""
    return DayRow(day, n_blocks, Counter(by_tif.get("Ioc", Counter())), max_p.get("Ioc"),
                  pos=dict(pos or {}))


def _pad(text: str, width: int, right: bool = False) -> str:
    """한글은 두 칸 차지 — 표 칸 맞춤(모자란 만큼 공백; right면 오른쪽 정렬)."""
    wide = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))
    fill = " " * max(0, width - len(text) - wide)
    return fill + text if right else text + fill


def format_day_table(rows: list[DayRow]) -> list[str]:
    """날짜별 IOC 수수료 분포 표(머리 2줄 + 날짜마다 1줄 + 합계 1줄). 비율은 그날 IOC 건수 대비 %.
    내 주문 위치는 넣지 않는다(사용자 2026-11-18 "특정 시간대 수수료만 조사")."""
    head = (f"{_pad('날짜', 10)} {_pad('블록', 4, True)} {'IOC':>7s} {_pad('없음', 5, True)}"
            f" {'p≈0':>5s} {'<1bp':>5s} {'1~8bp':>5s} {'≥8bp':>5s} {_pad('최대bp', 6, True)}"
            f" | {_pad('위치≥1bp', 8, True)} {_pad('위치<1bp', 8, True)}"
            f" {_pad('위치무료', 8, True)}")
    width = len(head) + 6  # 한글 표시 너비 보정(위치 머리 3개 × 2)
    lines = ["위치 = 그 IOC들이 블록 안에서 선 자리의 평균(0% 맨 앞, 100% 맨 뒤). 앞줄일수록 작다.",
             head, "-" * width]
    total = Counter[str]()
    total_blocks = 0
    total_max: int | None = None
    total_pos: dict[str, list[tuple[int, float]]] = {}

    def pct(c: Counter[str], keys: tuple[str, ...]) -> str:
        tot = sum(c.values())
        return f"{sum(c[k] for k in keys) * 100 // tot:4d}%" if tot else "    -"

    def pos_text(pos: dict[str, tuple[int, float]], g: str) -> str:
        return f"{pos[g][1] * 100:7.0f}%" if g in pos else "       -"

    def row_text(label: str, blocks: int, c: Counter[str], mp: int | None,
                 pos: dict[str, tuple[int, float]]) -> str:
        mp_txt = f"{bp_of(mp):6.2f}" if mp is not None else "     -"
        return (f"{_pad(label, 10)} {blocks:4d} {sum(c.values()):7,} {pct(c, (BUCKETS[0],))}"
                f" {pct(c, (BUCKETS[1], BUCKETS[2]))} {pct(c, (BUCKETS[3],))}"
                f" {pct(c, (BUCKETS[4],))} {pct(c, (BUCKETS[5],))} {mp_txt}"
                f" | {pos_text(pos, POS_GROUPS[0])} {pos_text(pos, POS_GROUPS[1])}"
                f" {pos_text(pos, POS_GROUPS[2])}")

    for r in rows:
        if r.error:
            lines.append(f"{r.day:10s} 실패: {r.error}")
            continue
        lines.append(row_text(r.day, r.blocks, r.ioc, r.max_p, r.pos))
        total.update(r.ioc)
        total_blocks += r.blocks
        if r.max_p is not None and (total_max is None or r.max_p > total_max):
            total_max = r.max_p
        for g, (n, mean) in r.pos.items():
            total_pos.setdefault(g, []).append((n, mean))
    merged_pos = {g: (sum(n for n, _m in v), sum(n * m for n, m in v) / sum(n for n, _m in v))
                  for g, v in total_pos.items()}
    lines.append("-" * width)
    lines.append(row_text("합계", total_blocks, total, total_max, merged_pos))
    return lines
