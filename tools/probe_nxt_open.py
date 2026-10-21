"""오전 NXT 개장 실측(스크래치, 모의 접속·주문 없음) — DESIGN-nxt-open.md §7.
NXT 체결(NS3)·호가(NH1), KRX 체결(S3_)·호가(H1_), 환율(CUR), 장운영(JIF)은 xing으로,
HL은 웹소켓 원문(bbo·체결·마크/오라클·l2Book)을 그대로 저장한다.
시작 때와 끝에 하나고시 현물환도 한 번씩 적는다.
사용: PYTHONPATH=. .venv32/Scripts/python tools/probe_nxt_open.py <시작> <끝> <파일|auto>
      (시각은 HH:MM:SS) 예: … 07:59:30 08:00:40 auto → logs/probe/nxt_open_YYYYMMDD.jsonl
(코어가 떠 있어도 됨 — 별도 로그인, 10-20 실증. tools/probe_nxt_open.bat로도 실행.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from kp_arb.config import XingCredentials, load_config  # noqa: E402
from kp_arb.gateways.fx_spot import fetch_usdkrw_spot  # noqa: E402
from kp_arb.gateways.hl_ws import HLWebSocketClient, HLWebSocketConnector  # noqa: E402
from kp_arb.gateways.xing_com import Win32ComFactory, XingSession  # noqa: E402

STK = ("005930", "000660")
SUBS = ([(tr, f"N{c}   ") for c in STK for tr in ("NS3", "NH1")]      # NXT 체결·호가
        + [(tr, c) for c in STK for tr in ("S3_", "H1_")]             # KRX 체결·호가(비교용)
        + [("JIF", "0"), ("CUR", "USD   ")])


def until(hms: str) -> float:
    now = datetime.now()
    tgt = now.replace(hour=int(hms[:2]), minute=int(hms[3:5]), second=int(hms[6:8]), microsecond=0)
    return (tgt - now).total_seconds()


def stamp() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


async def main(start: str, end: str, out: str) -> None:
    wait = until(start)
    if wait > 0:
        print(f"{start}까지 {wait:.0f}초 대기", flush=True)
        await asyncio.sleep(wait)
    if out == "auto":
        out = str(ROOT / "logs" / "probe" / f"nxt_open_{time.strftime('%Y%m%d')}.jsonl")
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    buf: list[str] = []
    count: dict[str, int] = {}
    t0 = time.perf_counter()

    def put(src: str, tr: str, row: dict[str, object]) -> None:
        line = {"ms": round((time.perf_counter() - t0) * 1000, 2), "pc": stamp(),
                "src": src, "tr": tr}
        line.update(row)
        buf.append(json.dumps(line, ensure_ascii=False))
        count[f"{src}:{tr}"] = count.get(f"{src}:{tr}", 0) + 1

    # 하나고시 현물환(시작)
    try:
        put("hana", "spot", {"usdkrw": await fetch_usdkrw_spot()})
    except Exception as exc:  # noqa: BLE001
        put("hana", "spot", {"error": str(exc)})

    # HL 웹소켓 — 원문 그대로(bbo·trades·activeAssetCtx·l2Book)
    symbols = load_config(str(ROOT / "config.yaml")).hl_symbols()
    hl = HLWebSocketClient(HLWebSocketConnector(), symbols=symbols,
                           max_reconnects=1_000, reconnect_backoff_s=1.0)
    hl.subscribe_marks()
    hl.subscribe_bbo()
    hl.subscribe_l2book()
    hl.subscribe_trades()

    def on_raw(text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        ch = str(msg.get("channel", "?"))
        if ch in ("subscriptionResponse", "pong"):
            return
        put("hl", ch, {"data": msg.get("data")})

    hl.on_raw.append(on_raw)
    hl_task = asyncio.create_task(hl.run())

    # xing — NXT·KRX·환율·장운영
    creds = XingCredentials.load()
    session = XingSession(Win32ComFactory(creds.path), Path(creds.path) / "Res")
    await session.start()
    await session.login(creds.host, creds.port, creds.user_id, creds.password,
                        creds.cert_password, creds.server_type)

    def on_real(tr: str, key: str, f: dict[str, str]) -> None:
        put("xing", tr, {"key": key.strip(), **f})

    session.on_real.append(on_real)
    failed = []
    for tr, key in SUBS:
        try:
            await session.advise(tr, key)
        except Exception as exc:  # noqa: BLE001
            failed.append((tr, key, str(exc)))
    print("등록 완료", stamp(), "실패:", failed, flush=True)

    while until(end) > 0:
        await asyncio.sleep(5)
        lines, buf[:] = list(buf), []
        if lines:
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        print(stamp(), dict(sorted(count.items())), flush=True)
    try:
        put("hana", "spot", {"usdkrw": await fetch_usdkrw_spot()})
    except Exception as exc:  # noqa: BLE001
        put("hana", "spot", {"error": str(exc)})
    lines, buf[:] = list(buf), []
    if lines:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    hl_task.cancel()
    await session.unadvise_all()
    session.close()
    print("끝", stamp(), "저장", out, sum(count.values()), "건")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
