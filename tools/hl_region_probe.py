"""HL 지연 지역 비교 — 도쿄 리전 vs 한국(사용자 2026-11-10 "도쿄 리전에서 테스트 한번").

주문을 내지 않는다. 1단계는 키 없이 /info 왕복, 2단계는 존재하지 않는 주문번호로 취소를 보내
/exchange 왕복을 재고(주문·포지션 없음) 체인 블록 시각과 맞춰 요청→블록·블록→응답을 나눈다.
필요: pip install hyperliquid-python-sdk  (이 파일 하나만 복사하면 됨, 저장소 불필요)
키: 환경변수 HL_AGENT_KEY(에이전트 키)·HL_ACCOUNT_ADDRESS. 없으면 1단계만.
      원격 PC에는 시험용 에이전트 키를 새로 만들어 쓰고 끝나면 지운다(출금 권한 없음).
시계: 원격 PC는 NTP 동기화 상태여야 요청→블록 값이 맞다(chrony/timesyncd 확인).
사용: python hl_region_probe.py [횟수=5] [꼬리표=여기]
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

INFO_URL = "https://api.hyperliquid.xyz/info"
EXPLORER_URL = "https://rpc.hyperliquid.xyz/explorer"
COIN, DEX = "xyz:SMSN", "xyz"
FAKE_OID = 123456789  # 존재하지 않는 주문번호
KST = timezone(timedelta(hours=9))


def post(url: str, payload: dict[str, Any]) -> Any:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def kst(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%H:%M:%S.%f")[:-3]


def stat(name: str, xs: list[float]) -> str:
    return (f"{name}: n={len(xs)} 중앙 {statistics.median(xs):.0f}ms "
            f"최소 {min(xs):.0f} 최대 {max(xs):.0f}")


def stage_info(n: int) -> None:
    rtts = []
    for i in range(n):
        t0 = time.perf_counter()
        post(INFO_URL, {"type": "meta", "dex": DEX})
        ms = (time.perf_counter() - t0) * 1000
        rtts.append(ms)
        print(f"  info {i + 1}회 {ms:.0f}ms")
        time.sleep(1.0)
    print(stat("[1단계] /info 왕복", rtts))


def stage_cancel(n: int, key: str, addr: str) -> None:
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    ex = Exchange(Account.from_key(key), constants.MAINNET_API_URL,
                  account_address=addr, perp_dexs=[DEX], timeout=10)
    sent: list[tuple[float, float]] = []  # (보낸 시각 ms, 응답 시각 ms) 벽시계
    for i in range(n):
        t_send = time.time() * 1000
        t0 = time.perf_counter()
        try:
            resp = ex.cancel(COIN, FAKE_OID)
            tail = json.dumps(resp, ensure_ascii=False)[:90]
        except Exception as exc:  # noqa: BLE001
            tail = f"EXC {type(exc).__name__}: {exc}"
        ms = (time.perf_counter() - t0) * 1000
        sent.append((t_send, t_send + ms))
        print(f"  취소 {i + 1}회 {kst(t_send)} 왕복 {ms:.0f}ms  {tail}")
        time.sleep(1.5)
    rtts = [b - a for a, b in sent]
    print(stat("[2단계] /exchange 취소 왕복", rtts))
    # 체인 블록 시각 대조(공개 탐색기, 429면 잠시 뒤 다시). 탐색기 색인이 몇 초 늦을 수 있어
    # 건수가 모자라면 기다렸다 다시 본다(도쿄 실측 2026-11-11: 2초 뒤 0건, 뒤늦게 5건 확인).
    rows: list[dict[str, Any]] = []
    for attempt in range(6):
        time.sleep(3.0 * (attempt + 1))
        try:
            d = post(EXPLORER_URL, {"type": "userDetails", "user": addr})
            rows = [t for t in d.get("txs", [])
                    if t.get("time", 0) >= sent[0][0] - 500
                    and t.get("action", {}).get("type") == "cancel"]
        except Exception as exc:  # noqa: BLE001
            print(f"  탐색기 조회 실패({exc}) — 잠시 뒤 다시")
            continue
        if len(rows) >= len(sent):
            break
        print(f"  탐색기에 {len(rows)}건/{len(sent)}건 — 색인 대기 뒤 다시")
    rows.sort(key=lambda t: t["time"])
    if len(rows) != len(sent):
        print(f"  체인 기록 {len(rows)}건 ≠ 보낸 {len(sent)}건 — 블록 대조 생략"
              "(시계·탐색기 지연 확인)")
        return
    to_block, to_resp = [], []
    for (a, b), t in zip(sent, rows, strict=True):
        tb = float(t["time"])
        to_block.append(tb - a)
        to_resp.append(b - tb)
        print(f"  블록 {t.get('block')} {kst(tb)}  요청→블록 {tb - a:.0f}ms"
              f"  블록→응답 {b - tb:.0f}ms")
    print(stat("[2단계] 요청→블록", to_block))
    print(stat("[2단계] 블록→응답", to_resp))
    if min(to_block) < 0:
        print("  ※ 요청→블록 음수 = 이 PC 시계가 느림. NTP 동기화 뒤 다시.")


def main(argv: list[str]) -> int:
    n = int(argv[0]) if argv else 5
    tag = argv[1] if len(argv) > 1 else "여기"
    print(f"== HL 지연 측정 [{tag}] {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST, 각 {n}회 ==")
    stage_info(n)
    key, addr = os.environ.get("HL_AGENT_KEY"), os.environ.get("HL_ACCOUNT_ADDRESS")
    if key and addr:
        stage_cancel(n, key, addr)
    else:
        print("[2단계] 건너뜀 — HL_AGENT_KEY/HL_ACCOUNT_ADDRESS 환경변수 없음")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
