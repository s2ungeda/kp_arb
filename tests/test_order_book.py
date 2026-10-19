"""OrderBook 계약 테스트 — 스냅샷 1회 + 이후 이벤트로만 상태·포지션·잔고 갱신."""
import logging

import pytest

from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent, Position
from kp_arb.gateways.ls_ws import Fill
from kp_arb.order_book import OrderBook, OrderStatus, TrackedOrder

SAMSUNG = Underlying.SAMSUNG


def intent(side: Side = Side.BUY, qty: float = 10,
           instrument: Instrument = Instrument.KR_STOCK) -> OrderIntent:
    return OrderIntent(venue=Venue.LS, underlying=SAMSUNG, instrument=instrument,
                       side=side, qty=qty, order_type=OrderType.MARKET)


def fill(order_id: str, qty: float, price: float, *, fee: float = 0.0,
         fill_id: str = "F1") -> Fill:
    return Fill(fill_id=fill_id, order_id=order_id, qty=qty, price=price, fee=fee, ts=1.0)


# --- 스냅샷 (최초 실행/온디맨드) ---


def test_snapshot_initializes_state() -> None:
    ob = OrderBook()
    ob.load_snapshot(
        positions=[Position(venue=Venue.LS, instrument=Instrument.KR_STOCK,
                            underlying=SAMSUNG, side=Side.BUY, qty=100, avg_price=290_000,
                            account=Account.KR_STOCK)],
        balances={Account.KR_STOCK: 5_000_000},
        open_orders=[TrackedOrder("100", intent(qty=5), status=OrderStatus.ACCEPTED)],
    )
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 100
    assert ob.balance(Account.KR_STOCK) == 5_000_000
    assert [o.order_id for o in ob.open_orders()] == ["100"]


def test_snapshot_removes_stale_phantom_keeps_recent() -> None:
    # HL은 취소·체결 통보가 없어, '적' 재조회 시 스냅샷에 사라진 미체결(phantom)을 정리한다.
    # 단 방금 낸 주문은 아직 거래소 미체결조회에 안 뜰 수 있어 유예 내면 보존.
    ob = OrderBook()
    stale = ob.track("OLD", intent())
    stale.placed_ts = 0.0  # 아주 오래됨(monotonic 기준 유예 밖) → phantom
    ob.track("NEW", intent())  # 방금(placed_ts=now) → 유예 안
    ob.load_snapshot(open_orders=())  # 거래소엔 아무 미체결도 없음
    assert ob.order("OLD") is None       # 오래된 phantom 정리
    assert ob.order("NEW") is not None    # 방금 낸 것은 보존


def test_snapshot_merges_into_tracked_order_keeping_defenses() -> None:
    # 검토 2026-10-13 ★2: 재동기가 추적 객체를 통째로 바꿔 끼우면 seen_fill_ids(중복 방어)·
    # cancelled_qty가 사라진다 → 병합. ① 같은 체결번호 재통보는 재동기 뒤에도 무시 ② 스냅샷 체결이
    # 더 많으면 차이를
    # 훅으로 알림(resync-번호) ③ 로컬이 더 많으면 로컬 유지 ④ 로컬이 닫은 주문은 되돌리지 않음.
    ob = OrderBook()
    applied: list[tuple[float, str]] = []
    ob.on_fill_applied.append(lambda o, q, p, f: applied.append((q, f)))
    it = OrderIntent(venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
                     side=Side.SELL, qty=10, order_type=OrderType.LIMIT, price=300_000.0,
                     account=Account.KR_DERIV, source="autom", tag="선정1진")
    local = ob.track("O1", it)
    ob.on_fill(fill("O1", 2, 300_000.0, fill_id="C-1"))
    ob.load_snapshot(open_orders=[TrackedOrder("O1", it, status=OrderStatus.PARTIAL,
                                               filled_qty=2)])
    assert ob.order("O1") is local                                     # ① 객체 유지
    ob.on_fill(fill("O1", 2, 300_000.0, fill_id="C-1"))                # 재통보 → 여전히 무시
    assert local.filled_qty == 2 and applied == [(2, "C-1")]
    ob.load_snapshot(open_orders=[TrackedOrder("O1", it, status=OrderStatus.PARTIAL,
                                               filled_qty=5, avg_fill_price=300_000.0)])
    assert local.filled_qty == 5 and applied[-1] == (3, "resync-O1")  # ② 끊긴 사이 체결 3
    assert local.status is OrderStatus.PARTIAL
    ob.load_snapshot(open_orders=[TrackedOrder("O1", it, status=OrderStatus.PARTIAL,
                                               filled_qty=4)])
    assert local.filled_qty == 5 and len(applied) == 2                 # ③ 로컬 유지
    ob.on_cancel("O1", qty=5)                                          # 남은 5 취소 확정 → 닫힘
    ob.load_snapshot(open_orders=[TrackedOrder("O1", it, status=OrderStatus.PARTIAL,
                                               filled_qty=5)])
    assert local.status is OrderStatus.CANCELLED and ob.open_orders() == []  # ④ 안 되돌림


