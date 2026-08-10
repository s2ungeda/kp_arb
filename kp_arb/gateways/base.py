"""게이트웨이 계약 (DESIGN.md §5.1, §5.2). 구현은 Claude Code가 채운다."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ..domain.enums import Account, Underlying
from ..domain.models import OrderIntent, Position

if TYPE_CHECKING:
    from ..order_book import TrackedOrder


class LSGateway(ABC):
    """LS Open API 게이트웨이 (주식계좌 + 선물옵션계좌). REST+WS, OAuth2."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def place_order(self, intent: OrderIntent) -> str: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    async def get_positions(self, account: Account) -> Sequence[Position]: ...

    @abstractmethod
    async def get_balance(self, account: Account) -> float: ...

    @abstractmethod
    async def get_open_orders(self, account: Account) -> Sequence[TrackedOrder]:
        """미체결 주문 스냅샷(최초 실행/온디맨드 조회용)."""


class HLGateway(ABC):
    """Hyperliquid HIP-3 perp 게이트웨이 (Trade.xyz). 에이전트 서명."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def place_order(self, intent: OrderIntent) -> str: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    async def amend_order(
        self, order_id: str, *, qty: float | None = None, price: float | None = None
    ) -> str:
        """정정(modify). 기본은 미지원 — 지원 게이트웨이(HLSdkGateway)가 재정의."""
        raise NotImplementedError("이 게이트웨이는 정정을 지원하지 않는다")

    @abstractmethod
    async def get_positions(self) -> Sequence[Position]: ...

    @abstractmethod
    async def get_funding(self, underlying: Underlying) -> float: ...

    async def get_position_details(self) -> dict[Underlying, dict[str, Any]]:
        """clearinghouseState 포지션 상세(마진·청산가·레버리지 등) — 표시용, 종목별.

        필수 아님(기본 빈 dict). HLSdkGateway가 실제 구현(잔고표 B2·레버리지 D).
        """
        return {}

    async def update_leverage(
        self, underlying: Underlying, leverage: int, *, is_cross: bool
    ) -> None:
        """레버리지·마진모드 변경(updateLeverage) — 주문과 별개 액션(§1-3). 기본 미지원."""
        raise NotImplementedError("이 게이트웨이는 레버리지 변경을 지원하지 않는다")

    @abstractmethod
    async def get_open_orders(self) -> Sequence[TrackedOrder]:
        """미체결 주문 스냅샷(최초 실행/온디맨드 조회용)."""
