"""LS 실시간 WS 라이브 점검 — 호가·장운영 구독 후 프레임 출력. 읽기전용(주문 없음).

    python -m kp_arb.ws_check [SECONDS]   # 기본 15초

OAuth2 토큰(주식계좌 키)으로 wss://…:9443/websocket 접속 → 삼성 호가·장운영 구독 → 프레임 출력.
실시간 시세는 장 시간에만 흐른다(마감 시 연결·구독은 되나 데이터가 없을 수 있음).
"""
from __future__ import annotations

import asyncio
import os
import sys

from .config import LSAccounts, current_mode
from .domain.enums import Account, Underlying
from .gateways.ls import LIVE_BASE_URL
from .gateways.ls_http import AiohttpTokenTransport
from .gateways.ls_ws_live import LSWebSocketConnector, ls_ws_url


async def _token() -> str:
    import aiohttp

    cred = LSAccounts.load().for_account(Account.KR_STOCK)
    async with aiohttp.ClientSession() as session:
        tx = AiohttpTokenTransport(session, LIVE_BASE_URL, scope=os.environ.get("LS_SCOPE", "oob"))
        return (await tx.fetch_token(cred.appkey, cred.appsecret)).access_token


async def _run(seconds: float) -> None:
    from .gateways.ls_ws import LSWebSocketClient

    token = await _token()
    url = ls_ws_url(current_mode())
    print(f"token ok (len={len(token)}), connecting {url} for {seconds}s ...")
    client = LSWebSocketClient(LSWebSocketConnector(url), token=token)
    client.on_raw.append(lambda r: print("RAW", r[:400]))
    client.on_quote.append(lambda q: print("QUOTE", q.underlying.value, q.bid, q.ask))
    client.on_market_status.append(lambda s: print("MARKET", s.tr_key, s.body))
    client.on_fill.append(lambda f: print("FILL", f.order_id, f.qty, f.price))
    client.subscribe_quotes(Underlying.SAMSUNG)
    client.subscribe_market_status(Underlying.SAMSUNG)
    try:
        await asyncio.wait_for(client.run(), timeout=seconds)
    except TimeoutError:
        print("done (timeout) — 구독/연결 확인됨")


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    asyncio.run(_run(seconds))


if __name__ == "__main__":
    main()
