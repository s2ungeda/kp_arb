"""텔레그램 수신 테스트 — 라이브 텔레그램 호출 금지(fetch 목 주입)."""
from typing import Any

from kp_arb.telegram_rx import Incoming, TelegramReceiver, parse_updates, updates_url


class _FakeSecrets:
    def __init__(self, **values: str) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


_SECRETS = _FakeSecrets(KP_TELEGRAM_TOKEN="T", KP_TELEGRAM_CHAT_ID="9")


def _update(uid: int, chat_id: int, text: str | None, user: str = "me") -> dict[str, Any]:
    msg: dict[str, Any] = {"chat": {"id": chat_id}, "from": {"username": user}}
    if text is not None:
        msg["text"] = text
    return {"update_id": uid, "message": msg}


def test_updates_url_carries_offset_and_long_poll_timeout() -> None:
    assert updates_url("T", None, 25) == "https://api.telegram.org/botT/getUpdates?timeout=25"
    assert updates_url("T", 101, 25).endswith("getUpdates?timeout=25&offset=101")
    assert updates_url("T", -1, 0).endswith("getUpdates?timeout=0&offset=-1")


def test_parse_updates_accepts_only_registered_chat() -> None:
    # 등록된 chat_id의 글자 메시지만 받는다 — 봇 아이디를 아는 누구나 말을 걸 수 있으므로.
    # 다음 offset은 (버린 것 포함) 받은 update_id 최댓값 + 1.
    body = {"ok": True, "result": [
        _update(100, 9, "  상태  "),
        _update(101, 777, "전체 정지", user="stranger"),   # 다른 대화 — 버림
        _update(102, 9, None),                              # 사진 등 글자 아님 — 버림
        {"update_id": 103, "edited_message": {}},            # message 아님 — 버림
        _update(104, 9, "잔고"),
    ]}
    msgs, nxt, ignored = parse_updates(body, "9")
    assert [(m.update_id, m.text, m.sender) for m in msgs] == [(100, "상태", "me"),
                                                                (104, "잔고", "me")]
    assert nxt == 105 and ignored == 3
    assert parse_updates({"ok": True, "result": []}, "9") == ([], None, 0)
    assert parse_updates({"ok": False}, "9") == ([], None, 0)


async def test_receiver_skips_backlog_then_delivers_new_messages() -> None:
    # 코어가 꺼져 있던 동안 쌓인 글은 처리하지 않는다 — 첫 조회(offset=-1)는 건너뛰기만 하고,
    # 그다음부터 새 글만 넘긴다. offset은 받은 만큼 옮겨 같은 글이 다시 오지 않는다.
    calls: list[str] = []
    bodies = [
        {"ok": True, "result": [_update(50, 9, "옛 글")]},
        {"ok": True, "result": [_update(51, 9, "상태")]},
        {"ok": True, "result": []},
    ]

    def fetch(url: str, _timeout: float) -> dict[str, Any]:
        calls.append(url)
        return bodies[len(calls) - 1]

    got: list[Incoming] = []
    rx = TelegramReceiver(got.append, secrets=_SECRETS, fetch=fetch, poll_timeout_s=1)
    assert await rx.poll_once() == []            # 옛 글 건너뜀
    assert calls[0].endswith("timeout=0&offset=-1")
    msgs = await rx.poll_once()
    assert [m.text for m in msgs] == ["상태"] and calls[1].endswith("timeout=1&offset=51")
    assert await rx.poll_once() == [] and calls[2].endswith("offset=52")


async def test_receiver_without_config_does_nothing() -> None:
    def fetch(_url: str, _timeout: float) -> dict[str, Any]:
        raise AssertionError("미설정이면 조회하지 않는다")

    rx = TelegramReceiver(lambda _m: None, secrets=_FakeSecrets(), fetch=fetch)
    assert await rx.poll_once() == []
    await rx.run()  # 바로 끝난다
