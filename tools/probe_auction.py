"""장마감 동시호가 실측(스크래치, 모의). 시세 수신만 — 주문 없음.
주식선물 예상체결(YJC)·호가(JH0)·체결(JC0), 원달러선물 예상체결(YC3 — 통화선물 포함 여부 확인용)·체결(FC9)·호가(FH9),
주식 예상체결(YS3·NYS·UYS)·NXT 호가/체결(NH1·NS3), 장운영(JIF)을 전부 원문 그대로 저장한다.
사용: PYTHONPATH=. .venv32/Scripts/python xing_auction_probe.py <시작 HH:MM:SS> <끝 HH:MM:SS> <저장 파일>
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

load_dotenv(Path(r"C:\project\VibeCode\kp-arb\.env"))
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from kp_arb.config import XingCredentials  # noqa: E402
from kp_arb.gateways.xing_com import Win32ComFactory, XingSession  # noqa: E402

FUT = ("A116A000", "A506A000")            # 삼성·하이닉스 주식선물 10월물
FX = ("A756A000", "A756B000")              # 원달러선물 근·차근
STK = ("005930", "000660")
SUBS = ([(tr, c) for c in FUT for tr in ("YJC", "JH0", "JC0")]
        + [(tr, c) for c in FX for tr in ("YC3", "YF9", "FC9", "FH9")]
        + [("YS3", c) for c in STK] + [("H1_", c) for c in STK] + [("S3_", c) for c in STK]
        + [(tr, f"N{c}   ") for c in STK for tr in ("NYS", "NH1", "NS3")]
        + [(tr, f"U{c}   ") for c in STK for tr in ("UYS",)]
        + [("JIF", "0"), ("CUR", "USD   ")])


def until(hms: str) -> float:
    now = datetime.now()
    tgt = now.replace(hour=int(hms[:2]), minute=int(hms[3:5]), second=int(hms[6:8]), microsecond=0)
    return (tgt - now).total_seconds()


async def main(start: str, end: str, out: str) -> None:
    wait = until(start)
    if wait > 0:
        print(f"{start}까지 {wait:.0f}초 대기", flush=True)
        await asyncio.sleep(wait)
    creds = XingCredentials.load()
    session = XingSession(Win32ComFactory(creds.path), Path(creds.path) / "Res")
    await session.start()
    await session.login(creds.host, creds.port, creds.user_id, creds.password,
                        creds.cert_password, creds.server_type)
    buf: list[str] = []
    count: dict[str, int] = {}
    t0 = time.perf_counter()

    def on_real(tr: str, key: str, f: dict[str, str]) -> None:
        now = time.time()
        row = {"ms": round((time.perf_counter() - t0) * 1000, 2), "tr": tr, "key": key.strip(),
               "pc": time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"}
        row.update(f)
        buf.append(json.dumps(row, ensure_ascii=False))
        count[tr] = count.get(tr, 0) + 1

    session.on_real.append(on_real)
    failed = []
    for tr, key in SUBS:
        try:
            await session.advise(tr, key)
        except Exception as exc:  # noqa: BLE001
            failed.append((tr, key, str(exc)))
    print("등록 완료", time.strftime("%H:%M:%S"), "실패:", failed, flush=True)
    path = Path(out)
    path.write_text("", encoding="utf-8")
    while until(end) > 0:
        await asyncio.sleep(10)
        lines, buf[:] = list(buf), []
        if lines:
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        print(time.strftime("%H:%M:%S"), dict(sorted(count.items())), flush=True)
    await session.unadvise_all()
    session.close()
    print("끝", time.strftime("%H:%M:%S"), "저장", out, sum(count.values()), "건")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
