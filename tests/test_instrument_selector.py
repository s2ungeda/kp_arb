"""InstrumentSelector 계약 테스트. 순수 로직(세션 맵 입력)."""
from kp_arb.domain.enums import Account, Instrument, SessionPhase, Side, Underlying
from kp_arb.domain.models import InstrumentStatus
from kp_arb.instrument_selector import InstrumentSelector, Selection
from kp_arb.session import build_session

SAMSUNG = Underlying.SAMSUNG


def _status(instrument: Instrument, *, tradeable: bool) -> InstrumentStatus:
    return InstrumentStatus(instrument=instrument, tradeable=tradeable)


# --- 정규장 ---


def test_regular_long_selects_cheapest() -> None:
    session = build_session(SessionPhase.REGULAR)
    # 주식이 가장 저렴 → 주식/주식계좌 선택.
    sel = InstrumentSelector(
        costs={
            Instrument.KR_STOCK: 0.0005,
            Instrument.KR_ETF: 0.002,
            Instrument.KR_STOCK_FUTURE: 0.001,
        }
    ).select(SAMSUNG, Side.BUY, session)
    assert sel == Selection(Instrument.KR_STOCK, Account.KR_STOCK)


def test_regular_short_selects_future_not_spot() -> None:
    session = build_session(SessionPhase.REGULAR)
    # 숏: 주식·ETF 제외 → 정규장에서 유일 후보는 주식선물.
    sel = InstrumentSelector().select(SAMSUNG, Side.SELL, session)
    assert sel == Selection(Instrument.KR_STOCK_FUTURE, Account.KR_DERIV)


def test_short_never_selects_spot_even_if_only_spot() -> None:
    # 주식만 거래가능한 상황에서 숏이면 선택 불가(None).
    session = {Instrument.KR_STOCK: _status(Instrument.KR_STOCK, tradeable=True)}
    assert InstrumentSelector().select(SAMSUNG, Side.SELL, session) is None


def test_short_excludes_etf_too() -> None:
    session = {
        Instrument.KR_ETF: _status(Instrument.KR_ETF, tradeable=True),
        Instrument.KR_STOCK_FUTURE: _status(Instrument.KR_STOCK_FUTURE, tradeable=True),
    }
    sel = InstrumentSelector().select(SAMSUNG, Side.SELL, session)
    assert sel is not None and sel.instrument is Instrument.KR_STOCK_FUTURE


# --- 애프터마켓 ---


def test_after_market_selects_stock_future() -> None:
    # 애프터마켓: 주식·주식선물 거래. 롱/숏 모두 주식선물 선택(기본 우선순위 + 숏은 spot 제외).
    session = build_session(SessionPhase.AFTER_MARKET)
    for side in (Side.BUY, Side.SELL):
        sel = InstrumentSelector().select(SAMSUNG, side, session)
        assert sel == Selection(Instrument.KR_STOCK_FUTURE, Account.KR_DERIV)


def test_etf_excluded_for_underlying_without_product() -> None:
    # 현대차는 레버리지 ETF 상품이 없음 → ETF 후보에서 제외(config 주입).
    session = build_session(SessionPhase.REGULAR)
    selector = InstrumentSelector(
        costs={Instrument.KR_ETF: 0.0, Instrument.KR_STOCK: 1.0,
               Instrument.KR_STOCK_FUTURE: 1.0},  # ETF가 최저비용이어도
        etf_underlyings=frozenset({Underlying.SAMSUNG, Underlying.SK_HYNIX}),
    )
    sel_samsung = selector.select(Underlying.SAMSUNG, Side.BUY, session)
    sel_hyundai = selector.select(Underlying.HYUNDAI, Side.BUY, session)
    assert sel_samsung is not None and sel_samsung.instrument is Instrument.KR_ETF
    assert sel_hyundai is not None and sel_hyundai.instrument is not Instrument.KR_ETF


# --- tiebreak / 가용성 ---


def test_liquidity_breaks_cost_tie() -> None:
    session = build_session(SessionPhase.REGULAR)
    # 비용 동률 → 유동성 높은 ETF 선택.
    sel = InstrumentSelector(
        liquidity={Instrument.KR_ETF: 100.0},
    ).select(SAMSUNG, Side.BUY, session)
    assert sel is not None and sel.instrument is Instrument.KR_ETF


def test_deadzone_returns_none() -> None:
    session = build_session(SessionPhase.DEAD)
    assert InstrumentSelector().select(SAMSUNG, Side.BUY, session) is None


def test_untradeable_excluded() -> None:
    session = {
        Instrument.KR_STOCK: _status(Instrument.KR_STOCK, tradeable=False),
        Instrument.KR_STOCK_FUTURE: _status(Instrument.KR_STOCK_FUTURE, tradeable=True),
    }
    sel = InstrumentSelector().select(SAMSUNG, Side.BUY, session)
    assert sel is not None and sel.instrument is Instrument.KR_STOCK_FUTURE
