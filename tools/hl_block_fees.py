"""HL 블록 우선순위 수수료 조회 도구 — 공개 탐색기(rpc.hyperliquid.xyz) 읽기 전용, 주문 없음.

주어진 시각부터 N초 동안의 블록을 찾아, 블록마다 주문 수·종류(Ioc/Alo/Gtc)·우선순위 수수료(p) 분포와
내 주문의 블록 안 위치를 표로 찍는다(exec 결정 91 뒤 2026-11-09 사용자 "내가 직접 볼 수 있는 방법").
p ÷ 10,000 = bp. 순수 로직은 kp_arb/hl_block_fees.py.

사용: python tools\\hl_block_fees.py <YYYY-MM-DD> <HH:MM:SS> [초=2] [--me 0x주소]
  예: … 2026-11-04 08:00:00 2
--me 를 안 주면 키링의 HL_ACCOUNT_ADDRESS(운영 계정 주소)를 쓴다. 시각은 이 PC 시간대(KST).
--ours 는 우리 종목(xyz:SMSN·SKHX·HYUNDAI)만, --coin xyz:SKHX,xyz:SMSN 은 고른 종목만 센다
(기본 = HL 전체). 자산 번호(주문의 a)는 HL 메타(info meta)로 이름을 붙인다 — 예 110022 = xyz:SKHX.
--who 는 주소별 행동(메이커형(LP)/테이커형, 종류별 건수, 취소, 낸 수수료)을 활동 많은 순으로.
  주소가 누구인지는 체인에 없다 — 행동으로만 가른다. 종목 필터와 같이 쓰면 취소는 주소별로 안 센다.
결과는 화면과 logs\\probe\\hl_block_fees_<날짜>_<시각>.txt 둘 다에 남는다.
(tools\\hl_block_fees.bat 로도 실행 — 날짜·시각을 물어본다.)
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kp_arb.hl_block_fees import (  # noqa: E402
    BlockSummary,
    asset_id,
    find_block_at,
    format_block_line,
    format_summary,
    format_users,
    kst,
    merge_summaries,
    merge_users,
    summarize_block,
    top_paid_assets,
)

INFO_URL = "https://api.hyperliquid.xyz/info"
OUR_COINS = ("xyz:SMSN", "xyz:SKHX", "xyz:HYUNDAI")

EXPLORER_URL = "https://rpc.hyperliquid.xyz/explorer"
# 기준점(높이, blockTime ms) — 2026-11-04 08:00:01.216 KST 블록(실측). 평균 간격 70.7ms로 첫 추정.
REF_HEIGHT, REF_TIME_MS = 1208130639, 1793746801216
MS_PER_BLOCK = 70.7


REQUEST_GAP_S = 0.25   # 조회 사이 쉼 — 공개 탐색기 한도(429) 예방
RETRY_WAITS_S = (3, 6, 12, 24, 48)  # 429(Too Many Requests)면 이만큼 쉬고 다시


def rpc(payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(EXPLORER_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt, wait in enumerate((*RETRY_WAITS_S, None)):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data: dict[str, Any] = json.load(r)
            time.sleep(REQUEST_GAP_S)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or wait is None:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            pause = float(retry_after) if retry_after and retry_after.isdigit() else float(wait)
            print(f"  탐색기 요청 한도(429) — {pause:g}초 쉬고 다시 "
                  f"({attempt + 1}/{len(RETRY_WAITS_S)})", flush=True)
            time.sleep(pause)
    raise RuntimeError("unreachable")


def block_details(height: int) -> dict[str, Any]:
    d = rpc({"type": "blockDetails", "height": height})
    inner = d.get("blockDetails")
    return inner if isinstance(inner, dict) else d


def _info(payload: dict[str, Any]) -> Any:
    req = urllib.request.Request(INFO_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def asset_names() -> dict[int, str]:
    """자산 번호 → 이름. 본 dex(``meta``) + 빌더 dex 목록(``perpDexs``, 첫 칸은 본 dex=None)마다
    ``meta``. 실패하면 비어 있는 채(번호로만 본다)."""
    names: dict[int, str] = {}
    try:
        dexs = _info({"type": "perpDexs"})
    except Exception:  # noqa: BLE001
        dexs = [None]
    if not isinstance(dexs, list):
        dexs = [None]
    for di, dex in enumerate(dexs):
        payload: dict[str, Any] = {"type": "meta"}
        if isinstance(dex, dict) and dex.get("name"):
            payload["dex"] = dex["name"]
        elif di > 0:
            continue
        try:
            meta = _info(payload)
        except Exception:  # noqa: BLE001
            continue
        for idx, u in enumerate(meta.get("universe") or []):
            names[asset_id(di, idx)] = str(u.get("name", ""))
    return names


def default_me() -> str | None:
    try:
        from kp_arb.config import default_secrets

        return default_secrets().get("HL_ACCOUNT_ADDRESS") or None
    except Exception:  # noqa: BLE001 - 키링 없으면 내 주문 표시만 생략
        return None


def run(start: datetime, seconds: float, me: str | None, out: TextIO,
        coins: tuple[str, ...] = (), who: bool = False) -> None:
    def say(line: str = "") -> None:
        print(line, flush=True)
        out.write(line + "\n")
        out.flush()

    t0 = int(start.timestamp() * 1000)
    t1 = t0 + int(seconds * 1000)
    say(f"대상 {start:%Y-%m-%d %H:%M:%S} ~ +{seconds:g}초 (이 PC 시간대) / 내 주소 {me or '없음'}")
    names = asset_names()
    assets: set[int] | None = None
    if coins:
        assets = {a for a, n in names.items() if n in coins}
        picked = ", ".join(f"{a}={n}" for a, n in sorted(names.items()) if n in coins)
        say(f"종목 필터: {picked or '(메타에서 못 찾음 — 전체로 셈)'}"
            " (취소는 종목을 알 수 없어 전부 셈)")
        if not assets:
            assets = None
    else:
        say("종목 필터 없음 — HL 전체 종목 합계 (--ours 또는 --coin 으로 좁힐 수 있음)")
    say("탐색기(rpc.hyperliquid.xyz)에 블록 시각을 물어 첫 블록을 찾는 중... (조회 한 번에 1~3초)")
    calls = 0

    def get_time(h: int) -> int:
        nonlocal calls
        calls += 1
        t = int(block_details(h)["blockTime"])
        print(f"  탐색 {calls}회: 블록 {h} = {kst(t)}", flush=True)
        return t

    h = find_block_at(t0, get_time, REF_HEIGHT, REF_TIME_MS, MS_PER_BLOCK)
    say(f"첫 블록 {h} (탐색 {calls}회)")
    summaries: list[BlockSummary] = []
    while True:
        s = summarize_block(h, block_details(h), me, assets)
        summaries.append(s)
        say(format_block_line(s))
        if s.time_ms >= t1 or len(summaries) >= 200:
            break
        h += 1
    by_tif, max_p = merge_summaries(summaries)
    say()
    say(f"== 합계: 블록 {len(summaries)}개 {kst(summaries[0].time_ms)}~{kst(summaries[-1].time_ms)}"
        f" (p ÷ 10,000 = bp; IOC는 8bp에서 지연 단축 상한) ==")
    for line in format_summary(by_tif, max_p):
        say("  " + line)
    tops = top_paid_assets(summaries, names)
    if tops:
        say(f"  수수료 낸 Ioc가 많은 종목: {', '.join(tops)}")
    if who:
        users = merge_users(summaries)
        say()
        say(f"== 주소별 행동 (주소 {len(users):,}개 중 활동 많은 순; 메이커형 = ALO 80%↑, "
            f"테이커형 = IOC 80%↑) ==")
        for line in format_users(users, me):
            say("  " + line)
    mine = [(s, i, a) for s in summaries for i, a in s.mine]
    if mine:
        say()
        say("== 내 주문 ==")
        for s, i, a in mine:
            orders = a.get("orders") or []
            desc = ", ".join(
                f"{names.get(o.get('a'), o.get('a'))} {'매수' if o.get('b') else '매도'} "
                f"{o.get('s')} @ {o.get('p')} "
                f"{(o.get('t') or {}).get('limit', {}).get('tif', '?')}" for o in orders)
            say(f"  블록 {s.height} {kst(s.time_ms)} 위치 {i}/{s.n_tx} grouping={a.get('grouping')}"
                f" {desc}")
    elif me:
        say()
        say("(이 구간에 내 주문 없음)")


def main(argv: list[str]) -> int:
    me: str | None = None
    coins: tuple[str, ...] = ()
    args = list(argv)
    if "--me" in args:
        i = args.index("--me")
        me = args[i + 1]
        del args[i:i + 2]
    if "--coin" in args:
        i = args.index("--coin")
        coins = tuple(c.strip() for c in args[i + 1].split(",") if c.strip())
        del args[i:i + 2]
    if "--ours" in args:
        coins = OUR_COINS
        args.remove("--ours")
    who = "--who" in args
    if who:
        args.remove("--who")
    if len(args) < 2:
        print(__doc__)
        return 2
    try:
        start = datetime.strptime(f"{args[0]} {args[1]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        print(f"날짜·시각 형식이 틀립니다: {args[0]!r} {args[1]!r} — 예: 2026-11-04 08:00:00")
        return 2
    seconds = float(args[2]) if len(args) > 2 and args[2] else 2.0
    me = me or default_me()
    out_dir = ROOT / "logs" / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"hl_block_fees_{start:%Y%m%d_%H%M%S}.txt"
    print(f"결과 파일: {out_path}", flush=True)
    with open(out_path, "w", encoding="utf-8") as out:
        try:
            run(start, seconds, me, out, coins, who)
        except Exception as exc:  # noqa: BLE001 - 화면에 원인을 보여 주고 끝낸다
            msg = f"실패: {type(exc).__name__}: {exc}"
            print(msg, flush=True)
            out.write(msg + "\n")
            return 1
    print(f"저장: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
