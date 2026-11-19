"""HL 블록 우선순위 수수료 조회 — 공개 탐색기(rpc.hyperliquid.xyz) **읽기 전용**, 주문 없음.

주어진 시각부터 N초 동안의 블록을 찾아, 블록마다 주문 수·종류(Ioc/Alo/Gtc)·우선순위 수수료(p) 분포와
내 주문의 블록 안 위치를 표로 찍는다(2026-11-09 사용자 "내가 직접 볼 수 있는 방법").
p ÷ 10,000 = bp. 순수 로직은 hl_block_fees.py, 여기는 네트워크·파일 I/O.
개발은 tools/hl_block_fees.py(.bat), 배포판은 ``meme-core.exe blockfees``(block_fees.bat).

사용: blockfees <YYYY-MM-DD | A..B> <HH:MM:SS> [초=2] [--me 0x주소] [--ours | --coin a,b] [--who]
  날짜 범위(A..B)는 평일마다 같은 시각으로 돌리고 끝에 날짜별 IOC 수수료 분포 표를
  hl_block_fees_<첫날>-<끝날>_<시각>.txt로 남긴다(내 주문 위치는 안 봄 — 2026-11-18).
  --me    내 주소(없으면 키링 HL_ACCOUNT_ADDRESS). 시각은 이 PC 시간대.
  --ours  우리 종목(xyz:SMSN·SKHX·HYUNDAI)만 / --coin xyz:SKHX,xyz:SMSN 고른 종목만(기본 = HL 전체).
  --who   주소별 행동(메이커형(LP)/테이커형, 종류별 건수, 취소, 낸 수수료). 신원은 체인에 없다.
탐색기 한도: 오래된 블록은 **하루 100블록**(IP당, 실측 2026-11-18 "More than 100 archived blocks
  queried in one day"). 3초 조사 = 블록 약 42개 + 탐색 수 회라 하루 한두 날치가 상한. 한도에 걸리면
  그날은 실패로 적고 남은 날은 건너뛴다(내일 이어서).
결과는 화면과 (현재 폴더)logs\\probe\\hl_block_fees_<날짜>_<시각>.txt 둘 다에 남는다.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, TextIO

from .hl_block_fees import (
    BlockSummary,
    DayRow,
    asset_id,
    day_row,
    find_block_at,
    format_ahead,
    format_block_line,
    format_day_table,
    format_fill,
    format_positions,
    format_summary,
    format_users,
    ioc_positions,
    kst,
    merge_summaries,
    merge_users,
    summarize_block,
    summarize_fills,
    top_paid_assets,
    weekdays_between,
)

EXPLORER_URL = "https://rpc.hyperliquid.xyz/explorer"
INFO_URL = "https://api.hyperliquid.xyz/info"
OUR_COINS = ("xyz:SMSN", "xyz:SKHX", "xyz:HYUNDAI")
# 기준점(높이, blockTime ms) — 2026-11-04 08:00:01.216 KST 블록(실측). 평균 간격 70.7ms로 첫 추정.
REF_HEIGHT, REF_TIME_MS = 1208130639, 1793746801216
MS_PER_BLOCK = 70.7
# 탐색 기준점은 찾을 때마다 갱신(여러 날 조사에서 다음 날 추정이 가까워져 탐색 호출이 준다 — 탐색기
# 일일 한도 때문에 호출 수가 곧 조사 가능한 날 수, 2026-11-18)
_ref: list[float] = [REF_HEIGHT, REF_TIME_MS, MS_PER_BLOCK]


class ExplorerError(RuntimeError):
    """탐색기가 블록 대신 오류 응답을 줌(예: 하루 한도 "More than 100 archived blocks queried in
    one day")."""


def quota_exceeded(message: str) -> bool:
    """탐색기 오류 문구가 일일 한도인지(순수)."""
    return "archived blocks" in message.lower()
REQUEST_GAP_S = 0.25   # 조회 사이 쉼 — 공개 탐색기 한도(429) 예방
RETRY_WAITS_S = (3, 6, 12, 24, 48)  # 429(Too Many Requests)면 이만큼 쉬고 다시


def _post(url: str, payload: dict[str, Any]) -> Any:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def rpc(payload: dict[str, Any]) -> dict[str, Any]:
    """탐색기 조회 — 429면 쉬었다 다시."""
    for attempt, wait in enumerate((*RETRY_WAITS_S, None)):
        try:
            data = _post(EXPLORER_URL, payload)
            time.sleep(REQUEST_GAP_S)
            return data if isinstance(data, dict) else {}
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
    if d.get("type") == "error":  # 한도 초과 등 — KeyError 대신 사유를 그대로
        raise ExplorerError(f"탐색기 오류: {d.get('message')}")
    inner = d.get("blockDetails")
    return inner if isinstance(inner, dict) else d


def asset_names() -> dict[int, str]:
    """자산 번호 → 이름. 본 dex(``meta``) + 빌더 dex 목록(``perpDexs``, 첫 칸은 본 dex=None)마다
    ``meta``. 실패하면 비어 있는 채(번호로만 본다)."""
    names: dict[int, str] = {}
    try:
        dexs = _post(INFO_URL, {"type": "perpDexs"})
    except Exception:  # noqa: BLE001 - 이름 없이도 번호로 본다
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
            meta = _post(INFO_URL, payload)
        except Exception:  # noqa: BLE001
            continue
        universe = meta.get("universe") if isinstance(meta, dict) else None
        for idx, u in enumerate(universe or []):
            names[asset_id(di, idx)] = str(u.get("name", ""))
    return names


def my_fills(me: str, t0: int, t1: int) -> dict[str, Any]:
    """내 체결(공개 info userFillsByTime, 읽기 전용) → tx 해시별 요약. 실패하면 빈 dict."""
    try:
        fills = _post(INFO_URL, {"type": "userFillsByTime", "user": me,
                                 "startTime": t0, "endTime": t1})
    except Exception as exc:  # noqa: BLE001 - 체결 없이도 나머지는 보여 준다
        print(f"  (체결 조회 실패: {type(exc).__name__}: {exc})", flush=True)
        return {}
    return summarize_fills(fills if isinstance(fills, list) else [])


def default_me() -> str | None:
    try:
        from .config import default_secrets

        return default_secrets().get("HL_ACCOUNT_ADDRESS") or None
    except Exception:  # noqa: BLE001 - 키링 없으면 내 주문 표시만 생략
        return None


def run(start: datetime, seconds: float, me: str | None, out: TextIO,
        coins: tuple[str, ...] = (), who: bool = False) -> DayRow:
    """한 구간 조사 → 화면·파일에 찍고, 날짜별 표용 IOC 합계 한 줄(DayRow)을 돌려준다."""
    def say(line: str = "") -> None:
        print(line, flush=True)
        out.write(line + "\n")
        out.flush()

    t0 = int(start.timestamp() * 1000)
    t1 = t0 + int(seconds * 1000)
    say(f"대상 {start:%Y-%m-%d %H:%M:%S} ~ +{seconds:g}초 (이 PC 시간대) / 내 주소 {me or '없음'}")
    names = asset_names()
    assets: set[int] | None = None
    scope = "HL 전체 종목"  # 합계 줄에도 찍는다(화면에 합계만 보여도 범위를 알 수 있게, 2026-11-18)
    if coins:
        assets = {a for a, n in names.items() if n in coins}
        picked = ", ".join(f"{a}={n}" for a, n in sorted(names.items()) if n in coins)
        say(f"종목 필터: {picked or '(메타에서 못 찾음 — 전체로 셈)'}"
            " (취소는 종목을 알 수 없어 전부 셈)")
        if not assets:
            assets = None
        else:
            scope = "종목 " + ", ".join(n for _a, n in sorted(names.items()) if n in coins)
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

    h = find_block_at(t0, get_time, int(_ref[0]), int(_ref[1]), _ref[2])
    say(f"첫 블록 {h} (탐색 {calls}회)")
    first = block_details(h)
    t_first = int(first["blockTime"])
    if h != REF_HEIGHT:  # 평균 간격을 실측으로 고쳐 다음 탐색 추정을 가깝게
        _ref[2] = (t_first - REF_TIME_MS) / (h - REF_HEIGHT)
    _ref[0], _ref[1] = h, t_first
    first_h = h
    summaries: list[BlockSummary] = []
    while True:
        s = summarize_block(h, first if h == first_h else block_details(h), me, assets)
        summaries.append(s)
        say(format_block_line(s))
        if s.time_ms >= t1 or len(summaries) >= 200:
            break
        h += 1
    by_tif, max_p = merge_summaries(summaries)
    say()
    say(f"== 합계({scope}): 블록 {len(summaries)}개 {kst(summaries[0].time_ms)}"
        f"~{kst(summaries[-1].time_ms)} (p ÷ 10,000 = bp; IOC는 8bp에서 지연 단축 상한) ==")
    for line in format_summary(by_tif, max_p):
        say("  " + line)
    pos = ioc_positions(summaries)
    say("  " + format_positions(pos))
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
    mine = [(s, i, a, txh) for s in summaries for i, a, txh in s.mine]
    if mine:
        say()
        say("== 내 주문 (체결 = 체인 기록 userFills를 tx 해시로 이음; 구간 끝 +60초까지) ==")
        fills = my_fills(me or "", summaries[0].time_ms - 1000, summaries[-1].time_ms + 60_000)
        sent = send_times_for(start)  # cloid → 발주요청 시각(ms) — hl_order 로그가 있으면
        for s, i, a, txh in mine:
            orders = a.get("orders") or []
            desc = ", ".join(
                f"{names.get(o.get('a'), o.get('a'))} {'매수' if o.get('b') else '매도'} "
                f"{o.get('s')} @ {o.get('p')} "
                f"{(o.get('t') or {}).get('limit', {}).get('tif', '?')}" for o in orders)
            say(f"  블록 {s.height} {kst(s.time_ms)} 위치 {i}/{s.n_tx} grouping={a.get('grouping')}"
                f" {desc} tx {txh[:12]}…")
            waits = [s.time_ms - sent[str(o.get("c"))] for o in orders
                     if str(o.get("c")) in sent]
            if waits:
                say(f"      보낸 뒤 블록까지 {waits[0]:,} ms ≈ 블록 {waits[0] / _ref[2]:.0f}개분 "
                    "(발주요청 시각 → 블록 시각; 네트워크 편도 + 노드 대기, PC 시계 오차 포함)")
            if i in s.mine_ahead:
                say(f"      {format_ahead(s.mine_ahead[i])}")
            fs = fills.get(txh)
            say(f"      {format_fill(fs)}" if fs else "      체결 없음(구간 안)")
    elif me:
        say()
        say("(이 구간에 내 주문 없음)")
    return day_row(f"{start:%Y-%m-%d}", len(summaries), by_tif, max_p, pos)


_PACKET_CLOID = re.compile(r'"c": "(0x[0-9a-f]+)"')


def send_times_for(start: datetime) -> dict[str, int]:
    """그날 hl_order 로그(현재 폴더 logs/ 또는 real_log/core_<날짜>/)에서 요청패킷의 cloid →
    발주요청 시각(ms). 없으면 빈 dict(대기 시간 표시만 생략)."""
    day = f"{start:%Y%m%d}"
    for base in (Path.cwd() / "logs", Path.cwd() / "real_log" / f"core_{day}"):
        path = base / f"hl_order_{day}.log"
        if not path.exists():
            continue
        out: dict[str, int] = {}
        last_req: int | None = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if " 발주요청 " in line:
                try:
                    last_req = int(datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                                   .timestamp() * 1000)
                except ValueError:
                    last_req = None
                continue
            if "요청패킷" in line and last_req is not None:
                for c in _PACKET_CLOID.findall(line):
                    out[c] = last_req
        return out
    return {}


def parse_days(text: str) -> list[date]:
    """날짜 인자 → 조사할 날 목록. "YYYY-MM-DD" 하루, "A..B"는 평일만(사용자 2026-11-18 "여러 날
    보기 힘들다"). 형식이 틀리면 ValueError."""
    if ".." in text:
        a, b = text.split("..", 1)
        days = weekdays_between(datetime.strptime(a, "%Y-%m-%d").date(),
                                datetime.strptime(b, "%Y-%m-%d").date())
        if not days:
            raise ValueError(f"범위에 평일이 없습니다: {text!r}")
        return days
    return [datetime.strptime(text, "%Y-%m-%d").date()]


def main(argv: list[str]) -> int:
    """인자 파싱 → run. 결과 파일은 현재 폴더의 logs/probe/."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
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
        days = parse_days(args[0])
        clock = datetime.strptime(args[1], "%H:%M:%S").time()
    except ValueError:
        print(f"날짜·시각 형식이 틀립니다: {args[0]!r} {args[1]!r} — 예: 2026-11-04 08:00:00"
              " 또는 날짜 범위 2026-11-10..2026-11-18")
        return 2
    seconds = float(args[2]) if len(args) > 2 and args[2] else 2.0
    ranged = len(days) > 1
    # 여러 날 조사는 수수료 분포만 본다(사용자 2026-11-18 "내 주문 포함 안 시키고 특정 시간대
    # 수수료만") — 내 주문 위치·체결 조회(me)는 하루 조사에서만
    me = None if ranged else (me or default_me())
    out_dir = Path.cwd() / "logs" / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[DayRow] = []
    for n, day in enumerate(days, 1):
        start = datetime.combine(day, clock)
        out_path = out_dir / f"hl_block_fees_{start:%Y%m%d_%H%M%S}.txt"
        if ranged:
            print(f"\n### {n}/{len(days)} {day} ###", flush=True)
        print(f"결과 파일: {out_path}", flush=True)
        with open(out_path, "w", encoding="utf-8") as out:
            try:
                rows.append(run(start, seconds, me, out, coins, who))
            except Exception as exc:  # noqa: BLE001 - 화면에 원인을 보여 주고(여러 날이면 다음 날로)
                why = str(exc) if isinstance(exc, ExplorerError) else f"{type(exc).__name__}: {exc}"
                msg = f"실패: {why}"
                print(msg, flush=True)
                out.write(msg + "\n")
                if not ranged:
                    return 1
                rows.append(DayRow(f"{day:%Y-%m-%d}", 0, error=why))
                if isinstance(exc, ExplorerError) and quota_exceeded(why):
                    # 한도는 오늘 안 풀린다 — 남은 날은 조회 없이 "한도로 건너뜀"
                    for rest in days[n:]:
                        rows.append(DayRow(f"{rest:%Y-%m-%d}", 0,
                                           error="탐색기 일일 한도로 건너뜀(내일 이어서)"))
                    print(f"탐색기 일일 한도 — 남은 {len(days) - n}일 건너뜀", flush=True)
                    break
        print(f"저장: {out_path}", flush=True)
    if not ranged:
        return 0
    # 날짜별 표 — 화면 + 요약 파일
    table_path = out_dir / (f"hl_block_fees_{days[0]:%Y%m%d}-{days[-1]:%Y%m%d}"
                            f"_{clock:%H%M%S}.txt")
    lines = [f"HL 블록 우선수수료 날짜별(IOC만) — 매일 {clock:%H:%M:%S}부터 {seconds:g}초, "
             f"종목 {', '.join(coins) if coins else 'HL 전체'} (p ÷ 10,000 = bp)",
             *format_day_table(rows)]
    print("\n" + "\n".join(lines), flush=True)
    table_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"요약 저장: {table_path}", flush=True)
    return 0 if all(r.error is None for r in rows) else 1
