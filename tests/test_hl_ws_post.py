"""HL 주문 WS post 클라이언트 계약 테스트 — 가짜 연결(라이브 없음), DESIGN-hl-ws-post.md."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from kp_arb.gateways.hl_live import install_ws_post
from kp_arb.gateways.hl_ws_post import (
    HLWsPostClient,
    HLWsPostError,
    HLWsPostUnavailable,
)
from kp_arb.gateways.ls_ws import WSClosed


class ScriptedConnection:
    """보낸 post에 각본대로 응답하는 가짜 연결. 응답은 큐로 흘려 ``async for``가 받는다."""

    def __init__(self, reply: Any = None, *, raise_on_send: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.reply = reply  # callable(id, payload) -> 응답 frame(str) | None
        self.raise_on_send = raise_on_send

    async def send(self, message: str) -> None:
        if self.raise_on_send:
            raise ConnectionError("boom")
        msg = json.loads(message)
        self.sent.append(msg)
        if msg.get("method") == "post" and self.reply is not None:
            frame = self.reply(msg["id"], msg["request"]["payload"])
            if frame is not None:
                await self.queue.put(frame)

    def push(self, frame: str) -> None:
        self.queue.put_nowait(frame)

    def close(self) -> None:
        self.queue.put_nowait(None)

    async def _gen(self) -> AsyncIterator[str]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen()


class ScriptedConnector:
    def __init__(self, conns: list[ScriptedConnection]) -> None:
        self.conns = list(conns)
        self.connects = 0

    async def connect(self) -> ScriptedConnection:
        if not self.conns:
            raise WSClosed()
        self.connects += 1
        return self.conns.pop(0)


def ok_frame(rid: int, status: str = "ok") -> str:
    return json.dumps({"channel": "post", "data": {"id": rid, "response": {
        "type": "action", "payload": {"status": status, "response": {"type": "order"}}}}})


def error_frame(rid: int, text: str) -> str:
    return json.dumps({"channel": "post", "data": {"id": rid, "response": {
        "type": "error", "payload": text}}})


def payload(kind: str = "order") -> dict[str, Any]:
    return {"action": {"type": kind}, "nonce": 1, "signature": {"r": "0x1"},
            "vaultAddress": None, "expiresAfter": None}


async def _start(client: HLWsPostClient) -> asyncio.Task[None]:
    task = asyncio.create_task(client.run())
    for _ in range(50):
        if client.ready:
            break
        await asyncio.sleep(0.01)
    assert client.ready
    return task


@pytest.mark.asyncio
async def test_post_sends_official_frame_and_matches_reply_by_id() -> None:
    conn = ScriptedConnection(reply=lambda rid, _p: ok_frame(rid))
    client = HLWsPostClient(ScriptedConnector([conn]), reconnect_backoff_s=0.0)
    task = await _start(client)
    result = await client.post_action(payload("cancel"), timeout_s=1.0)
    assert result == {"status": "ok", "response": {"type": "order"}}
    frame = conn.sent[-1]
    assert frame["method"] == "post" and frame["id"] == 1
    assert frame["request"] == {"type": "action", "payload": payload("cancel")}
    assert client.sent_count == 1 and client.status.rx_count == 1
    conn.close()
    await task


@pytest.mark.asyncio
async def test_replies_out_of_order_still_matched() -> None:
    conn = ScriptedConnection()  # 응답은 손으로 밀어 넣는다
    client = HLWsPostClient(ScriptedConnector([conn]), reconnect_backoff_s=0.0)
    task = await _start(client)
    a = asyncio.create_task(client.post_action(payload(), 1.0))
    b = asyncio.create_task(client.post_action(payload(), 1.0))
    await asyncio.sleep(0.01)
    conn.push(ok_frame(2, "second"))  # 둘째 요청 응답이 먼저
    conn.push(ok_frame(1, "first"))
    assert (await a)["status"] == "first"
    assert (await b)["status"] == "second"
    conn.close()
    await task


@pytest.mark.asyncio
async def test_error_reply_raises_and_unknown_frames_ignored() -> None:
    conn = ScriptedConnection(reply=lambda rid, _p: error_frame(rid, "429: rate limited"))
    client = HLWsPostClient(ScriptedConnector([conn]), reconnect_backoff_s=0.0)
    task = await _start(client)
    conn.push(json.dumps({"channel": "pong"}))
    conn.push(json.dumps({"channel": "post", "data": {"id": 99, "response": {}}}))  # 대기 없음
    with pytest.raises(HLWsPostError, match="429"):
        await client.post_action(payload(), 1.0)
    assert not client._pending
    conn.close()
    await task


@pytest.mark.asyncio
async def test_not_connected_is_unavailable_without_sending() -> None:
    client = HLWsPostClient(ScriptedConnector([]), reconnect_backoff_s=0.0)
    with pytest.raises(HLWsPostUnavailable):
        await client.post_action(payload(), 1.0)
    with pytest.raises(HLWsPostUnavailable):
        client.post_action_threadsafe(payload(), 1.0)  # 루프도 없음
    assert client.sent_count == 0


@pytest.mark.asyncio
async def test_disconnect_fails_pending_then_reconnects_with_new_ping() -> None:
    first, second = ScriptedConnection(), ScriptedConnection(reply=lambda rid, _p: ok_frame(rid))
    client = HLWsPostClient(ScriptedConnector([first, second]), reconnect_backoff_s=0.0,
                            ping_interval_s=0.01)
    task = await _start(client)
    waiting = asyncio.create_task(client.post_action(payload(), 5.0))
    await asyncio.sleep(0.03)
    assert any(m.get("method") == "ping" for m in first.sent)  # 첫 연결에서 ping이 돌았다
    first.close()  # 서버가 닫음 → 대기 요청 실패 + 재접속
    with pytest.raises(ConnectionError, match="끊김"):
        await waiting
    for _ in range(50):
        if client.ready and client.status.connects == 2:
            break
        await asyncio.sleep(0.01)
    assert client.status.connects == 2 and client.status.disconnects == 1
    assert (await client.post_action(payload(), 1.0))["status"] == "ok"  # 새 연결로 나간다
    await asyncio.sleep(0.03)
    assert any(m.get("method") == "ping" for m in second.sent)  # 재접속에도 ping 재시작
    second.close()
    await task


@pytest.mark.asyncio
async def test_timeout_is_unknown_result_and_clears_pending() -> None:
    conn = ScriptedConnection()  # 응답 없음
    client = HLWsPostClient(ScriptedConnector([conn]), reconnect_backoff_s=0.0)
    task = await _start(client)
    with pytest.raises(TimeoutError):
        await client.post_action(payload(), 0.02)
    assert not client._pending and client.sent_count == 1
    conn.close()
    await task


@pytest.mark.asyncio
async def test_send_failure_raises_connection_error() -> None:
    conn = ScriptedConnection(raise_on_send=True)
    client = HLWsPostClient(ScriptedConnector([conn]), reconnect_backoff_s=0.0)
    task = await _start(client)
    with pytest.raises(ConnectionError):
        await client.post_action(payload(), 1.0)
    assert not client._pending
    conn.close()
    await task


# --- SDK 훅(install_ws_post) ---


class FakeExchange:
    """SDK Exchange의 _post_action 자리만 흉내 — 본문 모양·HTTP 폴백 확인용."""

    vault_address = None
    expires_after = None

    def __init__(self) -> None:
        self.http_calls: list[tuple[Any, Any, int]] = []

    def _post_action(self, action: Any, signature: Any, nonce: int) -> Any:
        self.http_calls.append((action, signature, nonce))
        return {"status": "ok", "via": "http"}


class FakePostClient:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.payloads: list[dict[str, Any]] = []
        self.fallback_count = 0

    def post_action_threadsafe(self, payload: dict[str, Any], timeout_s: float) -> Any:
        if not self.available:
            raise HLWsPostUnavailable("down")
        self.payloads.append(payload)
        return {"status": "ok", "via": "ws"}


def test_install_ws_post_builds_sdk_payload_and_routes_to_ws() -> None:
    ex, client = FakeExchange(), FakePostClient()
    assert install_ws_post(ex, client, timeout_s=3.0)
    resp = ex._post_action({"type": "order", "orders": []}, {"r": "0x1"}, 1234)
    assert resp == {"status": "ok", "via": "ws"} and not ex.http_calls
    assert client.payloads == [{"action": {"type": "order", "orders": []}, "nonce": 1234,
                                "signature": {"r": "0x1"}, "vaultAddress": None,
                                "expiresAfter": None}]
    # 이체류 액션은 vaultAddress를 비운다(SDK 원본과 같음)
    ex.vault_address = "0xvault"
    ex._post_action({"type": "sendAsset"}, {}, 1)
    ex._post_action({"type": "cancel"}, {}, 2)
    assert client.payloads[-2]["vaultAddress"] is None
    assert client.payloads[-1]["vaultAddress"] == "0xvault"


def test_install_ws_post_falls_back_to_http_when_ws_unavailable() -> None:
    ex, client = FakeExchange(), FakePostClient(available=False)
    assert install_ws_post(ex, client)
    resp = ex._post_action({"type": "cancel"}, {"r": "0x1"}, 7)
    assert resp == {"status": "ok", "via": "http"}
    assert ex.http_calls == [({"type": "cancel"}, {"r": "0x1"}, 7)]
    assert client.fallback_count == 1


def test_install_ws_post_refuses_sdk_without_hook() -> None:
    class NoHook:
        pass

    assert install_ws_post(NoHook(), FakePostClient()) is False
