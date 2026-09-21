"""텔레그램 수신 — 사용자가 봇에게 보낸 글을 코어가 받는다 (2026-09-21, 수신만).

알림 전송(alert.py)의 반대 방향. 텔레그램 봇 API ``getUpdates`` **롱폴링**으로 받는다 — 서버가 새
글이 올 때까지 연결을 붙잡고 있다가 바로 돌려주므로 사실상 실시간이고, 이 PC로 들어오는 포트를 열
필요가 없다.

지금 단계(사용자 "일단 수신만"): 받은 글을 **코어 로그에 남기고 "수신: …"으로 답장**만 한다. 명령
해석·실행은 아직 없다(후속 — 넣더라도 조회·정지 계열만, 실행을 켜거나 주문을 내는 명령은 안 둔다).

안전:
- **등록된 chat_id에서 온 글만** 받는다. 봇 아이디를 아는 누구나 말을 걸 수 있으므로 그 외는 버린다.
- 코어가 꺼져 있던 동안 쌓인 옛 글은 처리하지 않는다(시동 때 건너뜀).
- 토큰은 비밀(config.default_secrets)로만 읽고 로그에 찍지 않는다. 미설정이면 조용히 끝난다.
- 실제 HTTP는 주입 가능한 fetch 뒤로 격리 — 테스트는 라이브 텔레그램을 호출하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import request as urllib_request

from . import alert
from .config import SecretProvider

log = logging.getLogger(__name__)

POLL_TIMEOUT_S = 25      # 롱폴링 대기(초) — 텔레그램이 이 시간 동안 새 글을 기다렸다 돌려준다
ERROR_BACKOFF_S = 5.0    # 조회 실패 뒤 쉬는 시간

# (url, timeout_s) -> 응답 JSON(dict). 테스트는 여기에 목을 넣는다.
Fetch = Callable[[str, float], dict[str, Any]]


@dataclass(frozen=True)
class Incoming:
    """받은 글 1건."""

    update_id: int
    chat_id: str
    sender: str
    text: str


def updates_url(token: str, offset: int | None, timeout_s: int) -> str:
    """getUpdates 주소 — offset 이후 것만, timeout 초 동안 기다림(롱폴링)."""
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout={int(timeout_s)}"
    return url if offset is None else f"{url}&offset={int(offset)}"


def parse_updates(body: dict[str, Any], chat_id: str) -> tuple[list[Incoming], int | None, int]:
    """getUpdates 응답 → (등록된 chat_id의 글 목록, 다음 offset, 버린 건수). 순수 함수.

    다음 offset = 받은 update_id 최댓값 + 1(그래야 같은 글이 다시 안 온다). 글이 없으면 None.
    다른 대화에서 온 글·글자가 아닌 것(사진 등)은 버린 건수로만 센다.
    """
    results = body.get("result") if isinstance(body, dict) else None
    if not isinstance(results, list):
        return [], None, 0
    out: list[Incoming] = []
    ignored = 0
    last: int | None = None
    for upd in results:
        if not isinstance(upd, dict):
            continue
        uid = upd.get("update_id")
        if isinstance(uid, int):
            last = uid if last is None else max(last, uid)
        msg = upd.get("message")
        if not isinstance(msg, dict):
            ignored += 1
            continue
        raw_chat, raw_from = msg.get("chat"), msg.get("from")
        chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
        frm: dict[str, Any] = raw_from if isinstance(raw_from, dict) else {}
        text = msg.get("text")
        if str(chat.get("id")) != str(chat_id) or not isinstance(text, str) or not text.strip():
            ignored += 1
            continue
        sender = str(frm.get("username") or frm.get("first_name") or "")
        out.append(Incoming(update_id=int(uid) if isinstance(uid, int) else 0,
                            chat_id=str(chat.get("id")), sender=sender, text=text.strip()))
    return out, (last + 1 if last is not None else None), ignored


def _http_get(url: str, timeout_s: float) -> dict[str, Any]:
    """고정 https 호스트로 GET → JSON(라이브 — 테스트는 fetch 주입)."""
    with urllib_request.urlopen(url, timeout=timeout_s) as resp:  # 고정 https 호스트
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


class TelegramReceiver:
    """봇에 온 글을 받아 ``on_message``로 넘긴다. 미설정이면 ``run()``이 바로 끝난다."""

    def __init__(self, on_message: Callable[[Incoming], None], *,
                 secrets: SecretProvider | None = None, fetch: Fetch | None = None,
                 poll_timeout_s: int = POLL_TIMEOUT_S) -> None:
        self._on_message = on_message
        self._secrets = secrets
        self._fetch = fetch or _http_get
        self._poll_timeout_s = poll_timeout_s
        self._offset: int | None = None
        self._primed = False  # 시동 때 쌓여 있던 옛 글을 건너뛰었는가

    async def poll_once(self) -> list[Incoming]:
        """한 번 조회 — 새 글을 돌려주고 offset을 옮긴다. 첫 조회는 쌓인 옛 글을 건너뛰기만 한다."""
        cfg = alert.telegram_config(self._secrets)
        if cfg is None:
            return []
        token, chat_id = cfg
        if not self._primed:
            # offset=-1: 가장 최근 글 하나만 — 그 다음부터 받는다(코어가 꺼져 있던 동안의 글은 버림)
            body = await asyncio.to_thread(self._fetch, updates_url(token, -1, 0), 10.0)
            _msgs, nxt, _ign = parse_updates(body, chat_id)
            self._offset = nxt
            self._primed = True
            return []
        body = await asyncio.to_thread(
            self._fetch, updates_url(token, self._offset, self._poll_timeout_s),
            float(self._poll_timeout_s + 10))
        msgs, nxt, ignored = parse_updates(body, chat_id)
        if nxt is not None:
            self._offset = nxt
        if ignored:
            log.warning("텔레그램 수신: 등록 안 된 대화·글자 아닌 것 %d건 버림", ignored)
        return msgs

    async def run(self) -> None:
        if alert.telegram_config(self._secrets) is None:
            log.info("텔레그램 수신 안 함 — 토큰·chat_id 미설정")
            return
        log.info("텔레그램 수신 시작(롱폴링 %d초, 등록된 대화만)", self._poll_timeout_s)
        while True:
            try:
                for msg in await self.poll_once():
                    try:
                        # 처리(답장 전송 등)는 스레드로 — 코어의 이벤트 루프를 막지 않는다
                        await asyncio.to_thread(self._on_message, msg)
                    except Exception:  # noqa: BLE001 - 처리 오류로 수신이 죽지 않게
                        log.exception("텔레그램 수신 처리 오류")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 네트워크 오류 등 — 쉬었다 다시
                log.warning("텔레그램 수신 조회 실패 — %ds 뒤 재시도: %s", int(ERROR_BACKOFF_S),
                            type(exc).__name__)
                await asyncio.sleep(ERROR_BACKOFF_S)


def log_and_ack(msg: Incoming) -> None:
    """지금 단계의 처리 — 코어 로그에 남기고 같은 대화로 "수신: …" 답장(명령 실행 없음)."""
    log.info("텔레그램 수신 [%s]: %s", msg.sender or msg.chat_id, msg.text)
    alert.notify(f"수신: {msg.text}", "info", category="reply")
