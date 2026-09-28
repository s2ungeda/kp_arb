"""xingAPI 접속 점검 — 로그인 · 조회 왕복(ms) · 실시간 수신 (주문 없음).

DESIGN-ls-xing.md §5 1단계 완료 조건용. 32비트 파이썬(.venv32)에서만 돈다(COM).
자격은 keyring(LS_XING_ID/PW/CERT_PW, KP_XING_HOST)에서 읽고 로그에 남기지 않는다.

    .venv32\\Scripts\\python -m kp_arb.xing_check [초]   # 기본 10초 실시간 수신
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path


async def _main(seconds: float) -> int:
    from .config import XingCredentials
    from .gateways.xing_com import Win32ComFactory, XingSession

    log = logging.getLogger("kp_arb.xing_check")
    creds = XingCredentials.load()
    res_dir = Path(creds.path) / "Res"
    log.info("xing 점검 시작 — 서버 %s:%d(%s) Res %s", creds.host, creds.port,
             "모의" if creds.server_type else "실서버", res_dir)
    session = XingSession(Win32ComFactory(creds.path), res_dir)  # 설치 폴더 → DLL 탐색 경로
    await session.start()
    t0 = time.perf_counter()
    accounts = await session.login(creds.host, creds.port, creds.user_id, creds.password,
                                   creds.cert_password, creds.server_type)
    log.info("로그인 %.2fs — 계좌 %d개: %s", time.perf_counter() - t0, len(accounts),
             [a[:4] + "…" + a[-2:] for a in accounts])
    # 조회 왕복 — t1102(삼성전자) 5회
    for i in range(5):
        t1 = time.perf_counter()
        r = await session.query("t1102", {"t1102InBlock": {"shcode": "005930"}})
        ms = (time.perf_counter() - t1) * 1000
        out = r.blocks.get("t1102OutBlock", {})
        log.info("t1102 #%d 왕복 %.1fms rsp=%s %s 현재가 %s", i + 1, ms, r.rsp_cd, r.rsp_msg,
                 out.get("price"))
    limits = await session.limits("t1102")
    log.info("t1102 한도(xing 제공): 초당 %d · 기준 %d초 · 한도 %d · 현재 %d", *limits)
    # 실시간 — 삼성 KRX 호가/체결 + 통합 호가 + JIF
    got: dict[str, int] = {}
    first: dict[str, float] = {}

    def on_real(tr: str, _key: str, fields: dict[str, str]) -> None:
        got[tr] = got.get(tr, 0) + 1
        if tr not in first:
            first[tr] = time.perf_counter()
            log.info("첫 수신 %s: %s", tr, {k: fields[k] for k in list(fields)[:6]})

    session.on_real.append(on_real)
    t2 = time.perf_counter()
    for tr, key in (("H1_", "005930"), ("S3_", "005930"), ("UH1", "U005930   "), ("JIF", "0"),
                    ("CUR", "USD   ")):
        await session.advise(tr, key)
    log.info("실시간 등록 완료 %.1fms — %.0f초 수신 대기", (time.perf_counter() - t2) * 1000,
             seconds)
    await asyncio.sleep(seconds)
    log.info("수신 건수: %s", got)
    await session.unadvise_all()
    session.close()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    return asyncio.run(_main(seconds))


if __name__ == "__main__":
    raise SystemExit(main())
