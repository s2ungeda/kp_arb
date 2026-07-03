"""도메인 모델(pydantic). 게이트웨이·엔진·전략이 주고받는 계약 타입."""
from __future__ import annotations

from pydantic import BaseModel, field_validator, model_validator

from .enums import Account, Instrument, OrderType, Side, Underlying, Venue


class Quote(BaseModel):
    underlying: Underlying
    instrument: Instrument
    bid: float
    ask: float
    ts: float
    bid_qty: float | None = None  # 매수 1호가 잔량 (모니터/전략 참고용)
    ask_qty: float | None = None  # 매도 1호가 잔량
    market: str = "krx"           # 가격 원천 시장: "krx" | "nxt" | "hl"
    bids: list[tuple[float, float]] | None = None  # 다단계 매수호가 [(가격, 잔량), ...]
    asks: list[tuple[float, float]] | None = None  # 다단계 매도호가

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class InstrumentStatus(BaseModel):
    instrument: Instrument
    tradeable: bool = False
    is_auction: bool = False
    is_reference: bool = False


class Position(BaseModel):
    venue: Venue
    instrument: Instrument
    underlying: Underlying
    side: Side
    qty: float
    avg_price: float
    account: Account | None = None

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side is Side.BUY else -self.qty


class OrderIntent(BaseModel):
    """전략이 산출하고 리스크·게이트웨이로 흐르는 주문 의도."""

    venue: Venue
    underlying: Underlying
    instrument: Instrument
    side: Side
    qty: float
    order_type: OrderType = OrderType.LIMIT
    price: float | None = None
    account: Account | None = None  # LS 전용. None이면 instrument로 자동 라우팅.

    @field_validator("qty")
    @classmethod
    def _qty_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("qty must be > 0")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> OrderIntent:
        if self.instrument.venue != self.venue:
            raise ValueError(f"instrument {self.instrument} not on venue {self.venue}")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("limit order requires price")
        if self.venue is Venue.LS:
            from ..routing import account_for

            expected = account_for(self.instrument)
            if self.account is None:
                self.account = expected
            elif self.account != expected:
                raise ValueError(f"account {self.account} != routed {expected}")
        elif self.account is not None:
            raise ValueError("Hyperliquid order must not carry a KR account")
        return self


class MarketState(BaseModel):
    """전략 입력 (DESIGN.md §6). underlying 단위 시장 상태 스냅샷."""

    underlying: Underlying
    reference_instrument: Instrument | None
    reference_price_krw: float | None
    hl_mark_usd: float | None
    usdkrw: float | None
    session: dict[Instrument, InstrumentStatus]
    positions: list[Position] = []
