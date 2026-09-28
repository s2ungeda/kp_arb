"""자동M(체결쏴) 정산 Sprd 리포트 — 판(선주문+후주문)이 끝날 때마다 Sprd와 그 인자를 시간순 CSV로.

    python -m kp_arb.report_sprd 20260928 logs   # logs/autom_*_20260928.log → sprd_20260928.csv
    python tools/round_sprd.py 20260928          # real_log/ 기본(운영 로그 복사본)

세 상품을 모두 읽는다(로그 파일 이름으로 구분):
    autom_<종목>_<날짜>.log              주식선물: 선주문 LS SF → 후주문 HL
    autom_<종목>_stock_<날짜>.log        주식:     선주문 LS 주식 → 후주문 HL
    autom_<종목>_sf_hl_first_<날짜>.log  HL선:     선주문 HL → 후주문 LS SF

한 판 = 같은 다리(진입|청산)의 선주문 체결 줄들 + 후주문 체결 줄들, 후주문 대기가 0이 되는 줄에서
끝난다. 중지(halt)로 끝난 미완 판은 **뽑지 않는다**(장부엔 짝 맞은 몫만 들어가지만 Sprd 인자가
한 판 값이 아님). 판 Sprd는 그 판의 인자만으로 다시 계산한다(exec §10·§7C·§7D, 결정 48):
    주식선물: (환 × HL평균 − S현재가)/S현재가 − (SF평균 − SF이론가)/SF이론가
    주식:     (환 × HL평균 − S체결평균)/S체결평균          (S = 그 판의 주식 평균 체결가)
    HL선:     주식선물과 같은 식(HL평균 = HL 선주문, SF평균 = LS 후주문)
'누적Sprd'는 로그에 찍힌 세트 누적값(장부 기준) — 판 값과 다른 게 정상(가중 평균).
"""
from __future__ import annotations

import csv
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TAG = (r"(?P<tag>(?:(?P<head>주식|HL선) )?(?P<dir>정방향|역방향) (?P<set>\d+)세트 "
        r"(?P<leg>진입|청산))")
_NUM = r"[-+]?[\d.,]+(?:[eE][-+]?\d+)?"
_RE_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ ")
# 선주문 체결 — LS(주식선물·주식): "선주문 #123 1 @ 270000", HL선: "HL 선주문 #123 1 @ 201.5"
_RE_PRE = re.compile(
    rf"체결 {_TAG}: (?P<hl>HL )?선주문 #\S+ (?P<qty>{_NUM}) @ (?P<px>{_NUM})")
# 후주문 체결 — HL(주식선물·주식): "후주문 #.. HL 8.046 @ 201.22 ... 환진입가 x S현재가 y
#   SF이론가 z → RT n HL대기 w ... Sprd s" / HL선: "LS 후주문 #.. SF 1 @ 275,000 ... LS대기 w"
_RE_POST = re.compile(
    rf"체결 {_TAG}: (?P<ls>LS )?후주문 #\S+ (?:HL|SF) (?P<qty>{_NUM}) @ (?P<px>{_NUM}) .*?"
    rf"환진입가 (?P<fx>\S+) S현재가 (?P<s>\S+) SF이론가 (?P<th>\S+) → RT (?P<rt>\d+) "
    rf"(?:HL|LS)대기 (?P<wait>{_NUM}) .*?Sprd (?P<sprd>\S+)")
_RE_HALT = re.compile(rf"행동 {_TAG}: halt\b")
_RE_FILE = re.compile(
    r"^autom_(?P<under>.+?)(?P<suffix>_stock|_sf_hl_first)?_(?P<day>\d{8})\.log$")

COLUMNS = ("완료시각", "종목", "상품", "방향", "세트", "다리", "RT", "선주문수량", "선주문평균가",
           "후주문수량", "후주문평균가", "환진입가", "S현재가", "SF이론가", "판Sprd(%)",
           "누적Sprd(%)", "파일")
_PRODUCT_NAME = {"stock": "주식", "sf_hl_first": "HL선"}


