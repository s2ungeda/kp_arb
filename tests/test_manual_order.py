"""수동 주문 순수 검증 로직 + 명령 핸들러 테스트 (DESIGN-manual-order.md §6.3)."""
import asyncio
import logging
from typing import Any

import pytest

from kp_arb.core_server import _ladder, _manual_command, manual_snapshot
from kp_arb.domain.enums import Account, Instrument, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent, Position, Quote
from kp_arb.manual_order import (
    is_spot_stock,
    sellable_qty,
    short_sale_error,
)
from kp_arb.order_book import OrderBook
from kp_arb.ws_status import WsStatus


def test_is_spot_stock() -> None:
    assert is_spot_stock(Instrument.KR_STOCK)
    assert is_spot_stock(Instrument.KR_ETF)
    assert not is_spot_stock(Instrument.KR_STOCK_FUTURE)  # 선물은 숏 허용
    assert not is_spot_stock(Instrument.HL_PERP)


def test_sellable_qty() -> None:
    assert sellable_qty(100, 30) == 70          # 보유100 − 미체결매도30
    assert sellable_qty(50, 50) == 0
    assert sellable_qty(10, 40) == 0            # 미체결이 보유 초과 → 0(음수 방지)
    assert sellable_qty(0, 0) == 0


def test_short_sale_error_spot_sell() -> None:
    # 매도가능(70) 이내면 통과
    assert short_sale_error(Instrument.KR_STOCK, Side.SELL, 70, 70) is None
    assert short_sale_error(Instrument.KR_STOCK, Side.SELL, 30, 70) is None
    # 초과면 공매도 거부
    msg = short_sale_error(Instrument.KR_STOCK, Side.SELL, 71, 70)
    assert msg is not None and "공매도" in msg
    # 보유 0인데 매도 → 거부
    assert short_sale_error(Instrument.KR_STOCK, Side.SELL, 1, 0) is not None


def test_short_sale_error_no_constraint_cases() -> None:
    # 매수는 언제나 통과(숏 아님)
    assert short_sale_error(Instrument.KR_STOCK, Side.BUY, 1000, 0) is None
    # 선물·HL 매도는 숏 허용 → 통과
    assert short_sale_error(Instrument.KR_STOCK_FUTURE, Side.SELL, 1000, 0) is None
    assert short_sale_error(Instrument.HL_PERP, Side.SELL, 1000, 0) is None


# --- 명령 핸들러 (_manual_command) — 가짜 LiveSystem + 실제 OrderBook ---


class _FakeSystem:
    """place/cancel를 기록하는 가짜 LiveSystem(라이브 API 없음)."""

    def __init__(
        self,
        order_book: OrderBook,
        fail: Exception | None = None,
        quotes: dict[Any, Quote] | None = None,
        trades: dict[Any, float] | None = None,
        ws: list[WsStatus] | None = None,
    ) -> None:
        self.order_book = order_book
        self._fail = fail
        self.quotes = quotes or {}
        self.trades = trades or {}
        self.hl_mark: dict[Any, Any] = {}          # 잔고표(B) — 마크+오라클
        self.hl_funding_rate: dict[Any, float] = {}
        self.hl_detail: dict[Any, dict[str, Any]] = {}
        self.instruments: dict[Any, Any] = {}  # 종목정보(§5.10) — 스냅샷 max_leverage 보정
        # 기본은 건강한 채널(연결된 주문 피드) — 무데이터 판정 없음 → 경고 없음.
        self.ws = ws if ws is not None else [
            WsStatus(venue="LS", name="LS", kind="주문", expects_stream=False,
                     connected=True)]
        self.placed: list[OrderIntent] = []
        self.cancelled: list[str] = []
        self.amended: list[tuple[str, float, bool, bool]] = []
        self.leverage_calls: list[tuple[Underlying, int, bool]] = []
        self.merges: list[tuple[Underlying, int | None, int | None]] = []
        self.refreshed = 0
        self.fx_started: list[Any] = []  # start_fx_auction 호출 기록
        self.fx_stopped = 0

    async def place(self, intent: OrderIntent) -> str:
        if self._fail is not None:
            raise self._fail
        self.placed.append(intent)
        return "OID-1"

    def bump_error(self) -> None:
        self.errors = getattr(self, "errors", 0) + 1  # 발주 실패 알람 카운터(테스트용)

    async def cancel(self, order_id: str) -> None:
        if self._fail is not None:
            raise self._fail
        self.cancelled.append(order_id)

    async def amend_price(self, order_id: str, price: float, *,
                          reduce_only: bool = False, post_only: bool = False) -> str:
        self.amended.append((order_id, price, reduce_only, post_only))
        return "OID-2"

    def set_hl_aggregation(self, u: Underlying, n_sig_figs: int | None,
                           mantissa: int | None) -> None:
        self.merges.append((u, n_sig_figs, mantissa))

    async def refresh_snapshot(self) -> None:
        self.refreshed += 1

    async def update_leverage(self, underlying: Underlying, leverage: int,
                              *, is_cross: bool) -> None:
        if self._fail is not None:
            raise self._fail
        self.leverage_calls.append((underlying, leverage, is_cross))

    def start_fx_auction(self, settings: Any) -> None:
        self.fx_started.append(settings)

    def stop_fx_auction(self) -> None:
        self.fx_stopped += 1

    def ws_statuses(self) -> list[WsStatus]:
        return self.ws