def test_fill_beyond_remaining_is_applied_with_error(caplog: pytest.LogCaptureFixture) -> None:
    # 검토 2026-10-13 F: 셈을 넘는 체결은 장부가 틀렸다는 뜻 — 자르지 않고 반영(실제 돈) + 에러 로그
    ob = OrderBook()
    ob.track("O1", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=3, order_type=OrderType.LIMIT, price=300_000.0,
        account=Account.KR_DERIV))
    ob.on_cancel("O1", qty=2)                     # 남은 1
    with caplog.at_level(logging.ERROR, logger="kp_arb.order"):
        ob.on_fill(fill("O1", 2, 300_000.0, fill_id="C-1"))  # 2 체결 — 1 초과
    o = ob.order("O1")
    assert o.filled_qty == 2 and o.status is OrderStatus.CANCELLED
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK_FUTURE, Account.KR_DERIV) == -2
    assert any("체결 2가 장부 남은 수량 1를 넘음" in r.getMessage() for r in caplog.records)


def test_snapshot_keeps_exchange_orders() -> None:
    # 스냅샷(거래소 실측)에 있으면 나이와 무관하게 유지.
    ob = OrderBook()
    stale = ob.track("EX", intent())
    stale.placed_ts = 0.0
    ob.load_snapshot(open_orders=[TrackedOrder("EX", intent(), status=OrderStatus.ACCEPTED)])
    assert ob.order("EX") is not None


def test_snapshot_preserves_orders_of_unreconciled_account() -> None:
    # 한 계좌 조회 실패(reconcile 목록에서 빠짐) → 그 계좌의 살아있는 미체결은 보존.
    ob = OrderBook()
    s = ob.track("S1", intent(instrument=Instrument.KR_STOCK))
    s.placed_ts = 0.0  # 오래됨(유예 밖)
    d = ob.track("D1", intent(instrument=Instrument.KR_STOCK_FUTURE))
    d.placed_ts = 0.0
    # KR_STOCK만 조회 성공(빈 미체결) — KR_DERIV는 실패라 정리 대상 아님.
    ob.load_snapshot(open_orders=(), reconcile_accounts={Account.KR_STOCK})
    assert ob.order("S1") is None      # 조회 성공 계좌 → phantom 정리
    assert ob.order("D1") is not None   # 조회 실패 계좌 → 보존


def test_replace_positions_only_touches_instrument() -> None:
    # 체결 후 HL 포지션만 실측으로 교체 — LS·잔고·주문은 유지, 목록에 없는 HL 종목은 청산.
    ob = OrderBook()
    ob.load_snapshot(positions=[
        Position(venue=Venue.LS, instrument=Instrument.KR_STOCK, underlying=SAMSUNG,
                 side=Side.BUY, qty=100, avg_price=70_000, account=Account.KR_STOCK),
        Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP, underlying=SAMSUNG,
                 side=Side.BUY, qty=0.1, avg_price=163.0),
    ])
    ob.replace_positions([
        Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                 underlying=Underlying.SK_HYNIX, side=Side.SELL, qty=0.05, avg_price=1400.0),
    ], instrument=Instrument.HL_PERP)
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 0            # 청산(목록 없음)
    assert ob.position_qty(Underlying.SK_HYNIX, Instrument.HL_PERP, None) == -0.05  # 신규
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 100   # LS 유지


def test_on_fill_ignores_duplicate_after_full_fill() -> None:
    # 발주 즉시체결을 반영해 전량 체결된 뒤, userFills 중복 통보가 와도 초과체결 안 된다.
    ob = OrderBook()
    ob.track("O1", OrderIntent(
        venue=Venue.HYPERLIQUID, underlying=SAMSUNG, instrument=Instrument.HL_PERP,
        side=Side.SELL, qty=0.1, order_type=OrderType.LIMIT, price=165.0))
    ob.on_fill(fill("O1", 0.1, 168.0))  # 전량 체결
    assert ob.order("O1").status is OrderStatus.FILLED
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == -0.1
    ob.on_fill(fill("O1", 0.1, 168.0, fill_id="F2"))  # 중복(같은 체결 재통보) → 무시
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == -0.1  # 초과 안 됨