def _num(text: str | None) -> float | None:
    """'274,976' '1.783e+06' '1358.2' → float. None·'-'·'None' → None."""
    if text is None:
        return None
    t = text.strip().rstrip("%").replace(",", "")
    if t in ("", "-", "None"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


@dataclass
class _Round:
    pre_qty: float = 0.0
    pre_px: float = 0.0     # 수량 가중 합
    post_qty: float = 0.0
    post_px: float = 0.0
    fx: float = 0.0         # 후주문 수량 가중 합(값 있던 체결만)
    fx_qty: float = 0.0
    s: float = 0.0
    s_qty: float = 0.0
    th: float = 0.0
    th_qty: float = 0.0
    lines: list[str] = field(default_factory=list)

    def avg(self, total: float, qty: float) -> float | None:
        return total / qty if qty > 0 else None


def round_sprd(product: str, pre_avg: float | None, post_avg: float | None,
               fx: float | None, s_last: float | None, theory: float | None) -> float | None:
    """한 판의 정산 Sprd(exec §10·§7C·§7D, 결정 48). 인자가 모자라면 None. 순수."""
    if product == "stock":
        if None in (fx, post_avg, pre_avg) or not pre_avg:
            return None
        assert fx is not None and post_avg is not None and pre_avg is not None
        return (fx * post_avg - pre_avg) / pre_avg
    if product == "sf_hl_first":
        hl, sf = pre_avg, post_avg
    else:
        hl, sf = post_avg, pre_avg
    if None in (fx, hl, sf, s_last, theory) or not s_last or not theory:
        return None
    assert fx is not None and hl is not None and sf is not None
    assert s_last is not None and theory is not None
    return (fx * hl - s_last) / s_last - (sf - theory) / theory


def parse_rounds(lines: Iterable[str], *, underlying: str, product: str,
                 source: str = "") -> list[dict[str, Any]]:
    """자동M 로그 줄들 → 끝난 판 목록(시간순). 순수.

    다리(방향·세트·진입|청산)마다 진행 중인 판을 하나씩 들고, 선주문 체결은 선주문 쪽에, 후주문
    체결은 후주문 쪽에 더한다. 후주문 대기가 0이 되는 줄에서 판을 확정한다. halt가 오면 그 다리의
    진행 중인 판은 버린다(미완).
    """
    open_rounds: dict[str, _Round] = {}
    out: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.rstrip("\n")
        m_ts = _RE_TS.match(line)
        if m_ts is None:
            continue
        m = _RE_PRE.search(line)
        if m is not None:
            r = open_rounds.setdefault(m.group("tag"), _Round())
            qty, px = _num(m.group("qty")), _num(m.group("px"))
            if qty is not None and px is not None:
                r.pre_qty += qty
                r.pre_px += qty * px
            continue
        m = _RE_POST.search(line)
        if m is not None:
            r = open_rounds.setdefault(m.group("tag"), _Round())
            qty, px = _num(m.group("qty")), _num(m.group("px"))
            if qty is not None and px is not None:
                r.post_qty += qty
                r.post_px += qty * px
                for key, tot, cnt in (("fx", "fx", "fx_qty"), ("s", "s", "s_qty"),
                                      ("th", "th", "th_qty")):
                    v = _num(m.group(key))
                    if v is not None:
                        setattr(r, tot, getattr(r, tot) + v * qty)
                        setattr(r, cnt, getattr(r, cnt) + qty)
            wait = _num(m.group("wait"))
            if wait is not None and wait <= 1e-9:  # 후주문까지 완료 → 판 확정
                pre_avg = r.avg(r.pre_px, r.pre_qty)
                post_avg = r.avg(r.post_px, r.post_qty)
                fx, s_last, th = (r.avg(r.fx, r.fx_qty), r.avg(r.s, r.s_qty),
                                  r.avg(r.th, r.th_qty))
                out.append({
                    "완료시각": m_ts.group("ts"), "종목": underlying,
                    "상품": _PRODUCT_NAME.get(product, "주식선물"),
                    "방향": m.group("dir"), "세트": int(m.group("set")), "다리": m.group("leg"),
                    "RT": int(m.group("rt")),
                    "선주문수량": r.pre_qty, "선주문평균가": pre_avg,
                    "후주문수량": r.post_qty, "후주문평균가": post_avg,
                    "환진입가": fx, "S현재가": s_last, "SF이론가": th,
                    "판Sprd(%)": _pct(round_sprd(product, pre_avg, post_avg, fx, s_last, th)),
                    "누적Sprd(%)": _cum_pct(m.group("sprd")),
                    "파일": source,
                })
                open_rounds.pop(m.group("tag"), None)
            continue
        m = _RE_HALT.search(line)
        if m is not None:
            open_rounds.pop(m.group("tag"), None)  # 중지로 끝난 미완 판은 버림
    return out


def _pct(v: float | None) -> float | None:
    return round(v * 100, 4) if v is not None else None


def _cum_pct(text: str) -> float | None:
    """로그의 누적 Sprd 표기('1.806%' | '-(판 미완)') → 퍼센트 값."""
    return _num(text) if text.endswith("%") else None


def collect(log_dir: Path, day: str) -> list[dict[str, Any]]:
    """log_dir의 그 날짜 자동M 로그 전부 → 끝난 판, 완료시각순."""
    rows: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob(f"autom_*_{day}.log")):
        m = _RE_FILE.match(path.name)
        if m is None:
            continue
        suffix = m.group("suffix") or ""
        product = {"_stock": "stock", "_sf_hl_first": "sf_hl_first"}.get(suffix, "sf")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows.extend(parse_rounds(text.splitlines(), underlying=m.group("under"),
                                 product=product, source=path.name))
    rows.sort(key=lambda r: str(r["완료시각"]))
    return rows


def write_csv(rows: list[dict[str, Any]], out: Path) -> None:
    """엑셀에서 바로 열리게 utf-8-sig."""
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS})


def default_log_dir() -> Path:
    from .report_latency import default_log_dir as _d

    return _d()


def main(argv: list[str]) -> int:
    from datetime import datetime

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    day = argv[1] if len(argv) > 1 and argv[1].isdigit() else datetime.now().strftime("%Y%m%d")
    log_dir = Path(argv[2]) if len(argv) > 2 else default_log_dir()
    rows = collect(log_dir, day)
    if not rows:
        print(f"{log_dir / f'autom_*_{day}.log'} 에 후주문까지 끝난 판이 없음")
        return 1
    out = log_dir / f"sprd_{day}.csv"
    write_csv(rows, out)
    for r in rows:
        print(f"{r['완료시각']} {r['종목']} {r['상품']} {r['방향']} {r['세트']}세트 {r['다리']} "
              f"RT{r['RT']} 선 {r['선주문수량']:g}@{r['선주문평균가']} "
              f"후 {r['후주문수량']:g}@{r['후주문평균가']} 환 {r['환진입가']} "
              f"S {r['S현재가']} 이론 {r['SF이론가']} 판Sprd {r['판Sprd(%)']}% "
              f"누적 {r['누적Sprd(%)']}%")
    print(f"\n{len(rows)}판 저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
