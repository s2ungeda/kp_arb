"""게이트웨이 계약 (DESIGN.md §5.1, §5.2). 구현은 Claude Code가 채운다."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ..domain.enums import Account, Side, Underlying
from ..domain.models import OrderIntent, Position

if TYPE_CHECKING:
    from ..order_book import TrackedOrder


def placed_at_from_ms(ms: object) -> str:
    """거래소 접수시각(epoch ms, HL frontendOpenOrders ``timestamp``) → 'HH:MM:SS'(현지 시각).
    시동 조회로 알게 된 주문도 주문 리스트 '접수시각' 칸이 비지 않게. 값이 없거나 이상하면 ''."""
    import time

    try:
        sec = float(str(ms)) / 1000.0
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(sec))


def placed_epoch_from_ms(ms: object) -> float:
    """epoch ms → epoch 초(정렬용). 없거나 이상하면 0.0."""
    try:
        sec = float(str(ms)) / 1000.0
    except (TypeError, ValueError):
        return 0.0
    return sec if sec > 0 else 0.0


def placed_at_from_hhmmss(raw: object) -> str:
    """LS 시각 문자열(``HHMMSS`` 또는 ``HHMMSSmmm``, CSPAQ13700 OrdTime·t0434 ordtime) →
    'HH:MM:SS'. 숫자 6자리 미만이면 ''."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) < 6:
        return ""
    return f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}"


def placed_epoch_from_hhmmss(raw: object, now: float | None = None) -> float:
    """LS 시각 문자열 → 오늘 날짜의 epoch 초(정렬용). LS 미체결은 당일 주문뿐이라 날짜는 오늘.
    시각이 없으면 0.0(맨 뒤로 정렬)."""
    import time

    hhmmss = placed_at_from_hhmmss(raw)
    if not hhmmss:
        return 0.0
    base = time.localtime(time.time() if now is None else now)
    h, m, s = (int(p) for p in hhmmss.split(":"))
    return time.mktime((base.tm_year, base.tm_mon, base.tm_mday, h, m, s, 0, 0, -1))


class LSGateway(ABC):
    """LS Open API 게이트웨이 (주식계좌 + 선물옵션계좌). REST+WS, OAuth2."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def place_order(self, intent: OrderIntent) -> str: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, qty: float | None = None) -> None:
        """취소. qty = 남은 수량(호출자가 장부 기준으로 넘김) — LS는 취소수량이 취소가능수량을
        넘으면 거부(01443, 실측 2026-09-09: 원주문 수량으로 보내 부분체결 뒤 거부)."""

    def open_orders_supported(self, account: Account) -> bool:
        """이 계좌의 미체결 조회(get_open_orders)가 실제 조회인가. False면 재동기 때 그 계좌의
        추적 주문을 유령으로 지우지 않는다(실측 2026-09-09: 선물 미체결 TR 미확인 → 빈 결과를
        조회 성공으로 봐 걸려 있던 선주문을 장부에서 지움)."""
        return True

    @abstractmethod
    async def get_positions(self, account: Account) -> Sequence[Position]: ...

    @abstractmethod
    async def get_balance(self, account: Account) -> float: ...

    @abstractmethod
    async def get_open_orders(self, account: Account) -> Sequence[TrackedOrder]:
        """미체결 주문 스냅샷(최초 실행/온디맨드 조회용)."""

    async def place_fx_futures(self, code: str, side: Side, qty: int,
                               price: float) -> str:
        """원달러선물 헤지 발주(KR_FX, §9.1) — 종목코드 직접 지정. 기본 미지원.

        3주식 Underlying 모델 밖의 전용 경로(OrderBook 미거침). LSApiGateway/Mock가 구현.
        """
        raise NotImplementedError("이 게이트웨이는 원달러선물 발주를 지원하지 않는다")


class HLGateway(ABC):
    """Hyperliquid HIP-3 perp 게이트웨이 (Trade.xyz). 에이전트 서명."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def place_order(self, intent: OrderIntent, cloid: str | None = None) -> str:
        """발주 → 거래소 주문번호(oid). cloid(클라이언트 주문번호, DESIGN §HL cloid)를 주면 주문에
        실어 보낸다 — 응답 전 통보(orderUpdates)로 oid를 식별하고, 응답 유실 시 조회로 복구."""

    def new_cloid(self) -> str | None:
        """클라이언트 주문번호 생성 — 지원 안 하면 None(목 등). HLSdkGateway가 구현."""
        return None

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    async def amend_order(
        self, order_id: str, *, qty: float | None = None, price: float | None = None,
        reduce_only: bool = False, post_only: bool = False,
    ) -> str:
        """정정(modify). reduce_only·post_only는 정정 화면이 명시 전달(원주문 상속 안 함).
        기본은 미지원 — 지원 게이트웨이(HLSdkGateway)가 재정의."""
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

    async def get_positions_and_details(
        self,
    ) -> tuple[Sequence[Position], dict[Underlying, dict[str, Any]]]:
        """포지션 + 상세를 함께(REST 왕복 절감). 기본은 두 메서드 조합, HLSdkGateway가 1회로."""
        return list(await self.get_positions()), await self.get_position_details()

    async def get_leverage_settings(self) -> dict[Underlying, dict[str, Any]]:
        """코인별 레버리지·마진모드(포지션 무관, activeAssetData) — 표시 캡션 보정용(§D).

        필수 아님(기본 빈 dict). HLSdkGateway가 실제 구현 — 미보유 종목도 실제 배수 표시.
        """
        return {}

    async def get_instrument_meta(self) -> dict[Underlying, dict[str, Any]]:
        """종목 메타(code·szDecimals·maxLeverage) — 시동 종목정보(§5.10). 기본 빈 dict."""
        return {}

    def pop_place_fill(self) -> tuple[float, float] | None:
        """직전 발주의 즉시체결(수량, 평균가) — 없으면 None. HLSdkGateway가 구현(§즉시체결)."""
        return None

    async def update_leverage(
        self, underlying: Underlying, leverage: int, *, is_cross: bool
    ) -> None:
        """레버리지·마진모드 변경(updateLeverage) — 주문과 별개 액션(§1-3). 기본 미지원."""
        raise NotImplementedError("이 게이트웨이는 레버리지 변경을 지원하지 않는다")

    @abstractmethod
    async def get_open_orders(self) -> Sequence[TrackedOrder]:
        """미체결 주문 스냅샷(최초 실행/온디맨드 조회용)."""
