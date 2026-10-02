"""10-02 측정 분석 — ① 누적거래량으로 체결 TR이 호가 TR보다 앞선 체결인지 가리기(주식)
② 1호가 잔량 − 체결량으로 '다 먹혔나' 판단 → 체결 뒤 1호가 추정 정확도. 본장(09:00~) 연속 매매만."""
import json, sys, statistics as st
from collections import Counter, defaultdict
from kp_arb.domain.enums import Instrument
from kp_arb.ticks import tick_for

Q = {"H1_", "JH0"}
NAMES = {"005930": ("삼성 주식", Instrument.KR_STOCK), "A116A000": ("삼성 주식선물", Instrument.KR_STOCK_FUTURE),
         "000660": ("하이닉스 주식", Instrument.KR_STOCK), "A506A000": ("하이닉스 주식선물", Instrument.KR_STOCK_FUTURE)}
by = defaultdict(list)
for line in open(sys.argv[1], encoding="utf-8"):
    r = json.loads(line)
    t = r.get("hotime") or r.get("chetime") or ""
    if not ("090000" <= t < "152000"):
        continue
    r["code"] = (r.get("shcode") or r.get("futcode") or "").strip()
    r["t"] = t
    r["bid"] = int(r.get("bidho1") or r.get("bidho") or 0)
    r["ask"] = int(r.get("offerho1") or r.get("offerho") or 0)
    r["vol"] = int(r.get("volume") or 0)
    by[r["code"]].append(r)


def dist(v):
    if not v: return "-"
    s = sorted(v); return f"중앙 {st.median(s):.0f} 90% {s[int(len(s)*.9)-1]:.0f} 최대 {s[-1]:.0f}"


for code, (name, inst) in NAMES.items():
    seq = by[code]
    quotes = [r for r in seq if r["tr"] in Q]; trades = [r for r in seq if r["tr"] not in Q]
    print(f"\n=== {name}: 호가 {len(quotes):,} 체결 {len(trades):,}")
    # ① 순서(주식만: 호가 TR volume vs 체결 TR volume)
    if inst is Instrument.KR_STOCK:
        c = Counter(); q0 = None
        for r in seq:
            if r["tr"] in Q: q0 = r; continue
            if q0 is None: continue
            if r["vol"] <= q0["vol"]: c["호가 TR에 이미 반영된 체결(누적 ≤ 호가 누적)"] += 1
            else: c["호가 TR 뒤의 새 체결(누적 > 호가 누적)"] += 1
        for k, v in c.items(): print(f"  ① {k}: {v:,}")
        # 호가 TR의 누적거래량이 직전 체결 TR 누적과 같은가(호가 TR이 체결 뒤 상태?)
        last_t = None; c2 = Counter()
        for r in seq:
            if r["tr"] not in Q: last_t = r; continue
            if last_t is None: continue
            c2["호가 누적 == 직전 체결 누적" if r["vol"] == last_t["vol"] else ("호가 누적 > 직전 체결 누적" if r["vol"] > last_t["vol"] else "호가 누적 < 직전 체결 누적")] += 1
        for k, v in c2.items(): print(f"  ① {k}: {v:,}")
    # ② 구간별: 직전 호가 q0, 그 뒤 체결들, 다음 호가 q1. 새 체결(주식: vol>q0.vol / 선물: 전부)만으로 잔량 차감
    c = Counter(); lead = []
    q0 = None; bucket = []
    for r in seq:
        if r["tr"] not in Q:
            if r["bid"] and r["ask"] and r["cgubun"] in "+-": bucket.append(r)
            continue
        if q0 is not None and bucket:
            new = [t for t in bucket if inst is not Instrument.KR_STOCK or t["vol"] > q0["vol"]]
            a = (q0["bid"], q0["ask"]); b = (r["bid"], r["ask"]); changed = a != b
            c["구간(체결 있음)"] += 1; c["  그중 1호가 바뀜"] += changed
            if not new:
                c["  새 체결 없음(전부 반영됨)"] += 1
                c["    그런데 1호가 바뀜(체결 없이 바뀐 것)"] += changed
                q0 = r; bucket = []; continue
            # 잔량 차감 추정
            bid, ask = a; brem, arem = int(q0["bidrem1"]), int(q0["offerrem1"])
            for t in new:
                p, g, cv = int(t["price"]), t["cgubun"], int(t["cvolume"])
                tk = tick_for(inst, p)
                if g == "+":
                    if p == ask:
                        arem -= cv
                        if arem <= 0: ask += tk; arem = 0; bid = max(bid, ask - tk) if False else bid
                    elif p > ask: ask = p; arem = 0
                else:
                    if p == bid:
                        brem -= cv
                        if brem <= 0: bid -= tk; brem = 0
                    elif p < bid: bid = p; brem = 0
            est = (bid, ask)
            if changed:
                if est == b: c["  바뀐 구간: 추정이 다음 호가와 같음(맞힘)"] += 1; lead.append(r["ms"] - new[-1]["ms"])
                elif est == a: c["  바뀐 구간: 추정 못 따라감(직전 그대로)"] += 1
                else:
                    # 한쪽만 맞았나
                    c["  바뀐 구간: 한쪽만 맞음" if (est[0] == b[0] or est[1] == b[1]) else "  바뀐 구간: 엉뚱한 값"] += 1
            else:
                c["  안 바뀐 구간: 추정이 틀린 값" if est != a else "  안 바뀐 구간: 추정 그대로(맞음)"] += 1
        q0 = r; bucket = []
    for k in c: print(f"  ② {k}: {c[k]:,}")
    print(f"  ② 맞힌 경우 앞선 시간(ms): {dist(lead)}")
