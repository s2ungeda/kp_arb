"""읽기전용 페이퍼 라이브 점검 — 잔고/포지션만 조회. **주문 없음(안전)**.

    python -m kp_arb.paper_check

- `KP_MODE`(기본 paper)와 `LSAccounts.load()`(keyring/env)로 접속.
- 예수금/증거금 원시 응답(JSON)을 출력 → 실제 TR 필드명 정합 확인.
- 파싱된 잔고/포지션도 시도(파서 필드명이 실제와 다르면 에러를 그대로 출력).
- `place_order`는 호출하지 않는다(읽기전용 게이트).

base_url은 기본 운영 도메인(모의는 모의 appkey 사용). 다르면 env `LS_BASE_URL`로 오버라이드.
"""
from __future__ import annotations

import asyncio
import os

from .config import LSAccounts, current_mode
from .domain.enums import Account
from .gateways.ls import LIVE_BASE_URL, LSApiGateway
from .gateways.ls_http import AiohttpRestTransport, AiohttpTokenTransport

_ACCT_TR = {
    Account.KR_STOCK: (LSApiGateway.STOCK_DEPOSIT_TR, LSApiGateway.STOCK_ACC_PATH),
    Account.KR_DERIV: (LSApiGateway.DERIV_TR, LSApiGateway.DERIV_ACC_PATH),
}


async def _run(base_url: str) -> None:
    import aiohttp

    accounts = LSAccounts.load()
    async with aiohttp.ClientSession() as session:
        token_tx = AiohttpTokenTransport(session, base_url, scope=os.environ.get("LS_SCOPE", "oob"))
        rest_tx = AiohttpRestTransport(session)
        gw = LSApiGateway.from_accounts(
            accounts, token_transport=token_tx, rest_transport=rest_tx, base_url=base_url
        )
        await gw.connect()

        for account in (Account.KR_STOCK, Account.KR_DERIV):
            tr_cd, path = _ACCT_TR[account]
            print(f"\n=== {account.value} : {tr_cd} ===")
            try:
                raw = await gw.raw_request(account, tr_cd, path)
                print(f"  status={raw.status_code}")
                print(f"  raw body keys: {list(raw.body)}")
                print(f"  raw body: {raw.body}")
            except Exception as exc:  # noqa: BLE001 - 진단 출력
                print(f"  RAW ERROR: {exc!r}")
            try:
                bal = await gw.get_balance(account)
                print(f"  parsed balance = {bal}")
            except Exception as exc:  # noqa: BLE001
                print(f"  parse balance ERROR (필드명 정합 필요): {exc!r}")


def main() -> None:
    # 편의: .env가 있으면 환경변수로 로드(라이브러리 코드는 여전히 env/keyring만 읽음).
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    base_url = os.environ.get("LS_BASE_URL", LIVE_BASE_URL)
    print(f"mode={current_mode().value}  base_url={base_url}  (읽기전용, 주문 없음)")
    asyncio.run(_run(base_url))


if __name__ == "__main__":
    main()
