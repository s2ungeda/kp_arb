"""RiskManager 계약 테스트. 순수 로직(주입형 상태/한도)."""
from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent
from kp_arb.risk import RiskDecision, RiskLimits, RiskManager, RiskState

SAMSUNG = Underlying.SAMSUNG


def ls_intent(*, qty: float = 10, price: float = 70_000) -> OrderIntent:
    return OrderIntent(
        venue=Venue.LS,
        underlying=SAMSUNG,
        instrument=Instrument.KR_STOCK,
        side=Side.BUY,
        qty=qty,
        order_type=OrderType.LIMIT,
        price=price,
    )


def hl_intent() -> OrderIntent:
    return OrderIntent(
        venue=Venue.HYPERLIQUID,
        underlying=SAMSUNG,
        instrument=Instrument.HL_PERP,
        side=Side.SELL,
        qty=1,
        order_type=OrderType.LIMIT,
        price=52.0,
    )


def _limits() -> RiskLimits:
    return RiskLimits(hl_margin_floor=0.05, account_buffer={Account.KR_STOCK: 1_000_000})


def _ok_state() -> RiskState:
    return RiskState(
        reference_available={SAMSUNG: True},
        account_available_funds={Account.KR_STOCK: 2_000_000},
        hl_margin_ratio=0.2,
    )


def test_allows_when_all_ok() -> None:
    rm = RiskManager(_limits())
    assert rm.check(ls_intent(), _ok_state()) == RiskDecision(True)
    assert rm.allow(hl_intent(), _ok_state()) is True


def test_deadzone_blocks_new_entry() -> None:
    rm = RiskManager(_limits())
    state = RiskState(reference_available={SAMSUNG: False},
                      account_available_funds={Account.KR_STOCK: 2_000_000})
    decision = rm.check(ls_intent(), state)
    assert decision.allowed is False
    assert "deadzone" in (decision.reason or "")


def test_missing_reference_defaults_blocked() -> None:
    # reference_available에 종목 자체가 없으면 보수적으로 거부.
    rm = RiskManager(_limits())
    assert rm.allow(ls_intent(), RiskState()) is False


def test_hl_margin_floor_violation_blocks() -> None:
    rm = RiskManager(_limits())
    state = RiskState(reference_available={SAMSUNG: True}, hl_margin_ratio=0.03)
    decision = rm.check(hl_intent(), state)
    assert decision.allowed is False
    assert "margin" in (decision.reason or "")


def test_hl_margin_unknown_blocks() -> None:
    rm = RiskManager(_limits())
    state = RiskState(reference_available={SAMSUNG: True}, hl_margin_ratio=None)
    assert rm.allow(hl_intent(), state) is False


def test_ls_order_not_gated_by_hl_margin() -> None:
    # LS 주문은 HL 마진비율(None)과 무관하게 통과(레퍼런스·버퍼 OK면).
    rm = RiskManager(_limits())
    state = RiskState(reference_available={SAMSUNG: True},
                      account_available_funds={Account.KR_STOCK: 2_000_000},
                      hl_margin_ratio=None)
    assert rm.allow(ls_intent(), state) is True


def test_account_buffer_breach_blocks() -> None:
    rm = RiskManager(_limits())
    # 가용 1.5M - 비용 0.7M = 0.8M < 버퍼 1.0M → 거부.
    state = RiskState(reference_available={SAMSUNG: True},
                      account_available_funds={Account.KR_STOCK: 1_500_000})
    decision = rm.check(ls_intent(), state)
    assert decision.allowed is False
    assert "buffer" in (decision.reason or "")


def test_kill_switch_blocks_all() -> None:
    rm = RiskManager(_limits())
    state = RiskState(reference_available={SAMSUNG: True},
                      account_available_funds={Account.KR_STOCK: 9_999_999},
                      hl_margin_ratio=0.9, kill_switch=True)
    assert rm.allow(ls_intent(), state) is False
    assert rm.allow(hl_intent(), state) is False


def test_filter_keeps_only_allowed() -> None:
    rm = RiskManager(_limits())
    # 레퍼런스 OK지만 버퍼 부족 → LS 거부, HL은 마진 OK → 통과.
    state = RiskState(reference_available={SAMSUNG: True},
                      account_available_funds={Account.KR_STOCK: 1_500_000},
                      hl_margin_ratio=0.2)
    kept = rm.filter([ls_intent(), hl_intent()], state)
    assert kept == [hl_intent()]
