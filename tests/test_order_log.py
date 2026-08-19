"""거래소별 주문 로그 — HL/LS 로거 라우팅·부분/전량 판정 (파일은 core 설정이 붙임)."""
import logging

import pytest

from kp_arb import order_log
from kp_arb.domain.enums import Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent


def _hl(qty: float = 0.14) -> OrderIntent:
    return OrderIntent(venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
                       instrument=Instrument.HL_PERP, side=Side.BUY, qty=qty,
                       order_type=OrderType.LIMIT, price=163.45)


def _ls() -> OrderIntent:
    return OrderIntent(venue=Venue.LS, underlying=Underlying.SAMSUNG,
                       instrument=Instrument.KR_STOCK, side=Side.SELL, qty=10,
                       order_type=OrderType.MARKET)


def test_hl_order_goes_to_hl_logger(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="kp_arb.order.hl"):
        order_log.order_placed(_hl(), "513537756381", {"status": "ok"})
    rec = [r for r in caplog.records if r.name == "kp_arb.order.hl"]
    assert rec and "발주" in rec[-1].message and "513537756381" in rec[-1].message


def test_ls_order_goes_to_ls_logger(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="kp_arb.order.ls"):
        order_log.order_canceled(Venue.LS, "999")
    assert any(r.name == "kp_arb.order.ls" and "취소" in r.message for r in caplog.records)


def test_hl_and_ls_use_separate_loggers() -> None:
    # 거래소별 별도 파일로 갈리도록 로거 이름 자체가 분리돼야 한다.
    assert order_log.logger_for(Venue.HYPERLIQUID).name == "kp_arb.order.hl"
    assert order_log.logger_for(Venue.LS).name == "kp_arb.order.ls"


def test_ws_raw_routes_per_exchange(caplog: pytest.LogCaptureFixture) -> None:
    # WS 주문 원본은 거래소별 로거(ws_hl/ws_ls)로 갈려 각각 파일로 남아야 한다.
    with caplog.at_level(logging.INFO, logger="kp_arb.wsraw.hl"):
        order_log.ws_order_raw(Venue.HYPERLIQUID, '{"channel":"orderUpdates"}')
    with caplog.at_level(logging.INFO, logger="kp_arb.wsraw.ls"):
        order_log.ws_order_raw(Venue.LS, '{"header":{"tr_cd":"C01"}}')
    hl = [r for r in caplog.records if r.name == "kp_arb.wsraw.hl"]
    ls = [r for r in caplog.records if r.name == "kp_arb.wsraw.ls"]
    assert hl and "orderUpdates" in hl[-1].message
    assert ls and "C01" in ls[-1].message


def test_ws_raw_logger_names_separate() -> None:
    assert order_log.WS_HL_LOGGER == "kp_arb.wsraw.hl"
    assert order_log.WS_LS_LOGGER == "kp_arb.wsraw.ls"
    assert order_log.WS_HL_LOGGER != order_log.WS_LS_LOGGER


def test_fill_partial_then_full(caplog: pytest.LogCaptureFixture) -> None:
    # 0.14 주문에 0.054만 체결 → '부분', 누적/목표 함께 남겨 추적 가능.
    with caplog.at_level(logging.INFO, logger="kp_arb.order.hl"):
        order_log.order_filled(_hl(0.14), 0.054, 163.45, "F1", 0.054)
        order_log.order_filled(_hl(0.14), 0.086, 163.45, "F2", 0.14)
    msgs = [r.message for r in caplog.records if r.name == "kp_arb.order.hl"]
    assert "부분" in msgs[0] and "0.054/0.14" in msgs[0]
    assert "전량" in msgs[1] and "0.14/0.14" in msgs[1]
