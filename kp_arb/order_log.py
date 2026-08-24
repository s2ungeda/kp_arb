"""거래소별 주문 로그 — HL/LS의 발주·응답·체결·취소·정정·거부를 **거래소별 별도 파일**에 남긴다.

로거: ``kp_arb.order.hl`` / ``kp_arb.order.ls``. 자정 롤오버 파일 핸들러
(``hl_order_날짜.log`` / ``ls_order_날짜.log``)와 ``propagate=False`` 부착은
core_server._setup_logging이 한다 — 이 모듈은 '어느 로거로 무엇을' 남길지만 정한다
(단위 테스트는 caplog로 확인, 파일 불필요).

발주는 **거래소 원응답**까지 함께 남긴다 — HL은 즉시 체결/대기(filled/resting)·체결수량이
응답에 들어와, "주문 수량과 잔고가 다른" 상황(부분체결·상계)을 나중에 로그로 재구성할 수 있다.
"""
from __future__ import annotations

import logging
from typing import Any

from .domain.enums import Venue
from .domain.models import OrderIntent

HL_LOGGER = "kp_arb.order.hl"
LS_LOGGER = "kp_arb.order.ls"
WS_HL_LOGGER = "kp_arb.wsraw.hl"  # WS 주문 원본 — ws_hl_날짜.log
WS_LS_LOGGER = "kp_arb.wsraw.ls"  # WS 주문 원본 — ws_ls_날짜.log


def logger_for(venue: Venue) -> logging.Logger:
    """거래소별 주문 로거 — HL은 hl_order 파일, 그 외(LS)는 ls_order 파일."""
    return logging.getLogger(HL_LOGGER if venue is Venue.HYPERLIQUID else LS_LOGGER)


def ws_order_raw(venue: Venue, raw: str) -> None:
    """WS 주문 관련 원본 프레임을 거래소별 파일에 **가공 없이 그대로** 남긴다.

    체결·취소·주문상태 변화의 실제 수신 데이터를 사후 재구성하기 위한 것 — 파싱 전 원문.
    호가·마크 등 시세는 대상 아님(주문 관련만). 파일: ws_hl_날짜.log / ws_ls_날짜.log.
    """
    logging.getLogger(
        WS_HL_LOGGER if venue is Venue.HYPERLIQUID else WS_LS_LOGGER).info("%s", raw)


def _fmt(intent: OrderIntent, *, with_price: bool = True) -> str:
    s = (f"{intent.underlying.value} {intent.instrument.value} "
         f"{intent.side.value} {intent.qty:g}")
    if with_price and intent.price is not None:
        s += f" @ {intent.price}"
    return s + f"{' reduce' if intent.reduce_only else ''}{' post' if intent.post_only else ''}"


def order_placed(intent: OrderIntent, order_id: str, response: Any = None) -> None:
    """발주 성공(응답 수신) — 요청 요약 + 거래소 원응답(체결/대기·체결수량 판별용)."""
    logger_for(intent.venue).info(
        "발주 %s → #%s%s", _fmt(intent), order_id,
        f" | resp={response!r}" if response is not None else "")


def order_rejected(intent: OrderIntent, error: object) -> None:
    """발주 거부/오류 — 게이트웨이 예외(사유 포함)."""
    logger_for(intent.venue).warning("발주거부 %s — %s", _fmt(intent), error)


def order_canceled(venue: Venue, order_id: str) -> None:
    """취소 성공."""
    logger_for(venue).info("취소 #%s", order_id)


def order_amended(venue: Venue, order_id: str, new_id: str,
                  qty: float | None, price: float | None,
                  *, reduce_only: bool = False, post_only: bool = False) -> None:
    """정정(원주문 → 새 주문). 실제로 실어보낸 옵션(reduce/post)도 남긴다."""
    logger_for(venue).info(
        "정정 #%s → #%s qty=%s price=%s reduce=%s post=%s",
        order_id, new_id, qty, price, reduce_only, post_only)


def order_amend_rejected(venue: Venue, order_id: str, error: object,
                         *, qty: float | None = None, price: float | None = None,
                         reduce_only: bool = False, post_only: bool = False) -> None:
    """정정 거부/오류 — 게이트웨이 예외(사유 포함). 요청값·옵션(reduce/post) 함께 남긴다."""
    logger_for(venue).warning(
        "정정거부 #%s (요청 qty=%s price=%s reduce=%s post=%s) — %s",
        order_id, qty, price, reduce_only, post_only, error)


def order_filled(intent: OrderIntent, fill_qty: float, fill_price: float,
                 fill_id: str, cum_qty: float) -> None:
    """체결통보(WS) — 부분/전량 + 누적/목표 수량(주문이 어디로 갔는지 추적)."""
    kind = "전량" if cum_qty >= intent.qty - 1e-6 else "부분"  # 부동소수점 톨러런스
    logger_for(intent.venue).info(
        "체결(%s) %s %g @ %s [체결#%s] 누적 %g/%g",
        kind, _fmt(intent, with_price=False), fill_qty, fill_price,
        fill_id, cum_qty, intent.qty)
