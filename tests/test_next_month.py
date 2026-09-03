"""주식선물 차근월물(KR_STOCK_FUTURE_NEXT) — 도메인·순수 로직 매핑 (DESIGN §5.11, ①단계)."""
from datetime import datetime

from kp_arb.bootstrap import LiveSystem, select_months
from kp_arb.domain.enums import Account, Instrument, SessionPhase, Underlying
from kp_arb.fx import _DEFAULT_MULTIPLIERS
from kp_arb.routing import account_for
from kp_arb.session import build_session, tradeable_instruments
from kp_arb.session_service import market_of_instrument
from kp_arb.strategy_core import (
    Block,
    ScreenKind,
    ScreenState,
    allowed_order_qty,
    hl_qty_for,
)
from kp_arb.ticks import tick_for

NEAR, NEXT = Instrument.KR_STOCK_FUTURE, Instrument.KR_STOCK_FUTURE_NEXT


def test_next_month_shares_futures_rules() -> None:
    # 계좌·세션 시장·틱·승수·수량 규칙이 근월물과 동일해야 한다(빠지면 주식 규칙으로 잘못 흐름).
    assert NEXT.is_stock_future and NEAR.is_stock_future and not Instrument.KR_STOCK.is_stock_future
    assert account_for(NEXT) is Account.KR_DERIV
    assert market_of_instrument(NEXT) == market_of_instrument(NEAR)
    assert tick_for(NEXT, 293_000) == tick_for(NEAR, 293_000) == 500
    assert _DEFAULT_MULTIPLIERS[NEXT] == 10.0
    assert hl_qty_for(NEXT, 3) == 30
    # 선물은 잔고 없어도 매도(숏 스프레드) 가능 — 차근도 동일
    assert allowed_order_qty(Block.EXIT, NEXT, 0, 5, 20) == 5
    assert allowed_order_qty(Block.EXIT, Instrument.KR_STOCK, 0, 5, 20) == 0


def test_next_month_tradeable_in_regular_and_after_market() -> None:
    for phase in (SessionPhase.REGULAR, SessionPhase.AFTER_MARKET):
        assert NEXT in tradeable_instruments(build_session(phase))


def test_selector_never_auto_picks_next_month() -> None:
    # 차근은 세션상 거래 가능해도 자동 선택 후보가 아니다(명시 선택 전용, §5.11).
    from kp_arb.domain.enums import Side
    from kp_arb.instrument_selector import InstrumentSelector

    session = build_session(SessionPhase.REGULAR)
    for side in (Side.BUY, Side.SELL):
        sel = InstrumentSelector().select(Underlying.SAMSUNG, side, session)
        assert sel is not None and sel.instrument is not NEXT


def test_screen_counterpart_follows_future_month() -> None:
    # 자동M 세트 설정 "선물 월물"(near/next)이 상대 상품을 정한다. 자동T는 주식 고정.
    m = ScreenState(kind=ScreenKind.AUTO_M)
    assert m.counterpart is NEAR
    m.future_month = "next"
    assert m.counterpart is NEXT
    t = ScreenState(kind=ScreenKind.AUTO_T, future_month="next")
    assert t.counterpart is Instrument.KR_STOCK


def _live_system() -> LiveSystem:
    # 코어 상태(③) 검증용 최소 조립 — mock 게이트웨이 + 프레임 없는 가짜 WS(라이브 호출 없음).
    from kp_arb.gateways.ls_ws import LSWebSocketClient
    from kp_arb.gateways.mock_ls import MockLSGateway
    from kp_arb.order_book import OrderBook
    from kp_arb.session_service import SessionService

    class _Conn:
        async def send(self, message: str) -> None:
            return None

        def __aiter__(self) -> "_Conn":
            return self

        async def __anext__(self) -> str:
            raise StopAsyncIteration

    class _Connector:
        async def connect(self) -> _Conn:
            return _Conn()

    return LiveSystem(
        gateway=MockLSGateway(),  # type: ignore[arg-type]
        order_book=OrderBook(), session=SessionService(),
        stock_ws=LSWebSocketClient(_Connector()),  # type: ignore[arg-type]
        futures_symbols={Underlying.SAMSUNG: "A1169000"},
        next_futures_symbols={Underlying.SAMSUNG: "A116C000"},
        futures_expiry={(Underlying.SAMSUNG, NEAR): 202712, (Underlying.SAMSUNG, NEXT): 202803},
    )


def test_core_theory_and_board_per_month() -> None:
    # 이론가는 월물별 만기(잔존일)로 — 차근이 근보다 길어 이론가가 더 높다. 괴리보드에 차근 행.
    system = _live_system()
    system.trades[(Underlying.SAMSUNG, Instrument.KR_STOCK, "krx")] = 100_000.0
    near_t = system.stock_futures_theory(Underlying.SAMSUNG)
    next_t = system.stock_futures_theory(Underlying.SAMSUNG, NEXT)
    assert near_t is not None and next_t is not None and next_t > near_t > 100_000
    assert system.futures_codes == {(Underlying.SAMSUNG, NEAR): "A1169000",
                                    (Underlying.SAMSUNG, NEXT): "A116C000"}
    board = system.disparity_board(1)
    assert (Underlying.SAMSUNG, NEXT) in board and (Underlying.SAMSUNG, NEAR) in board
    # 동시호가 원달러 대응주문: 차근 코드도 감시 대상 종목으로 풀린다.
    assert system._resolve_fut_code("A116C000") is Underlying.SAMSUNG
    assert system._resolve_fut_code("A9999999") is None


async def test_core_load_instruments_registers_next_month() -> None:
    system = _live_system()
    await system.load_instruments()
    info = system.instruments[(Underlying.SAMSUNG, NEXT)]
    assert info.code == "A116C000" and info.expiry == 202803 and info.multiplier == 10.0
    assert system.instruments[(Underlying.SAMSUNG, NEAR)].expiry == 202712


_ROWS = [
    {"hname": "삼성전자   F 202609", "shcode": "A1169000", "basecode": "A005930"},
    {"hname": "삼성전자   F 202612", "shcode": "A116C000", "basecode": "A005930"},
    {"hname": "삼성전자   F 202703", "shcode": "A116F000", "basecode": "A005930"},
    {"hname": "삼성전자 SP 202609", "shcode": "A116SP00", "basecode": "A005930"},  # 스프레드 제외
    {"hname": "SK하이닉스 F 202609", "shcode": "A5069000", "basecode": "A000660"},
]


def test_select_months_near_and_next() -> None:
    got = select_months(_ROWS, count=2, now=datetime(2026, 9, 3, 10, 0))
    assert got[Underlying.SAMSUNG] == [("A1169000", 202609), ("A116C000", 202612)]
    assert got[Underlying.SK_HYNIX] == [("A5069000", 202609)]  # 차근 없음 → 1개


def test_select_months_skips_rolled_contract() -> None:
    # 9월물 최종거래일(둘째 목요일 2026-09-10) 15:45 이후엔 9월물을 버리고 12월물이 근이 된다 —
    # 기존 select_near_month는 이 필터가 없어 만기일 저녁 재시동 시 만기월물을 잡았다(§5.11).
    got = select_months(_ROWS, count=2, now=datetime(2026, 9, 10, 16, 0))
    assert got[Underlying.SAMSUNG] == [("A116C000", 202612), ("A116F000", 202703)]
    assert Underlying.SK_HYNIX not in got  # 남은 월물 없음
