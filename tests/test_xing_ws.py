"""xingAPI 실시간(xing_ws) — 가짜 세션: advise 목록, 프레임 변환→부모 파서, 끊김→재로그인→재동기."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from kp_arb.domain.enums import Underlying
from kp_arb.gateways.ls_ws import Fill
from kp_arb.gateways.xing_ws import XingRealClient, frame_from_real


class FakeSession:
    def __init__(self) -> None:
        self.on_real: list[Any] = []
        self.on_session: list[Any] = []
        self.advised: list[tuple[str, str]] = []
        self.fail_tr: set[str] = set()

    async def advise(self, tr: str, key: str) -> None:
        if tr in self.fail_tr:
            raise RuntimeError("Res 없음")
        self.advised.append((tr, key))

    def push(self, tr: str, fields: dict[str, str]) -> None:
        for cb in self.on_real:
            cb(tr, "", fields)

    def drop(self) -> None:
        for cb in self.on_session:
            cb("disconnect", "", "")


def test_frame_from_real_picks_key_and_keeps_fields() -> None:
    raw = frame_from_real("H1_", {"shcode": "005930", "bidho1": "70000"})
    assert json.loads(raw) == {"header": {"tr_cd": "H1_", "tr_key": "005930"},
                               "body": {"shcode": "005930", "bidho1": "70000"}}
    fx = json.loads(frame_from_real("FC9", {"futcode": "A7569000", "price": "1380.1"}))
    assert fx["header"]["tr_key"] == "A7569000"
    assert fx["body"]["shcode"] == "A7569000"  # 선물·원달러 실시간은 futcode뿐 → shcode로도
    assert json.loads(frame_from_real("SC1", {"ordno": "1"}))["header"]["tr_key"] == ""
    assert "shcode" not in json.loads(frame_from_real("SC1", {"ordno": "1"}))["body"]


async def test_run_advises_subs_dispatches_frames_and_resyncs_after_drop() -> None:
    fake = FakeSession()
    logins: list[int] = []

    async def ensure_login() -> None:
        logins.append(1)

    client = XingRealClient(fake, ensure_login=ensure_login,  # type: ignore[arg-type]
                            etf_symbols={Underlying.SAMSUNG: "122630"}, reconnect_backoff_s=0.0)
    client.subscribe_quotes(Underlying.SAMSUNG)
    client.subscribe_stock_fills()
    client.subscribe_market_status()
    quotes: list[Any] = []
    fills: list[Fill] = []
    reconnects: list[int] = []
    client.on_quote.append(quotes.append)
    client.on_fill.append(fills.append)
    client.on_reconnect.append(lambda: reconnects.append(1))
    task = asyncio.ensure_future(client.run())
    for _ in range(50):
        if fake.advised:
            break
        await asyncio.sleep(0.01)
    # 구독 목록이 그대로 XAReal advise로(H1_/UH1/NH1 + ETF, 계좌 통보는 키 없음, JIF "0")
    assert ("H1_", "005930") in fake.advised and ("UH1", "U005930   ") in fake.advised
    assert ("H1_", "122630") in fake.advised and ("SC1", "") in fake.advised
    assert ("JIF", "0") in fake.advised and logins == [1] and reconnects == []
    assert client.status.connected and client.status.connects == 1
    # XAReal 이벤트 → 부모 파서 → on_quote(Quote) / on_fill(Fill)
    fake.push("H1_", {"shcode": "005930", "hotime": "093000", "bidho1": "70000",
                      "offerho1": "70100", "bidrem1": "10", "offerrem1": "20"})
    fake.push("SC1", {"ordno": "1234", "execno": "9", "execqty": "3", "execprc": "70100",
                      "exectime": "093001000"})
    assert quotes and quotes[0].bid == 70000.0 and quotes[0].ask == 70100.0
    assert quotes[0].underlying is Underlying.SAMSUNG and quotes[0].market == "krx"
    assert fills and fills[0].order_id == "1234" and fills[0].qty == 3.0
    assert client.status.rx_count >= 2
    # 끊김 → 재로그인 → 재advise → on_reconnect(재동기)
    n_before = len(fake.advised)
    fake.drop()
    for _ in range(100):
        if reconnects:
            break
        await asyncio.sleep(0.01)
    assert reconnects == [1] and logins == [1, 1] and len(fake.advised) == 2 * n_before
    assert client.status.connects == 2 and client.status.connected
    client.stop()
    await asyncio.wait_for(task, 1.0)


async def test_advise_failure_does_not_block_others() -> None:
    fake = FakeSession()
    fake.fail_tr.add("UH1")

    async def ensure_login() -> None:
        return None

    client = XingRealClient(fake, ensure_login=ensure_login,  # type: ignore[arg-type]
                            reconnect_backoff_s=0.0)
    client.subscribe_quotes(Underlying.SK_HYNIX)
    task = asyncio.ensure_future(client.run())
    for _ in range(50):
        if len(fake.advised) >= 2:
            break
        await asyncio.sleep(0.01)
    assert ("H1_", "000660") in fake.advised and ("NH1", "N000660   ") in fake.advised
    assert all(tr != "UH1" for tr, _k in fake.advised)
    client.stop()
    await asyncio.wait_for(task, 1.0)