def _fake_system(
    order_book: OrderBook,
    fail: Exception | None = None,
    quotes: dict[Any, Quote] | None = None,
    trades: dict[Any, float] | None = None,
    ws: list[WsStatus] | None = None,
) -> Any:
    return _FakeSystem(order_book, fail, quotes, trades, ws)


def _ob_samsung(held: float = 100.0) -> OrderBook:
    ob = OrderBook()
    ob.load_snapshot(positions=[Position(
        venue=Venue.LS, instrument=Instrument.KR_STOCK, underlying=Underlying.SAMSUNG,
        side=Side.BUY, qty=held, avg_price=80000, account=Account.KR_STOCK)])
    return ob


async def test_manual_order_hl_buy_places() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_order", "instrument": "hl_perp",
        "underlying": "samsung", "side": "buy", "order_type": "market", "qty": 5})
    assert r["ok"] and r["order_id"] == "OID-1"
    assert len(sys.placed) == 1 and sys.placed[0].venue is Venue.HYPERLIQUID


async def test_manual_order_warns_when_ws_unhealthy_but_still_places() -> None:
    # Phase 8-6 — 수동은 경고만(§2 차단 아님): WS 끊김이어도 발주는 되고 warnings에 사유.
    down = WsStatus(venue="HL", name="HL", kind="시세/주문", expects_stream=True)  # 미연결
    sys = _fake_system(OrderBook(), ws=[down])
    r = await _manual_command(sys, {"cmd": "manual_order", "instrument": "hl_perp",
        "underlying": "samsung", "side": "buy", "order_type": "market", "qty": 5})
    assert r["ok"] and len(sys.placed) == 1        # 발주됨
    assert r["warnings"] and "끊김" in r["warnings"][0]  # 경고 동반


async def test_manual_order_no_warning_when_healthy() -> None:
    sys = _fake_system(OrderBook())  # 기본 건강한 채널
    r = await _manual_command(sys, {"cmd": "manual_order", "instrument": "hl_perp",
        "underlying": "samsung", "side": "buy", "order_type": "market", "qty": 5})
    assert r["ok"] and r["warnings"] == []


async def test_manual_order_stock_sell_within_holding() -> None:
    sys = _fake_system(_ob_samsung(100))
    r = await _manual_command(sys, {"cmd": "manual_order", "instrument": "kr_stock",
        "underlying": "samsung", "side": "sell", "order_type": "limit",
        "qty": 60, "price": 80000})
    assert r["ok"] and len(sys.placed) == 1