def test_on_fill_same_fill_id_applied_once_on_partial_fill(
        caplog: pytest.LogCaptureFixture) -> None:
    # 운영 사고 2026-10-12: 같은 체결 통보가 코어 안에서 두 번 처리돼(훅 이중 등록) 부분체결이
    # 이중 반영 → 유령 체결로 HL 약 80계약 초과 헤지. 장부는 "같은 주문의 같은 체결번호는 한 번만"
    # 반영하고, 거를 때는 숨기지 않고 경고 로그를 남긴다. 체결번호가 다르면 정상 반영.
    ob = OrderBook()
    ob.track("O1", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=10, order_type=OrderType.LIMIT, price=300_000.0,
        account=Account.KR_DERIV))
    ob.on_fill(fill("O1", 3, 300_000.0, fill_id="C-1"))
    with caplog.at_level(logging.WARNING, logger="kp_arb.order"):
        ob.on_fill(fill("O1", 3, 300_000.0, fill_id="C-1"))  # 같은 통보 두 번째 → 무시
    assert ob.order("O1").filled_qty == 3
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK_FUTURE, Account.KR_DERIV) == -3
    assert any("중복 체결 통보 무시 #O1 체결번호 C-1" in r.getMessage() for r in caplog.records)
    ob.on_fill(fill("O1", 2, 300_000.0, fill_id="C-2"))     # 다른 체결번호 → 반영
    assert ob.order("O1").filled_qty == 5


def test_cancel_closes_by_confirmed_qty_and_waits_for_pending_fills(
        caplog: pytest.LogCaptureFixture) -> None:
    # 결정 69(사용자 2026-10-13): "남은 수량이 있으면 기다리고 없으면 상태 확정" — 취소 확정 통보의
    # 수량(H01 qty·SC3 canccnfqty·HL sz)과 장부 남은 수량을 대조해 닫는다. 운영 10-12 #1518: 3계약
    # 중 1 체결, 장부는 2로 오인 → 취소 1 확정 → 실제 1계약이 살아 2초 뒤 체결. 옛 코드는 취소
    # 통보에 바로 닫고 120초 타이머로 늦은 체결을 받았다(타이머 폐기).
    ob = OrderBook()
    ob.track("O1", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=3, order_type=OrderType.LIMIT, price=300_000.0,
        account=Account.KR_DERIV))
    ob.on_fill(fill("O1", 1, 300_000.0, fill_id="C-1"))
    with caplog.at_level(logging.WARNING, logger="kp_arb.order"):
        ob.on_cancel("O1", qty=1)                      # 확정 1 < 남은 2 → 체결 1 대기
    o = ob.order("O1")
    assert o.status is OrderStatus.PARTIAL and o.is_open and o.cancelled_qty == 1
    assert o.remaining_qty == 1 and ob.open_orders() == [o]
    assert any("부분 취소 확정 #O1: 확정 1 < 남은 2" in r.getMessage() for r in caplog.records)
    ob.on_fill(fill("O1", 1, 300_000.0, fill_id="C-2"))  # 기다리던 체결 → 셈 끝 → 닫힘
    assert o.status is OrderStatus.CANCELLED and o.filled_qty == 2 and ob.open_orders() == []
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK_FUTURE, Account.KR_DERIV) == -2
    # 확정 = 남은 수량 → 바로 닫힘 / None(REST 응답으로 닫는 HL 취소) = 남은 전부
    ob.track("O2", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=3, order_type=OrderType.LIMIT, price=300_000.0,
        account=Account.KR_DERIV))
    ob.on_cancel("O2", qty=3)
    assert ob.order("O2").status is OrderStatus.CANCELLED
    ob.track("O3", _hl_intent(Side.BUY, 1.0))
    ob.on_cancel("O3")
    assert ob.order("O3").status is OrderStatus.CANCELLED


def test_snapshot_keeps_partially_cancelled_order_waiting_for_fill() -> None:
    # 검토 2026-10-13 A(결정 69가 연 구멍): 부분 취소 확정 뒤 체결 통보를 기다리는 주문은 거래소
    # 미체결 목록에 없는 게 정상 — 재동기 유령 정리에서 제외. 지우면 뒤따르는 체결이 미아가 된다.
    import time as _t

    ob = OrderBook()
    ob.track("O1", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=10, order_type=OrderType.LIMIT, price=300_000.0,
        account=Account.KR_DERIV))
    ob.order("O1").placed_ts = _t.monotonic() - 1000.0           # 유예(15초) 한참 지남
    ob.on_cancel("O1", qty=7)                                   # 확정 7 < 남은 10 → 체결 3 대기
    ob.load_snapshot(open_orders=[], reconcile_accounts={Account.KR_DERIV},
                     scope={Account.KR_DERIV})
    o = ob.order("O1")
    assert o is not None and o.is_open and o.remaining_qty == 3  # 유령 정리에서 제외
    ob.on_fill(fill("O1", 3, 300_000.0, fill_id="C-1"))         # 기다리던 체결 → 닫힘
    assert o.status is OrderStatus.CANCELLED and ob.open_orders() == []
    # 셈이 끝난(취소로 닫힌) 주문은 종전대로 — 열린 게 아니라 정리 대상도 아님
    ob.track("O2", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=10, order_type=OrderType.LIMIT, price=300_000.0,
        account=Account.KR_DERIV))
    ob.order("O2").placed_ts = _t.monotonic() - 1000.0
    ob.load_snapshot(open_orders=[], reconcile_accounts={Account.KR_DERIV},
                     scope={Account.KR_DERIV})
    assert ob.order("O2") is None                                # 보통 유령은 정리


