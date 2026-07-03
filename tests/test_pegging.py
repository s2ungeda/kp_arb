"""호가 추적(페깅) 판단 로직 테스트 — 순수 함수만."""
from kp_arb.domain.enums import Instrument, Side, Underlying, Venue
from kp_arb.domain.models import Quote
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
    assert decide(venue=Venue.LS, current_price=None, target=None).action is PegAction.WAIT


def test_decide_place_when_no_order() -> None:
    d = decide(venue=Venue.LS, current_price=None, target=99_900)
    assert d.action is PegAction.PLACE
    assert d.price == 99_900


def test_decide_none_when_price_matches() -> None:
    d = decide(venue=Venue.LS, current_price=99_900, target=99_900)
    assert d.action is PegAction.NONE
    assert d.price is None


def test_decide_ls_amends() -> None:
    d = decide(venue=Venue.LS, current_price=99_900, target=100_000)
    assert d.action is PegAction.AMEND
    assert d.price == 100_000


def test_decide_hl_cancels_and_places() -> None:
    d = decide(venue=Venue.HYPERLIQUID, current_price=183.5, target=183.6)
    assert d.action is PegAction.CANCEL_PLACE
    assert d.price == 183.6
