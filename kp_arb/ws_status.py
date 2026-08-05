"""WS 세션 현황 — 연결상태·수신카운트 추적 (Phase 8-3).

각 WS 게이트웨이(LS 시세·LS 주문·HL 시세·HL 주문)가 하나씩 들고, run 루프에서 연결/끊김을,
_dispatch에서 수신을 기록한다. 순수 상태 객체(시계는 주입) — 무데이터 판정(``is_stale``)은
주문 안전차단(Phase 8-6)이 재사용하고, 메인창이 표(no·거래소·이름·상태·수신카운트)로 표시한다.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass
class WsStatus:
    """WS 한 채널의 현황. 시각은 단조시계(time.monotonic)를 호출부가 주입한다."""

    venue: str            # "LS" | "HL"
    name: str             # 표시 이름 예 "LS 시세"
    kind: str             # "시세" | "주문"
    expects_stream: bool  # 시세=True(계속 수신) / 주문=False(체결 때만 옴)
    connected: bool = False
    rx_count: int = 0
    last_rx: float | None = None   # 마지막 수신 시각(주입된 단조시계)
    connects: int = 0              # 연결 성공 누적
    disconnects: int = 0           # 끊김 누적

    def on_connect(self) -> None:
        self.connected = True
        self.connects += 1

    def on_disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    def on_message(self, now: float) -> None:
        self.rx_count += 1
        self.last_rx = now

    def is_stale(self, now: float, max_idle_s: float) -> bool:
        """무데이터/끊김이면 True(주문 내면 위험). 주문 피드는 무데이터가 정상이라
        연결 여부만 본다 — 체결이 없어도 끊긴 게 아니다."""
        if not self.connected:
            return True
        if not self.expects_stream:
            return False
        if self.last_rx is None:
            return True  # 시세인데 아직 한 건도 못 받음
        return (now - self.last_rx) > max_idle_s

    def to_dict(self) -> dict[str, Any]:
        """JSON 스냅샷(코어 → 메인창). 파생값(stale)은 표시부가 시각을 넣어 계산."""
        return {
            "venue": self.venue,
            "name": self.name,
            "kind": self.kind,
            "connected": self.connected,
            "rx_count": self.rx_count,
            "disconnects": self.disconnects,
            "last_rx": self.last_rx,
        }


def order_block_reason(
    statuses: Sequence[WsStatus], now: float, max_idle_s: float
) -> str | None:
    """주문을 막아야 하면 그 사유(문자열), 괜찮으면 None (Phase 8-6 주문 안전차단).

    - 채널이 하나도 없으면(접속 전) 막는다.
    - 끊긴 채널이 있으면 막는다(하나라도).
    - 시세 채널이 무데이터(N초 초과)면 막는다 — 데이터 없이 발주는 위험.
    끊김과 지연을 구분해 사유를 돌려준다(로그·알림·화면 표시용).
    """
    if not statuses:
        return "WS 미접속"
    down = [s.name for s in statuses if not s.connected]
    if down:
        return f"WS 끊김: {', '.join(down)}"
    stale = [s.name for s in statuses if s.connected and s.is_stale(now, max_idle_s)]
    if stale:
        return f"시세 지연(무데이터 {max_idle_s:.0f}s+): {', '.join(stale)}"
    return None