def test_cancel_confirmed_more_than_remaining_closes_with_error(
        caplog: pytest.LogCaptureFixture) -> None:
    # 확정 수량 > 장부 남은 수량 = 장부가 체결을 더 셌다(이중 반영류) → 닫고 에러 로그(결정 69)
    ob = OrderBook()
    ob.track("O1", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=3, order_type=OrderType.LIMIT, price=300_000.0,
        account=Account.KR_DERIV))
    ob.on_fill(fill("O1", 2, 300_000.0, fill_id="C-1"))
    with caplog.at_level(logging.ERROR, logger="kp_arb.order"):
        ob.on_cancel("O1", qty=2)                      # 남은 1인데 2 확정
    assert ob.order("O1").status is OrderStatus.CANCELLED
    assert any("취소 확정 수량 2 > 장부 남은 수량 1 #O1" in r.getMessage() for r in caplog.records)


def test_fill_after_closed_order_keeps_status_but_applies_money(
        caplog: pytest.LogCaptureFixture) -> None:
    # 검토 2026-10-13 ★1: 취소·거부로 닫은 주문에 체결이 오면(셈이 맞으면 올 일 없음) 상태는
    # 되살리지 않고(유령 미체결 방지) 수량·포지션은 반영(실제 돈) + 에러 로그.
    ob = OrderBook()
    ob.track("O1", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=3, order_type=OrderType.LIMIT, price=300_000.0,
        account=Account.KR_DERIV))
    ob.on_cancel("O1", qty=3)
    applied: list[float] = []
    ob.on_fill_applied.append(lambda o, q, p, f: applied.append(q))
    with caplog.at_level(logging.ERROR, logger="kp_arb.order"):
        ob.on_fill(fill("O1", 1, 300_000.0, fill_id="C-9"))
    o = ob.order("O1")
    assert o.status is OrderStatus.CANCELLED and not o.is_open and ob.open_orders() == []
    assert o.filled_qty == 1 and applied == [1]
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK_FUTURE, Account.KR_DERIV) == -1
    assert any("종료(cancelled)된 주문 #O1에 체결 통보" in r.getMessage() for r in caplog.records)


def test_fill_after_assumed_full_cancel_corrects_cancelled_qty(
        caplog: pytest.LogCaptureFixture) -> None:
    # HL(사용자 2026-10-13: LS처럼 셈하지 않음): 수량 없는 취소(응답·통보)는 남은 전부로 닫고,
    # 취소 직전 체결의 늦은 통보는 정상 경합 — 취소 수량을 그만큼 줄이고 수량·포지션 반영, 상태는
    # 그대로, 에러 아님.
    ob = OrderBook()
    ob.track("H1", _hl_intent(Side.SELL, 1.0))
    ob.on_fill(fill("H1", 0.3, 100.0, fill_id="t1"))
    ob.on_cancel("H1")                                  # 응답/통보 — 남은 0.7 취소로 가정
    o = ob.order("H1")
    assert o.status is OrderStatus.CANCELLED and o.cancelled_qty == 0.7 and o.cancel_assumed
    with caplog.at_level(logging.WARNING, logger="kp_arb.order"):
        ob.on_fill(fill("H1", 0.2, 100.0, fill_id="t2"))   # 취소 직전 체결, 통보가 늦음
    assert o.status is OrderStatus.CANCELLED and o.filled_qty == 0.5
    assert abs(o.cancelled_qty - 0.5) < 1e-9 and ob.open_orders() == []
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == -0.5
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any("취소 직전 체결(정상 경합)" in r.getMessage() for r in caplog.records)


def test_cancel_before_track_replays_confirmed_qty() -> None:
    # 취소 확정이 track보다 먼저 와도 확정 수량이 보관됐다가 replay에 쓰인다(결정 69)
    ob = OrderBook()
    ob.on_cancel("O1", qty=1)
    ob.track("O1", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=3, order_type=OrderType.LIMIT, price=300_000.0,
        account=Account.KR_DERIV))
    ob.replay_pending("O1")
    o = ob.order("O1")
    assert o.is_open and o.cancelled_qty == 1 and o.remaining_qty == 2