async def test_manual_order_stock_short_blocked() -> None:
    sys = _fake_system(_ob_samsung(100))
    r = await _manual_command(sys, {"cmd": "manual_order", "instrument": "kr_stock",
        "underlying": "samsung", "side": "sell", "order_type": "limit",
        "qty": 150, "price": 80000})
    assert not r["ok"] and "공매도" in r["errors"][0]
    assert sys.placed == []


async def test_manual_order_sell_counts_pending() -> None:
    ob = _ob_samsung(100)
    ob.track("PEND", OrderIntent(venue=Venue.LS, underlying=Underlying.SAMSUNG,
        instrument=Instrument.KR_STOCK, side=Side.SELL, qty=40, price=80000))
    sys = _fake_system(ob)  # 매도가능 = 100 − 40 = 60
    r = await _manual_command(sys, {"cmd": "manual_order", "instrument": "kr_stock",
        "underlying": "samsung", "side": "sell", "order_type": "limit",
        "qty": 61, "price": 80000})
    assert not r["ok"] and "공매도" in r["errors"][0]


async def test_manual_order_future_short_allowed() -> None:
    sys = _fake_system(OrderBook())  # 보유 0이어도 선물 숏 허용
    r = await _manual_command(sys, {"cmd": "manual_order",
        "instrument": "kr_stock_future", "underlying": "samsung", "side": "sell",
        "order_type": "limit", "qty": 5, "price": 80000})
    assert r["ok"] and len(sys.placed) == 1


async def test_manual_order_reduce_post_flags() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_order", "instrument": "hl_perp",
        "underlying": "samsung", "side": "sell", "order_type": "limit",
        "qty": 5, "price": 1000, "reduce_only": True, "post_only": True})
    assert r["ok"]
    intent = sys.placed[0]
    assert intent.reduce_only is True and intent.post_only is True


async def test_manual_cancel_calls_system() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_cancel", "order_id": "X1"})
    assert r["ok"] and sys.cancelled == ["X1"]


async def test_manual_amend_calls_system() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(
        sys, {"cmd": "manual_amend", "order_id": "X1", "price": 80500,
              "reduce_only": True, "post_only": True})
    assert r["ok"] and r["order_id"] == "OID-2"
    assert sys.amended == [("X1", 80500.0, True, True)]  # 정정 옵션 전달


async def test_manual_leverage_calls_system() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_leverage",
        "underlying": "samsung", "leverage": 10, "is_cross": True})
    assert r["ok"]
    assert sys.leverage_calls == [(Underlying.SAMSUNG, 10, True)]


async def test_manual_leverage_bad_args() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_leverage", "underlying": "samsung"})
    assert not r["ok"] and "레버리지 인자" in r["errors"][0]


async def test_manual_cancel_failure_logs(caplog: pytest.LogCaptureFixture) -> None:
    # #3 실패 무조건 로그 — 취소 실패는 화면(_fail)뿐 아니라 파일 로그에도 남는다.
    sys = _fake_system(OrderBook(), fail=RuntimeError("거래소 거부"))
    with caplog.at_level(logging.WARNING, logger="kp_arb.order"):
        r = await _manual_command(sys, {"cmd": "manual_cancel", "order_id": "X1"})
    assert not r["ok"]
    assert any("취소 실패" in rec.message and rec.name == "kp_arb.order"
               for rec in caplog.records)


async def test_manual_leverage_failure_logs(caplog: pytest.LogCaptureFixture) -> None:
    # #3 실패 무조건 로그 — 레버리지 변경 실패도 파일 로그에 작업·사유가 남는다.
    sys = _fake_system(OrderBook(), fail=RuntimeError("증거금 부족"))
    with caplog.at_level(logging.WARNING, logger="kp_arb.order"):
        r = await _manual_command(sys, {"cmd": "manual_leverage",
            "underlying": "samsung", "leverage": 10, "is_cross": True})
    assert not r["ok"]
    assert any("레버리지 변경 실패" in rec.message for rec in caplog.records)


async def test_manual_amend_needs_price() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_amend", "order_id": "X1"})
    assert not r["ok"] and sys.amended == []


