"""HL 후주문 체결 즉시성 리포트 — 우선수수료를 얼마나 낼지 정하는 1번 근거(사용자 2026-11-18).

로그 파일만 읽는다(라이브 접속 없음). 전략이 낸 HL 주문(기본 [자동M]) 하나하나가 **발주 응답에서
바로 다 체결됐는지**를 센다. 수수료는 "안 냈더니 손해 본 일"이 있어야 낼 값어치가 있으므로, 즉시
전량 비율이 높으면 수수료가 사 줄 것이 거의 없고, 걸림·부분·미체결이 있으면 그 건들만 따로 본다.

    개발 PC:  python -m kp_arb.report_hl_fill 20261106 real_log/core_20261106
              python -m kp_arb.report_hl_fill 20261103..20261118 real_log   (core_<날짜>/ 폴더들)
              python -m kp_arb.report_hl_fill all real_log                   (real_log/core_*/ 전부)
    출처 [자동M] 외에 보려면 끝에 --source NXT개장 (기본 자동M).

줄의 출처(hl_order_<날짜>.log)
  발주요청 [출처] 종목 hl_perp 방향 수량 @ 가격 [ioc p=N]  → 요청(IOC·p 표시는 우선수수료 빌드~)
  발주 … → #oid | resp={… 'statuses': [{'filled': {'totalSz','avgPx'}} | {'resting': …}
                                        | {'error': …}]}
  HL 발주 왕복 N ms #oid cloid=…  /  HL 묶음 발주 왕복 N ms  (묶음은 요청패킷 cloid로 짝)
  발주거부 …                                               → 거부
  체결(전량|부분) [출처] … #oid …                            → 걸린 주문의 뒤늦은 체결(시각)
  주문종료(canceled) #oid                                   → 걸린 주문이 취소로 끝남
  (core_<날짜>.log) IOC 남은 수량 Q 장부 닫음 #oid(체결 F)   → IOC 미체결 잔량
결과는 화면과 `<로그폴더>/report_hl_fill_<날짜>.md`(범위면 `_<첫날>-<끝날>.md`) 둘 다.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_REQ = re.compile(r"INFO 발주요청 \[(?P<src>[^\]]+)\] (?P<name>\S+) hl_perp (?P<side>buy|sell) "
                  r"(?P<qty>[\d.]+) @ (?P<px>[\d.]+)(?P<ioc> ioc)?(?: p=(?P<p>\d+))?")
_PLACED = re.compile(r"INFO 발주 \[(?P<src>[^\]]+)\] (?P<name>\S+) hl_perp (?P<side>buy|sell) "
                     r"(?P<qty>[\d.]+) @ (?P<px>[\d.]+).*? → #(?P<oid>\d+) \| resp=(?P<resp>.*)$")
_REJECT = re.compile(r"WARNING 발주거부 \[(?P<src>[^\]]+)\] (?P<name>\S+) hl_perp "
                     r"(?P<side>buy|sell) (?P<qty>[\d.]+) @ (?P<px>[\d.]+)")
_FILL = re.compile(r"INFO 체결\((?P<kind>전량|부분)\) \[(?P<src>[^\]]+)\] .*? #(?P<oid>\d+) ")
_CLOSED = re.compile(r"INFO 주문종료\((?P<how>\w+)\) #(?P<oid>\d+)")
_RTT = re.compile(r"HL 발주 왕복 (?P<ms>\d+) ms #(?P<oid>\d+)")
_RTT_BULK = re.compile(r"HL 묶음 발주 왕복 (?P<ms>\d+) ms")
_IOC_LEFT = re.compile(r"IOC 남은 수량 (?P<left>[\d.]+) 장부 닫음 #(?P<oid>\d+)"
                       r"\(체결 (?P<filled>[\d.]+)\)")
_FILLED_RESP = re.compile(r"'filled': \{'totalSz': '(?P<sz>[\d.]+)', 'avgPx': '(?P<px>[\d.]+)'")

OUTCOMES = ("즉시전량", "즉시부분", "걸림", "거부", "응답없음")


@dataclass
class HlOrder:
    """전략 HL 주문 한 건의 결과."""

    t_req: datetime
    name: str
    side: str
    qty: float
    px: float
    ioc: bool = False
    p: int | None = None
    oid: str | None = None
    outcome: str = "응답없음"      # OUTCOMES
    filled_now: float = 0.0        # 응답에서 바로 체결된 수량
    avg_px: float | None = None    # 그 평균가
    rtt_ms: int | None = None
    t_done: datetime | None = None  # 걸린 주문이 체결(전량)·취소로 끝난 시각
    done_how: str | None = None     # "체결" | "취소"
    ioc_left: float | None = None   # IOC 남은 수량(core 로그)
    error: str | None = None


@dataclass
class DayFill:
    day: str
    orders: list[HlOrder] = field(default_factory=list)

    @property
    def counts(self) -> Counter[str]:
        return Counter(o.outcome for o in self.orders)


def _ts(line: str) -> datetime:
    return datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")


def _f(text: str) -> float:
    return float(text)


def parse_day(hl_lines: Sequence[str], core_lines: Sequence[str] = (), day: str = "",
              source: str = "자동M") -> DayFill:
    """hl_order 로그 줄들(+ core 로그 줄들, IOC 잔량용) → 그날 전략 HL 주문 결과. 순수.

    요청과 응답은 같은 (종목, 방향, 수량, 가격)의 **아직 응답 없는 가장 오래된 요청**으로 짝짓는다
    (묶음 발주는 요청 줄이 먼저 여러 개 찍히고 응답 줄이 뒤따른다). 응답 없이 끝난 요청은
    "응답없음".
    """
    out = DayFill(day)
    pending: dict[tuple[str, str, float, float], list[HlOrder]] = {}
    by_oid: dict[str, HlOrder] = {}
    last_bulk_rtt: int | None = None
    for line in hl_lines:
        m = _REQ.search(line)
        if m and m.group("src") == source:
            o = HlOrder(_ts(line), m.group("name"), m.group("side"), _f(m.group("qty")),
                        _f(m.group("px")), bool(m.group("ioc")),
                        int(m.group("p")) if m.group("p") else None)
            out.orders.append(o)
            pending.setdefault((o.name, o.side, o.qty, o.px), []).append(o)
            continue
        m = _RTT_BULK.search(line)
        if m:
            last_bulk_rtt = int(m.group("ms"))
            continue
        m = _PLACED.search(line)
        if m and m.group("src") == source:
            key = (m.group("name"), m.group("side"), _f(m.group("qty")), _f(m.group("px")))
            q = pending.get(key)
            if not q:
                continue
            o = q.pop(0)
            o.oid = m.group("oid")
            by_oid[o.oid] = o
            resp = m.group("resp")
            fm = _FILLED_RESP.search(resp)
            if fm:
                o.filled_now, o.avg_px = _f(fm.group("sz")), _f(fm.group("px"))
                o.outcome = "즉시전량" if o.filled_now >= o.qty - 1e-9 else "즉시부분"
            elif "'resting'" in resp:
                o.outcome = "걸림"
            elif "'error'" in resp:
                o.outcome, o.error = "거부", resp[:120]
            else:
                o.outcome = "걸림"  # 응답 모양을 모르면 보수적으로 걸림
            if last_bulk_rtt is not None:
                o.rtt_ms = last_bulk_rtt
            continue
        m = _REJECT.search(line)
        if m and m.group("src") == source:
            key = (m.group("name"), m.group("side"), _f(m.group("qty")), _f(m.group("px")))
            q = pending.get(key)
            if q:
                o = q.pop(0)
                o.outcome = "거부"
                o.error = line.split(" — ", 1)[1].strip()[:120] if " — " in line else None
            continue
        m = _RTT.search(line)
        if m and m.group("oid") in by_oid:
            by_oid[m.group("oid")].rtt_ms = int(m.group("ms"))
            continue
        m = _FILL.search(line)
        if m and m.group("src") == source and m.group("oid") in by_oid:
            o = by_oid[m.group("oid")]
            if o.outcome in ("걸림", "즉시부분") and m.group("kind") == "전량" and o.t_done is None:
                o.t_done, o.done_how = _ts(line), "체결"
            continue
        m = _CLOSED.search(line)
        if m and m.group("oid") in by_oid:
            o = by_oid[m.group("oid")]
            if o.t_done is None and m.group("how") == "canceled":
                o.t_done, o.done_how = _ts(line), "취소"
    for line in core_lines:
        m = _IOC_LEFT.search(line)
        if m and m.group("oid") in by_oid:
            by_oid[m.group("oid")].ioc_left = _f(m.group("left"))
    return out


def _pct(n: int, tot: int) -> str:
    return f"{n * 100 // tot:3d}%" if tot else "  -"


def _median(vals: list[int]) -> str:
    if not vals:
        return "-"
    s = sorted(vals)
    return str(s[len(s) // 2])


def render(days: list[DayFill], source: str = "자동M") -> str:
    """날짜별 표 + 즉시 전량이 아니었던 건의 목록. 순수."""
    lines = [f"# HL [{source}] 주문 체결 즉시성 — 발주 응답에서 바로 다 체결됐나(수수료 판단 근거)",
             "",
             "날짜       건수 즉시전량 즉시부분  걸림  거부 응답없음  IOC건 IOC잔량건  왕복중앙ms",
             "-" * 86]
    total: Counter[str] = Counter()
    n_total = 0
    rtts_all: list[int] = []
    for d in days:
        c = d.counts
        n = len(d.orders)
        n_total += n
        total.update(c)
        rtts = [o.rtt_ms for o in d.orders if o.rtt_ms is not None]
        rtts_all += rtts
        n_ioc = sum(1 for o in d.orders if o.ioc)
        n_left = sum(1 for o in d.orders if o.ioc_left)
        lines.append(f"{d.day:10s} {n:4d} {_pct(c['즉시전량'], n):>8s}"
                     f" {_pct(c['즉시부분'], n):>8s} {_pct(c['걸림'], n):>5s}"
                     f" {_pct(c['거부'], n):>5s} {_pct(c['응답없음'], n):>8s}"
                     f" {n_ioc:6d} {n_left:9d} {_median(rtts):>11s}")
    lines.append("-" * 86)
    lines.append(f"{'합계':10s} {n_total:4d} {_pct(total['즉시전량'], n_total):>8s}"
                 f" {_pct(total['즉시부분'], n_total):>8s} {_pct(total['걸림'], n_total):>5s}"
                 f" {_pct(total['거부'], n_total):>5s} {_pct(total['응답없음'], n_total):>8s}"
                 f" {sum(1 for d in days for o in d.orders if o.ioc):6d}"
                 f" {sum(1 for d in days for o in d.orders if o.ioc_left):9d}"
                 f" {_median(rtts_all):>11s}")
    odd = [(d.day, o) for d in days for o in d.orders if o.outcome != "즉시전량"]
    lines += ["", f"## 즉시 전량이 아니었던 건 {len(odd)}건"]
    if not odd:
        lines.append("(없음 — 수수료 없이도 전부 응답에서 바로 다 체결)")
    for day, o in odd:
        done = ""
        if o.t_done is not None:
            done = (f" → {o.done_how} {o.t_done:%H:%M:%S.%f}"[:-3]
                    + f" (+{(o.t_done - o.t_req).total_seconds():.1f}s)")
        extra = f" 즉시 {o.filled_now:g}/{o.qty:g}" if o.outcome == "즉시부분" else ""
        ioc = (" ioc" + (f" p={o.p}" if o.p else "")) if o.ioc else ""
        left = f" IOC잔량 {o.ioc_left:g}" if o.ioc_left else ""
        err = f" — {o.error}" if o.error else ""
        when = f"{o.t_req:%H:%M:%S.%f}"[:-3]
        lines.append(f"- {day} {when} {o.name} {o.side} {o.qty:g} @ {o.px:g}"
                     f"{ioc} #{o.oid or '-'}: {o.outcome}{extra}{done}{left}{err}")
    return "\n".join(lines)


def _read(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def load_day(log_dir: Path, day: str, source: str) -> DayFill | None:
    """`log_dir`가 그날 폴더(hl_order_<day>.log가 바로 있음)거나 상위(core_<day>/ 안)인 경우
    둘 다."""
    for base in (log_dir, log_dir / f"core_{day}"):
        hl = base / f"hl_order_{day}.log"
        if hl.exists():
            return parse_day(_read(hl), _read(base / f"core_{day}.log"), day, source)
    return None


def expand_days(arg: str, log_dir: Path) -> list[str]:
    """'YYYYMMDD' | 'A..B'(그 사이 폴더가 있는 날만) | 'all'(log_dir/core_*/ 전부)."""
    if arg == "all":
        return sorted(p.name[len("core_"):] for p in log_dir.glob("core_*") if p.is_dir())
    if ".." in arg:
        a, b = arg.split("..", 1)
        return [d for d in expand_days("all", log_dir) if a <= d <= b]
    return [arg]


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = [a for a in argv[1:]]
    source = "자동M"
    if "--source" in args:
        i = args.index("--source")
        source = args[i + 1]
        del args[i:i + 2]
    day_arg = args[0] if args else datetime.now().strftime("%Y%m%d")
    log_dir = Path(args[1]) if len(args) > 1 else Path("logs")
    days = [d for d in (load_day(log_dir, day, source) for day in expand_days(day_arg, log_dir))
            if d is not None and d.orders]
    if not days:
        print(f"{log_dir} 에 {day_arg} [{source}] HL 주문 기록이 없음")
        return 1
    text = render(days, source)
    print(text)
    tag = days[0].day if len(days) == 1 else f"{days[0].day}-{days[-1].day}"
    out = log_dir / f"report_hl_fill_{tag}.md"
    try:
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {out}")
    except OSError as exc:
        print(f"\n저장 실패: {out} — {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
