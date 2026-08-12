"""호가 추적(페깅) 판단 로직 테스트 + step 실행 분기(LS 정정 / HL 취소후신규)."""
from typing import Any

from kp_arb.domain.enums import Instrument, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent, Quote
from kp_arb.order_book import OrderBook
from kp_arb.peg_order import PegController
from kp_arb.pegging import PegAction, decide, target_price


def make_quote(*, with_depth: bool = True) -> Quote:
    return Quote(
        underlying=Underlying.SK_HYNIX,
        instrument=Instrument.KR_STOCK,
        bid=100_000,
        ask=100_100,
        ts=0.0,
        bids=[(100_000, 10), (99_900, 20), (99_800, 30)] if with_depth else None,
        asks=[(100_100, 5), (100_200, 15), (100_300, 25)] if with_depth else None,
    )


# --- target_price ---


def test_target_price_uses_depth() -> None:
    q = make_quote()
    assert target_price(q, Side.BUY, 1) == 100_000
    assert target_price(q, Side.BUY, 2) == 99_900
    assert target_price(q, Side.SELL, 3) == 100_300


def test_target_price_beyond_depth_is_none() -> None:
    assert target_price(make_quote(), Side.BUY, 4) is None


def test_target_price_fallback_level1_without_depth() -> None:
    # HL bbo처럼 다단계가 없으면 1호가만 지원.
    q = make_quote(with_depth=False)
    assert target_price(q, Side.BUY, 1) == 100_000
    assert target_price(q, Side.SELL, 1) == 100_100
    assert target_price(q, Side.BUY, 2) is None


def test_target_price_no_quote_or_bad_level() -> None:
    assert target_price(None, Side.BUY, 1) is None
    assert target_price(make_quote(), Side.BUY, 0) is None


# --- decide ---


def test_decide_wait_when_no_target() -> None:
    assert decide(current_price=None, target=None).action is PegAction.WAIT


def test_decide_place_when_no_order() -> None:
    d = decide(current_price=None, target=99_900)
    assert d.action is PegAction.PLACE
    assert d.price == 99_900


def test_decide_none_when_price_matches() -> None:
    d = decide(current_price=99_900, target=99_900)
    assert d.action is PegAction.NONE
    assert d.price is None


def test_decide_amends_when_target_moves() -> None:
    # AMEND는 '목표가로 옮겨라'는 추상 판단(실행은 LS=정정 TR / HL=취소후신규로 분기).
    d = decide(current_price=99_900, target=100_000)
    assert d.action is PegAction.AMEND
    assert d.price == 100_000


# --- step 실행 분기 (LS 정정 / HL 취소후신규) ---


class _FakePegSystem:
    """peg step 실행 확인용 최소 시스템 — place/cancel/amend_price 기록."""

    def __init__(self) -> None:
        self.order_book = OrderBook()
        self.quotes: dict[Any, Quote] = {}
        self.placed: list[tuple[str, float]] = []
        self.cancelled: list[str] = []
        self.amended: list[tuple[str, float]] = []
        self._n = 0

    async def place(self, intent: OrderIntent) -> str:
        self._n += 1
        oid = f"P{self._n}"
        self.placed.append((oid, intent.price or 0.0))
        self.order_book.track(oid, intent)
        return oid

    async def cancel(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self.order_book.on_cancel(order_id)

    async def amend_price(self, order_id: str, price: float, *,
                          reduce_only: bool = False, post_only: bool = False) -> str:
        self.amended.append((order_id, price))
        return order_id  # 정정은 원주문번호 유지(가짜)


def _hl_quote(bid: float) -> Quote:
    return Quote(underlying=Underlying.SAMSUNG, instrument=Instrument.HL_PERP,
                 bid=bid, ask=bid + 0.5, ts=0.0)  # bbo(다단계 없음)


def _ls_quote(bid: float) -> Quote:
    return Quote(underlying=Underlying.SAMSUNG, instrument=Instrument.KR_STOCK,
                 bid=bid, ask=bid + 100, ts=0.0,
                 bids=[(bid, 10)], asks=[(bid + 100, 10)])


async def test_peg_hl_moves_by_cancel_then_new_not_amend() -> None:
    # HL은 정정 금지 — 호가 이동 시 정정이 아니라 '취소 후 신규'로 옮긴다.
    sys = _FakePegSystem()
    key = (Underlying.SAMSUNG, Instrument.HL_PERP, "hl")
    pc = PegController(system=sys, venue=Venue.HYPERLIQUID,  # type: ignore[arg-type]
                       underlying=Underlying.SAMSUNG, instrument=Instrument.HL_PERP,
                       side=Side.BUY, level=1, qty=1.0)
    sys.quotes[key] = _hl_quote(100.0)
    await pc.step()                                   # 최초 PLACE @100
    assert len(sys.placed) == 1 and pc.order_id == "P1"

    sys.quotes[key] = _hl_quote(101.0)                # 호가 이동 → 옮겨야 함
    st = await pc.step()
    assert sys.amended == []                          # 정정 안 함
    assert sys.cancelled == ["P1"]                    # 이전 주문 취소
    assert len(sys.placed) == 2 and pc.order_id == "P2"  # 신규 발주
    assert "취소후신규" in st


async def test_peg_ls_moves_by_amend() -> None:
    # LS는 정정 TR로 옮긴다(취소·신규 아님).
    sys = _FakePegSystem()
    key = (Underlying.SAMSUNG, Instrument.KR_STOCK, "krx")
    pc = PegController(system=sys, venue=Venue.LS,  # type: ignore[arg-type]
                       underlying=Underlying.SAMSUNG, instrument=Instrument.KR_STOCK,
                       side=Side.BUY, level=1, qty=10.0)
    sys.quotes[key] = _ls_quote(100_000)
    await pc.step()                                   # PLACE
    sys.quotes[key] = _ls_quote(100_100)              # 호가 이동
    await pc.step()
    assert sys.amended == [("P1", 100_100.0)]         # 정정
    assert sys.cancelled == []                        # 취소 없음
    assert len(sys.placed) == 1                       # 신규 없음