async def test_manual_amend_hl_blocked_at_core() -> None:
    # HL 정정 금지가 화면뿐 아니라 코어에서도 막힌다 — 대상 주문이 HL이면 거부, modify 미발생.
    ob = OrderBook()
    ob.track("H1", OrderIntent(venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
        instrument=Instrument.HL_PERP, side=Side.SELL, qty=1, price=1000.0))
    sys = _fake_system(ob)
    r = await _manual_command(
        sys, {"cmd": "manual_amend", "order_id": "H1", "price": 1010})
    assert not r["ok"] and "HL" in r["errors"][0]
    assert sys.amended == []       # 코어에서 차단 — amend_price 안 불림


async def test_manual_amend_ls_still_allowed() -> None:
    # LS 주문은 정정 허용 — 코어 방어가 HL만 막는다.
    ob = OrderBook()
    ob.track("L1", OrderIntent(venue=Venue.LS, underlying=Underlying.SAMSUNG,
        instrument=Instrument.KR_STOCK, side=Side.SELL, qty=10, price=80000.0))
    sys = _fake_system(ob)
    r = await _manual_command(
        sys, {"cmd": "manual_amend", "order_id": "L1", "price": 80500})
    assert r["ok"] and sys.amended == [("L1", 80500.0, False, False)]


async def test_manual_hl_merge_calls_system() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_hl_merge",
        "underlying": "samsung", "n_sig_figs": 5, "mantissa": 2})
    assert r["ok"] and sys.merges == [(Underlying.SAMSUNG, 5, 2)]


async def test_manual_hl_merge_raw_is_none() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_hl_merge",
        "underlying": "samsung", "n_sig_figs": None, "mantissa": None})
    assert r["ok"] and sys.merges == [(Underlying.SAMSUNG, None, None)]


async def test_manual_refresh_resyncs() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_refresh"})
    assert r["ok"]                    # 즉시 OK(응답 안 막음)
    await asyncio.sleep(0.02)         # 백그라운드 새로고침이 돌 시간
    assert sys.refreshed == 1


async def test_manual_no_system_rejected() -> None:
    assert not (await _manual_command(None, {"cmd": "manual_order"}))["ok"]
    assert not (await _manual_command(None, {"cmd": "manual_cancel"}))["ok"]


async def test_manual_order_bad_args_rejected() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "manual_order", "instrument": "nope",
        "underlying": "samsung", "side": "buy", "qty": 1})
    assert not r["ok"] and sys.placed == []


async def test_manual_order_place_failure_reported() -> None:
    sys = _fake_system(OrderBook(), fail=RuntimeError("거부됨"))
    r = await _manual_command(sys, {"cmd": "manual_order", "instrument": "hl_perp",
        "underlying": "samsung", "side": "buy", "order_type": "market", "qty": 5})
    assert not r["ok"] and "거부됨" in r["errors"][0]


# --- 수동창 스냅샷 (manual_snapshot) ---


def test_ladder_sorts_and_merges() -> None:
    q = Quote(underlying=Underlying.SK_HYNIX, instrument=Instrument.HL_PERP,
              bid=1121.6, ask=1121.7, ts=0.0,
              asks=[(1122.0, 0.2), (1121.7, 1.0), (1121.8, 2.0), (1122.0, 0.5)],
              bids=[(1121.3, 1.0), (1121.6, 3.0)])
    asks = _ladder(q, asks=True)
    # 매도 오름차순(최우선=최저 먼저) + 같은 가격(1122.0) 잔량 병합
    assert asks == [[1121.7, 1.0], [1121.8, 2.0], [1122.0, 0.7]]
    bids = _ladder(q, asks=False)
    assert bids[0][0] == 1121.6  # 매수 내림차순(최우선=최고 먼저)


def test_manual_snapshot_no_system() -> None:
    snap = manual_snapshot(None)
    assert not snap["connected"] and snap["symbols"] == {} and snap["open_orders"] == []