def test_same_order_number_in_two_ls_accounts_is_kept_apart() -> None:
    # 결정 73(사용자 2026-10-14, 운영 사고 같은 날): LS 주문번호는 계좌마다 따로 매겨져 주식 #4545와
    # 선물 #4545가 공존한다. 장부 키는 (계좌, 번호) — 주식 통보(계좌 KR_STOCK)가 선물 주문에
    # 들어가지
    # 않고, 같은 번호 둘 다 추적된다. 계좌 없이 조회하면 번호가 한 계좌뿐일 때만 찾는다.
    ob = OrderBook()
    stock = ob.track("4545", OrderIntent(
        venue=Venue.LS, underlying=Underlying.SK_HYNIX, instrument=Instrument.KR_STOCK,
        side=Side.BUY, qty=100, order_type=OrderType.LIMIT, price=1_464_000.0,
        account=Account.KR_STOCK))
    fut = ob.track("4545", OrderIntent(
        venue=Venue.LS, underlying=Underlying.SK_HYNIX, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.BUY, qty=1, order_type=OrderType.LIMIT, price=1_475_000.0,
        account=Account.KR_DERIV))
    assert ob.order("4545", Account.KR_STOCK) is stock
    assert ob.order("4545", Account.KR_DERIV) is fut
    assert ob.order("4545") is None                                 # 둘 다 열림 — 계좌 없인 못 가림
    ob.on_fill(Fill(fill_id="s1", order_id="4545", qty=8, price=1_464_000.0, ts=0,
                    account=Account.KR_STOCK))                      # 주식 8주 체결(10-14 실제 통보)
    assert stock.filled_qty == 8 and fut.filled_qty == 0            # 선물 주문은 그대로
    ob.on_cancel("4545", qty=92, account=Account.KR_STOCK)          # 주식 남은 92 취소
    assert stock.status is OrderStatus.CANCELLED and fut.is_open
    assert ob.order("4545") is fut                                  # 열린 것이 하나면 그것
    assert ob.position_qty(Underlying.SK_HYNIX, Instrument.KR_STOCK_FUTURE,
                           Account.KR_DERIV) == 0
    assert ob.position_qty(Underlying.SK_HYNIX, Instrument.KR_STOCK, Account.KR_STOCK) == 8


def test_pending_events_are_kept_per_account() -> None:
    # 운영 사고 2026-10-14 재현: 08:23 주식 HTS #4508 취소 100주·#4545 체결 8주가 미추적으로
    # 보관됐다가
    # 09:06 선물 자동M이 같은 번호로 선주문을 내자 그 주문에 재생됐다(취소 100 > 남은 1, 체결 8 →
    # HL 80
    # 초과 헤지). 이제 보관도 (계좌, 번호)라 선물 주문 등록 때 주식 통보는 재생되지 않는다.
    ob = OrderBook()
    ob.on_fill(Fill(fill_id="s1", order_id="4545", qty=8, price=1_464_000.0, ts=0,
                    account=Account.KR_STOCK))
    ob.on_cancel("4508", qty=100, account=Account.KR_STOCK)
    fut = ob.track("4545", OrderIntent(
        venue=Venue.LS, underlying=Underlying.SK_HYNIX, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.BUY, qty=1, order_type=OrderType.LIMIT, price=1_475_000.0,
        account=Account.KR_DERIV))
    ob.replay_pending("4545")
    assert fut.filled_qty == 0 and fut.is_open
    fut2 = ob.track("4508", OrderIntent(
        venue=Venue.LS, underlying=Underlying.SK_HYNIX, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.BUY, qty=1, order_type=OrderType.LIMIT, price=1_474_000.0,
        account=Account.KR_DERIV))
    ob.replay_pending("4508")
    assert fut2.is_open and fut2.cancelled_qty == 0
    # 뒤늦게 주식 쪽 주문이 등록되면 보관분은 그 주문에 들어간다
    stock = ob.track("4545", OrderIntent(
        venue=Venue.LS, underlying=Underlying.SK_HYNIX, instrument=Instrument.KR_STOCK,
        side=Side.BUY, qty=100, order_type=OrderType.LIMIT, price=1_464_000.0,
        account=Account.KR_STOCK))
    ob.replay_pending("4545", Account.KR_STOCK)
    assert stock.filled_qty == 8 and fut.filled_qty == 0


def _hl_intent(side: Side, qty: float) -> OrderIntent:
    return OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                       instrument=Instrument.HL_PERP, side=side, qty=qty,
                       order_type=OrderType.LIMIT, price=100.0)


def test_place_fill_absorbs_duplicate_userfill_partial() -> None:
    # 부분 즉시체결: 발주응답 3/5 선반영 → 같은 3을 userFills가 재통보해도 이중 반영 안 됨.
    ob = OrderBook()
    ob.track("O1", _hl_intent(Side.BUY, 5))
    ob.apply_place_fill(fill("O1", 3, 100.0, fill_id="place-O1"))  # 발주응답 선반영
    assert ob.order("O1").filled_qty == 3
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 3
    assert ob.order("O1").status is OrderStatus.PARTIAL

    ob.on_fill(fill("O1", 3, 100.0, fill_id="hl1"))  # 같은 체결 userFills 재통보 → 흡수
    assert ob.order("O1").filled_qty == 3                            # 6 아님
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 3   # 이중 반영 안 됨
    assert ob.order("O1").status is OrderStatus.PARTIAL

    ob.on_fill(fill("O1", 2, 101.0, fill_id="hl2"))  # 잔여 2 진짜 체결
    assert ob.order("O1").filled_qty == 5
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 5
    assert ob.order("O1").status is OrderStatus.FILLED


