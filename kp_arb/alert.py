"""텔레그램 알림 — 무인 24시간 운영 경보 (BULID_PLAN Phase 8).

연결 끊김·코어 재기동·인증 오류·RateLimit 초과 등을 사람 텔레그램으로 밀어준다.
토큰·chat_id는 **비밀**(config.default_secrets: env → Windows 자격증명관리자)로만 읽는다.

설계 원칙(운영 안전):
- **미설정이면 조용히 무시(no-op)** — 알림을 안 걸어도 본 기능이 막히지 않는다.
- **전송 실패를 삼킨다** — 알림 때문에 호출자(코어·화면)가 죽으면 안 된다.
- 실제 HTTP는 주입 가능한 sender 뒤로 격리 — 테스트는 라이브 텔레그램을 호출하지 않는다.

비밀 이름::

    KP_TELEGRAM_TOKEN    봇 토큰 (BotFather 발급)
    KP_TELEGRAM_CHAT_ID  받을 대화 id (본인 채팅)

호출 예: ``alert.notify("코어 재기동됨", level="warn")``. asyncio 안에서는 블로킹을
피하려 ``asyncio.to_thread(alert.notify, ...)`` 로 감싸 쓴다.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from urllib import request as urllib_request

from .config import SecretProvider, default_secrets

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"

# 레벨별 접두 표식 (한눈에 심각도 구분)
_LEVEL_TAG: dict[str, str] = {
    "info": "ℹ️",
    "warn": "⚠️",
    "error": "🚨",
}

# (url, json_body) -> HTTP 상태코드. 테스트는 여기에 목을 넣는다.
Sender = Callable[[str, bytes], int]

# 지금은 보내지 않는 알림 종류(사용자 2026-09-21: "당분간은 자동주문 중지됐을 때만") —
# ws = WS 재연결·재동기 완료, core = 코어 미접속 자동 재기동·재기동 포기. 다시 받으려면 여기서 뺀다.
# 보내는 종류: halt(자동주문 세트 중지), reply(수신 확인 답장), test(시험 전송), general(기타).
MUTED_CATEGORIES: frozenset[str] = frozenset({"ws", "core"})


def telegram_config(secrets: SecretProvider | None = None) -> tuple[str, str] | None:
    """(token, chat_id) — 둘 다 있을 때만. 하나라도 없으면 None(미설정 = no-op)."""
    provider = secrets if secrets is not None else default_secrets()
    token = provider.get("KP_TELEGRAM_TOKEN")
    chat_id = provider.get("KP_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    return token, chat_id


def format_alert(text: str, level: str = "info") -> str:
    """레벨 표식 + 본문. 알 수 없는 레벨은 info로 취급."""
    tag = _LEVEL_TAG.get(level, _LEVEL_TAG["info"])
    return f"{tag} {text}"


def send_url(token: str) -> str:
    """sendMessage 엔드포인트 URL."""
    return f"{_API_BASE}/bot{token}/sendMessage"


def build_payload(chat_id: str, text: str) -> bytes:
    """텔레그램 sendMessage JSON 본문(bytes)."""
    return json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")


def _http_post(url: str, body: bytes) -> int:
    """고정 https 호스트로 JSON POST. 상태코드 반환(라이브 — 테스트는 sender 주입)."""
    req = urllib_request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib_request.urlopen(req, timeout=5.0) as resp:  # 고정 https 호스트
        return int(resp.status)


def notify(
    text: str,
    level: str = "info",
    *,
    category: str = "general",
    secrets: SecretProvider | None = None,
    sender: Sender | None = None,
) -> bool:
    """알림 전송. 성공 시 True. 미설정·실패·꺼 둔 종류는 False(예외 없이 삼킴).

    ``category``가 ``MUTED_CATEGORIES``에 있으면 보내지 않는다(로그에만 debug).
    ``sender``는 (url, body)->상태코드. 기본은 실제 HTTP, 테스트는 목을 주입한다.
    """
    if category in MUTED_CATEGORIES:
        log.debug("telegram 꺼 둔 종류(%s) — 안 보냄: %s", category, text)
        return False
    cfg = telegram_config(secrets)
    if cfg is None:
        log.debug("telegram 미설정 — 알림 버림: %s", text)
        return False
    token, chat_id = cfg
    body = build_payload(chat_id, format_alert(text, level))
    try:
        status = (sender or _http_post)(send_url(token), body)
    except Exception as exc:  # noqa: BLE001 - 알림 실패가 호출자를 죽이지 않게
        log.warning("telegram 전송 실패: %s", exc)
        return False
    if not 200 <= status < 300:
        log.warning("telegram 전송 거부 status=%s", status)
        return False
    return True


def _main() -> None:
    """시험 전송 — ``python -m kp_arb.alert [메시지]``. 설정 여부와 전송 결과를 출력한다
    (토큰·chat_id 값은 찍지 않는다)."""
    import sys

    text = " ".join(sys.argv[1:]) or "kp-arb 텔레그램 시험 전송"
    if telegram_config() is None:
        print("미설정 — KP_TELEGRAM_TOKEN / KP_TELEGRAM_CHAT_ID를 키 등록 화면에서 넣으세요")
        return
    ok = notify(text, "info", category="test")
    print("전송 성공" if ok else "전송 실패 — 토큰·chat_id·네트워크 확인")


if __name__ == "__main__":
    _main()