def test_manual_snapshot_shape() -> None:
    ob = _ob_samsung(100)
    ob.track("SELL1", OrderIntent(venue=Venue.LS, underlying=Underlying.SAMSUNG,
        instrument=Instrument.KR_STOCK, side=Side.SELL, qty=40, price=80000))
    quote = Quote(underlying=Underlying.SAMSUNG, instrument=Instrument.KR_STOCK,
        bid=79900, ask=80000, ts=0.0, bid_qty=10, ask_qty=20,
        bids=[(79900, 10), (79800, 5)], asks=[(80000, 20), (80100, 8)])
    sys = _fake_system(
        ob,
        quotes={(Underlying.SAMSUNG, Instrument.KR_STOCK, "krx"): quote},
        trades={(Underlying.SAMSUNG, Instrument.KR_STOCK, "krx"): 79950.0})
    snap = manual_snapshot(sys)
    assert snap["connected"]
    sam = snap["symbols"]["samsung|kr_stock"]
    assert sam["asks"] == [[80000, 20], [80100, 8]]      # 다단계 호가창
    assert sam["bids"][0] == [79900, 10]
    assert sam["position"] == 100
    assert sam["avg_price"] == 80000
    assert sam["last"] == 79950.0
    assert sam["pnl"] == (79950 - 80000) * 100            # 평가손익 = -5,000
    assert sam["eval"] == 100 * 79950                     # 평가금액
    assert sam["tick"] is not None and sam["tick"] > 0    # 호가모드용 틱
    assert sam["sellable"] == 60                          # 보유100 − 미체결매도40
    assert "balance" in sam                               # LS 계좌 잔고 포함
    # HL은 매도가능(현물 전용) 없음
    assert "sellable" not in snap["symbols"]["samsung|hl_perp"]
    # 미체결에 SELL1 포함
    assert any(o["order_id"] == "SELL1" for o in snap["open_orders"])


def test_manual_snapshot_hl_fields() -> None:
    # 잔고표 오른쪽(B) — 오라클·펀딩률(WS 저장), 마진·누적펀딩·청산가(clearinghouse detail)
    from kp_arb.gateways.hl import Mark

    sys = _fake_system(OrderBook())
    sys.hl_mark[Underlying.SAMSUNG] = Mark(
        underlying=Underlying.SAMSUNG, price=167.5, oracle=167.4)
    sys.hl_funding_rate[Underlying.SAMSUNG] = 0.0000125
    sys.hl_detail[Underlying.SAMSUNG] = {
        "margin": 12.3, "cum_funding": -0.45, "liq": 150.0,
        "leverage": 10.0, "leverage_cross": True, "max_leverage": 20.0}
    hl = manual_snapshot(sys)["symbols"]["samsung|hl_perp"]
    assert hl["oracle"] == 167.4
    assert hl["funding_rate"] == 0.0000125
    assert hl["margin"] == 12.3 and hl["cum_funding"] == -0.45
    assert hl["liq"] == 150.0  # detail이 기본 None을 덮어씀
    assert hl["leverage"] == 10.0 and hl["leverage_cross"] is True  # D: 버튼 캡션용
    assert hl["max_leverage"] == 20.0


async def test_fx_auction_start_stop() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "fx_auction_start",
        "windows": [["08:30", "08:46"], ["15:35", "15:46"]],
        "fx_code": "175X9000", "price": 1421.5, "tick": 10, "hedge_ratio": 50})
    assert r["ok"]
    s = sys.fx_started[0]
    assert s.fx_code == "175X9000" and s.tick == 10 and s.price == 1421.5
    assert s.hedge_ratio == 0.5  # % → 비율 변환
    assert s.windows == (("08:30", "08:46"), ("15:35", "15:46"))
    r2 = await _manual_command(sys, {"cmd": "fx_auction_stop"})
    assert r2["ok"] and sys.fx_stopped == 1


async def test_fx_auction_start_bad_args() -> None:
    sys = _fake_system(OrderBook())
    r = await _manual_command(sys, {"cmd": "fx_auction_start", "windows": []})  # 인자 부족
    assert not r["ok"] and sys.fx_started == []