def test_place_fill_absorb_across_split_userfills() -> None:
    # 선반영 3을 userFills가 2+1로 쪼개 재통보 → 각각 흡수(총 3), 이중 반영 없음.
    ob = OrderBook()
    ob.track("O1", _hl_intent(Side.BUY, 5))
    ob.apply_place_fill(fill("O1", 3, 100.0, fill_id="place-O1"))
    ob.on_fill(fill("O1", 2, 100.0, fill_id="hl1"))  # 2 흡수(잔여 선반영 1)
    ob.on_fill(fill("O1", 1, 100.0, fill_id="hl2"))  # 1 흡수(선반영 0)
    assert ob.order("O1").filled_qty == 3
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 3


def test_place_fill_absorb_partial_when_userfill_batches_more() -> None:
    # userFills 한 건이 즉시체결분(선반영 3)+잔여 1 = 4를 묶어 오면: 3 흡수, 1만 신규 반영.
    ob = OrderBook()
    ob.track("O1", _hl_intent(Side.BUY, 5))
    ob.apply_place_fill(fill("O1", 3, 100.0, fill_id="place-O1"))
    ob.on_fill(fill("O1", 4, 100.0, fill_id="hl1"))
    assert ob.order("O1").filled_qty == 4
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP, None) == 4


# --- 상태 전이 (이벤트로만) ---


def test_lifecycle_ack_partial_filled() -> None:
    ob = OrderBook()
    ob.track("1", intent(qty=10))
    assert ob.order("1").status is OrderStatus.NEW

    ob.on_ack("1")
    assert ob.order("1").status is OrderStatus.ACCEPTED

    ob.on_fill(fill("1", qty=4, price=290_000))
    o = ob.order("1")
    assert o.status is OrderStatus.PARTIAL
    assert o.filled_qty == 4 and o.remaining_qty == 6

    ob.on_fill(fill("1", qty=6, price=291_000, fill_id="F2"))
    o = ob.order("1")
    assert o.status is OrderStatus.FILLED
    assert o.filled_qty == 10
    assert o.avg_fill_price == (4 * 290_000 + 6 * 291_000) / 10
    assert ob.open_orders() == []


def test_cancel_and_reject() -> None:
    ob = OrderBook()
    ob.track("1", intent())
    ob.track("2", intent())
    ob.on_cancel("1")
    ob.on_reject("2")
    assert ob.order("1").status is OrderStatus.CANCELLED
    assert ob.order("2").status is OrderStatus.REJECTED
    assert ob.open_orders() == []


def test_ws_order_event_dispatch() -> None:
    # WS OrderEvent(SC0 접수/SC3 취소)가 상태 전이로 연결됨 — 취소 통보는 원주문(orgordno) 대상.
    from kp_arb.gateways.ls_ws import OrderEvent

    ob = OrderBook()
    ob.track("9852", intent())
    ob.on_order_event(OrderEvent(kind="ack", order_id="9852"))
    assert ob.order("9852").status is OrderStatus.ACCEPTED

    ob.on_order_event(OrderEvent(kind="cancel", order_id="9901", org_order_id="9852"))
    assert ob.order("9852").status is OrderStatus.CANCELLED


def test_unknown_order_fill_ignored() -> None:
    ob = OrderBook()
    assert ob.on_fill(fill("999", qty=1, price=100)) is None
    assert ob.positions() == []


# --- 포지션 증분 (체결 즉시) ---


def test_fill_creates_and_averages_position() -> None:
    ob = OrderBook()
    ob.track("1", intent(qty=10))
    ob.on_fill(fill("1", qty=10, price=290_000))
    ob.track("2", intent(qty=10))
    ob.on_fill(fill("2", qty=10, price=292_000))

    positions = ob.positions()
    assert len(positions) == 1
    assert positions[0].qty == 20
    assert positions[0].avg_price == 291_000  # 가중평균
    assert positions[0].side is Side.BUY


def test_sell_fill_reduces_then_closes() -> None:
    ob = OrderBook()
    ob.track("1", intent(qty=10))
    ob.on_fill(fill("1", qty=10, price=290_000))

    ob.track("2", intent(side=Side.SELL, qty=4))
    ob.on_fill(fill("2", qty=4, price=295_000))
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 6
    assert ob.positions()[0].avg_price == 290_000  # 감소는 평단 유지

    ob.track("3", intent(side=Side.SELL, qty=6))
    ob.on_fill(fill("3", qty=6, price=295_000))
    assert ob.positions() == []  # 청산


