"""도메인 열거형. DESIGN.md의 underlying/계좌/instrument 계약을 코드로 고정."""
from __future__ import annotations

from enum import StrEnum


class Underlying(StrEnum):
    SAMSUNG = "samsung"
    SK_HYNIX = "sk_hynix"
    HYUNDAI = "hyundai"

    @property
    def krx_code(self) -> str:
        return {
            Underlying.SAMSUNG: "005930",
            Underlying.SK_HYNIX: "000660",
            Underlying.HYUNDAI: "005380",
        }[self]

    @classmethod
    def from_krx_code(cls, code: str) -> Underlying | None:
        """KRX 종목코드를 underlying으로 역매핑. 미지 코드는 None."""
        for underlying in cls:
            if underlying.krx_code == code:
                return underlying
        return None


class Venue(StrEnum):
    LS = "ls"
    HYPERLIQUID = "hyperliquid"


class Account(StrEnum):
    """LS 2계좌 (DESIGN.md §3)."""

    KR_STOCK = "kr_stock"   # 주식계좌: 주식, ETF
    KR_DERIV = "kr_deriv"   # 선물옵션계좌: 주식선물, 야간선물 (주간·야간 공용)


class Instrument(StrEnum):
    KR_STOCK = "kr_stock"
    KR_ETF = "kr_etf"
    KR_STOCK_FUTURE = "kr_stock_future"  # 정규장 + 애프터마켓(~20:00, 2026-09-14~) 공용
    HL_PERP = "hl_perp"

    @property
    def venue(self) -> Venue:
        return Venue.HYPERLIQUID if self is Instrument.HL_PERP else Venue.LS


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class SessionPhase(StrEnum):
    """장운영 단계. 실제 값은 LS 장운영데이터에서 산출되며, 여기선 입력으로 받는다."""

    REGULAR = "regular"            # 정규장
    PRE_OPEN = "pre_open"          # 동시호가/장전
    NXT = "nxt"                    # NXT/시간외
    AFTER_MARKET = "after_market"  # 애프터마켓 ~20:00 (2026-09-14~, 주식·주식선물)
    DEAD = "dead"                  # 데드존/휴장
