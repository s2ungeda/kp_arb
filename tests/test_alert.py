"""텔레그램 알림 모듈 테스트 — 라이브 텔레그램 호출 금지(sender 목 주입)."""
from __future__ import annotations

import json

from kp_arb.alert import (
    build_payload,
    format_alert,
    notify,
    send_url,
    telegram_config,
)


class _FakeSecrets:
    """비밀 조회 목 — 주어진 값만 돌려준다."""

    def __init__(self, **values: str) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


def test_telegram_config_needs_both() -> None:
    assert telegram_config(_FakeSecrets()) is None                      # 둘 다 없음
    assert telegram_config(_FakeSecrets(KP_TELEGRAM_TOKEN="t")) is None  # chat 없음
    assert telegram_config(_FakeSecrets(KP_TELEGRAM_CHAT_ID="c")) is None  # token 없음
    assert telegram_config(
        _FakeSecrets(KP_TELEGRAM_TOKEN="t", KP_TELEGRAM_CHAT_ID="c")) == ("t", "c")


def test_format_alert_level_tags() -> None:
    assert format_alert("x", "info").endswith(" x")
    assert format_alert("x", "warn").startswith("⚠️")
    assert format_alert("x", "error").startswith("🚨")
    assert format_alert("x", "nope") == format_alert("x", "info")  # 알 수 없는 레벨=info


def test_send_url_and_payload() -> None:
    assert send_url("ABC") == "https://api.telegram.org/botABC/sendMessage"
    body = json.loads(build_payload("42", "hi").decode("utf-8"))
    assert body == {"chat_id": "42", "text": "hi"}


def test_notify_noop_when_unconfigured() -> None:
    calls: list[tuple[str, bytes]] = []

    def sender(url: str, body: bytes) -> int:
        calls.append((url, body))
        return 200

    assert notify("경보", secrets=_FakeSecrets(), sender=sender) is False
    assert calls == []  # 미설정이면 sender 호출조차 안 함


def test_notify_sends_when_configured() -> None:
    calls: list[tuple[str, bytes]] = []

    def sender(url: str, body: bytes) -> int:
        calls.append((url, body))
        return 200

    ok = notify("코어 재기동", "warn",
                secrets=_FakeSecrets(KP_TELEGRAM_TOKEN="T", KP_TELEGRAM_CHAT_ID="9"),
                sender=sender)
    assert ok is True
    assert len(calls) == 1
    url, body = calls[0]
    assert url == "https://api.telegram.org/botT/sendMessage"
    payload = json.loads(body.decode("utf-8"))
    assert payload["chat_id"] == "9"
    assert payload["text"].startswith("⚠️")


def test_notify_swallows_sender_error() -> None:
    def boom(url: str, body: bytes) -> int:
        raise RuntimeError("network down")

    ok = notify("x", secrets=_FakeSecrets(KP_TELEGRAM_TOKEN="T", KP_TELEGRAM_CHAT_ID="9"),
                sender=boom)
    assert ok is False  # 예외를 삼키고 False


def test_notify_false_on_non_2xx() -> None:
    ok = notify("x", secrets=_FakeSecrets(KP_TELEGRAM_TOKEN="T", KP_TELEGRAM_CHAT_ID="9"),
                sender=lambda url, body: 500)
    assert ok is False