def test_fill_reverses_direction() -> None:
    # 선물: 롱 2 → 매도 5 체결 → 숏 3(평단=체결가).
    ob = OrderBook()
    ob.track("1", intent(qty=2, instrument=Instrument.KR_STOCK_FUTURE))
    ob.on_fill(fill("1", qty=2, price=71_000))
    ob.track("2", intent(side=Side.SELL, qty=5, instrument=Instrument.KR_STOCK_FUTURE))
    ob.on_fill(fill("2", qty=5, price=72_000))

    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK_FUTURE, Account.KR_DERIV) == -3
    pos = ob.positions()[0]
    assert pos.side is Side.SELL and pos.qty == 3
    assert pos.avg_price == 72_000


# --- 잔고 증분 (체결 즉시) ---


def test_balance_updates_on_fill() -> None:
    ob = OrderBook()
    ob.load_snapshot(balances={Account.KR_STOCK: 1_000_000})
    ob.track("1", intent(qty=2))
    ob.on_fill(fill("1", qty=2, price=100_000, fee=100))
    assert ob.balance(Account.KR_STOCK) == 1_000_000 - 200_000 - 100  # 매수: 차감+수수료

    ob.track("2", intent(side=Side.SELL, qty=1))
    ob.on_fill(fill("2", qty=1, price=110_000))
    assert ob.balance(Account.KR_STOCK) == 799_900 + 110_000  # 매도: 가산


def test_hl_fill_skips_balance() -> None:
    # HL(계좌 없음)은 KR 현금 잔고 증분 대상 아님(마진 모델) — 포지션만.
    ob = OrderBook()
    oi = OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                     instrument=Instrument.HL_PERP, side=Side.SELL, qty=1,
                     order_type=OrderType.MARKET)
    ob.track("1", oi)
    ob.on_fill(fill("1", qty=1, price=52.0))
    assert ob.position_qty(SAMSUNG, Instrument.HL_PERP) == -1
    assert ob.balance(Account.KR_STOCK) == 0.0


# --- 주문 역전(reordering) 대비: 미아 이벤트 버퍼 + replay (LS·HL 공용) ---

def test_fill_before_track_is_buffered_then_applied() -> None:
    # 체결이 track보다 먼저 와도 track 시 replay로 반영(taker 즉시체결 누락 방지).
    ob = OrderBook()
    assert ob.on_fill(fill("O1", 10, 100.0)) is None   # 미아 → 버퍼
    ob.track("O1", intent(qty=10))
    ob.replay_pending("O1")
    order = ob.order("O1")
    assert order is not None and order.filled_qty == 10
    assert order.status is OrderStatus.FILLED


def test_cancel_before_track_is_buffered_then_applied() -> None:
    ob = OrderBook()
    ob.on_cancel("O2")                                  # 취소가 먼저 → 버퍼
    ob.track("O2", intent())
    ob.replay_pending("O2")
    order = ob.order("O2")
    assert order is not None and order.status is OrderStatus.CANCELLED


def test_ack_before_track_is_buffered_then_applied() -> None:
    ob = OrderBook()
    ob.on_ack("O3")                                     # 접수가 먼저 → 버퍼
    ob.track("O3", intent())
    ob.replay_pending("O3")
    order = ob.order("O3")
    assert order is not None and order.status is OrderStatus.ACCEPTED


def test_replay_absorbs_duplicate_with_provisional() -> None:
    # 즉시체결(apply_place_fill) 뒤 replay되는 같은 체결은 provisional로 흡수 — 이중 반영 없음.
    ob = OrderBook()
    ob.on_fill(fill("O4", 0.1, 100.0))                  # WS 체결이 먼저 → 버퍼
    ob.track("O4", _hl_intent(Side.SELL, 0.1))
    ob.apply_place_fill(fill("O4", 0.1, 100.0, fill_id="place-O4"))  # 응답 즉시체결
    ob.replay_pending("O4")                             # 버퍼된 WS 체결 → 흡수
    order = ob.order("O4")
    assert order is not None and order.filled_qty == 0.1   # 0.2 아님(이중 없음)


def test_replay_float_residue_is_not_a_fill() -> None:
    # 실측 2026-09-11: 선반영 5.963 vs WS 체결 0.521+5.208+0.234 — 부동소수점 찌꺼기 2.8e-17이
    # '체결 2.77556e-17'로 반영돼 체결내역에 0짜리 행이 생겼다. 찌꺼기는 체결로 안 본다.
    ob = OrderBook()
    seen: list[float] = []
    ob.on_fill_applied.append(lambda _o, q, _p, _f: seen.append(q))
    for i, q in enumerate((0.521, 5.208, 0.234)):
        ob.on_fill(fill("O5", q, 192.0, fill_id=f"ws{i}"))       # WS 체결이 먼저 → 버퍼
    ob.track("O5", _hl_intent(Side.SELL, 10.0))
    ob.apply_place_fill(fill("O5", 5.963, 192.03, fill_id="place-O5"))
    ob.replay_pending("O5")
    order = ob.order("O5")
    assert order is not None and order.filled_qty == 5.963
    assert seen == [5.963]  # 선반영 1회뿐 — 찌꺼기 체결 없음


def test_pending_buffer_is_bounded() -> None:
    ob = OrderBook()
    for i in range(OrderBook._PENDING_CAP + 50):        # 외부 주문 이벤트 무한 누적 방지
        ob.on_fill(fill(f"X{i}", 1, 1.0))
    assert len(ob._pending) <= OrderBook._PENDING_CAP


def test_float_accumulated_fills_reach_filled() -> None:
    # 실증 버그: HL 체결이 잘게 쪼개져 합이 부동소수점상 주문수량에 한 톨 못 미쳐도
    # (0.7+0.2+0.1 = 0.9999999999999999 < 1.0) '전량 체결'로 판정돼야 한다.
    ob = OrderBook()
    ob.track("F", intent(side=Side.SELL, qty=1.0))
    for i, q in enumerate((0.7, 0.2, 0.1)):
        ob.on_fill(fill("F", q, 1177.5, fill_id=f"f{i}"))
    order = ob.order("F")
    assert order is not None
    assert order.filled_qty < 1.0                       # 실제 누적은 1.0 미만(부동소수점)
    assert order.status is OrderStatus.FILLED           # 그래도 전량으로 판정
    assert order.remaining_qty == 0.0
    assert not order.is_open                             # 미체결 목록에서 빠진다


def test_genuine_partial_stays_partial() -> None:
    # 톨러런스가 진짜 부분체결을 가리지 않는다 — 6/10은 그대로 PARTIAL.
    ob = OrderBook()
    ob.track("P", intent(side=Side.SELL, qty=10))
    ob.on_fill(fill("P", 6, 100.0))
    order = ob.order("P")
    assert order is not None
    assert order.status is OrderStatus.PARTIAL and order.remaining_qty == 4.0


def test_untracked_fill_logs_a_warning_once(caplog: pytest.LogCaptureFixture) -> None:
    # 실증 2026-09-16: 응답 없는 발주 #3326의 체결이 조용히 보관만 돼 아무도 몰랐다 → 경고 1회
    import logging

    ob = OrderBook()
    with caplog.at_level(logging.WARNING, logger="kp_arb.order"):
        assert ob.on_fill(fill("X9", 1, 1_717_000.0)) is None
        assert ob.on_fill(fill("X9", 1, 1_717_000.0)) is None
    msgs = [r.getMessage() for r in caplog.records if "미추적 주문 #X9" in r.getMessage()]
    assert len(msgs) == 1 and "1 @ 1.717e+06" in msgs[0]


def test_replay_pending_needs_account_when_same_number_open_on_both_accounts(
        caplog: pytest.LogCaptureFixture) -> None:
    # 검토 2026-10-19 §A(결정 84 뒤 남은 구멍): 주식 #1871이 **열린 채** 있는데 선물 #1871 체결
    # 통보가 발주 응답보다 먼저 와 (KR_DERIV, "1871")에 보관 → 선물 track 뒤 계좌 없이 되돌리면
    # 장부가 계좌를 못 골라(둘 다 열림) 보관 체결이 그대로 남는다(= 선주문 체결 유실·헤지 없음).
    # 계좌를 넘기면 반영.
    ob = OrderBook()
    stock = ob.track("1871", OrderIntent(
        venue=Venue.LS, underlying=Underlying.SK_HYNIX, instrument=Instrument.KR_STOCK,
        side=Side.BUY, qty=20, order_type=OrderType.LIMIT, price=1_184_000.0,
        account=Account.KR_STOCK))
    ob.on_fill(Fill(fill_id="d1", order_id="1871", qty=2, price=1_212_000.0, ts=0,
                    account=Account.KR_DERIV))                       # 선물 체결, 등록 전 → 보관
    fut = ob.track("1871", OrderIntent(
        venue=Venue.LS, underlying=Underlying.SK_HYNIX, instrument=Instrument.KR_STOCK_FUTURE,
        side=Side.SELL, qty=2, order_type=OrderType.LIMIT, price=1_212_000.0,
        account=Account.KR_DERIV))
    with caplog.at_level(logging.WARNING, logger="kp_arb.order"):
        ob.replay_pending("1871")                                    # 계좌 없이 — 못 고름
    assert fut.filled_qty == 0 and stock.filled_qty == 0
    assert any("보관 통보 미반영" in r.getMessage() for r in caplog.records)
    ob.replay_pending("1871", account=Account.KR_DERIV)              # 계좌를 넘기면 반영
    assert fut.filled_qty == 2 and not fut.is_open and stock.filled_qty == 0
