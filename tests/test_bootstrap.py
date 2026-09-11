"""부트스트랩 계약 테스트 — 시동(스냅샷→실시간 결선)과 선물 월물 선택. 라이브 없음."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from kp_arb.bootstrap import (
    HLAmendForbidden,
    LiveSystem,
    select_near_month_futures,
    startup_symbol_error,
)
from kp_arb.domain.enums import Account, Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent, Position
from kp_arb.gateways.ls_ws import LSWebSocketClient, WSClosed
from kp_arb.gateways.mock_ls import MockLSGateway
from kp_arb.order_book import OrderBook, OrderStatus
from kp_arb.session_service import SessionService

SAMSUNG = Underlying.SAMSUNG


# --- 선물 최근월물 선택 (t8401 실측 shape) ---

MASTER_ROWS = [
    {"hname": "삼성전자   F 202608", "shcode": "A1168000", "basecode": "A005930"},
    {"hname": "삼성전자   F 202607", "shcode": "A1167000", "basecode": "A005930"},
    {"hname": "삼성전자   F 202703", "shcode": "A1173000", "basecode": "A005930"},
    {"hname": "삼성전자   SP 2607-2", "shcode": "D116768S", "basecode": "A005930"},  # 스프레드 제외
    {"hname": "현대차     F 202607", "shcode": "A1667000", "basecode": "A005380"},
    {"hname": "SK하이닉스 F 202607", "shcode": "A5067000", "basecode": "A000660"},
    {"hname": "카카오     F 202607", "shcode": "A9997000", "basecode": "A035720"},  # 대상 외
]


def test_record_fill_appends_from_applied_fill() -> None:
    # 체결내역 보관 — 실제 적용 시점(order+체결량)으로 기록(종목·방향 함께).
    from collections import deque
    from types import SimpleNamespace

    ob = OrderBook()
    order = ob.track("O1", OrderIntent(
        venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
        instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.1,
        order_type=OrderType.LIMIT, price=163.0))
    sys = SimpleNamespace(order_book=ob, fills=deque(maxlen=200))
    LiveSystem._record_fill(sys, order, 0.1, 163.5, "F1")  # type: ignore[arg-type]
    assert len(sys.fills) == 1
    assert sys.fills[0]["side"] == "sell"
    assert sys.fills[0]["qty"] == 0.1 and sys.fills[0]["price"] == 163.5


def test_taker_immediate_fill_recorded_once_via_on_fill_applied() -> None:
    # 회귀(2026-08-20): taker 즉시체결(apply_place_fill)이 체결내역에 1회 잡히고,
    # 뒤늦은 userFills 재통보는 흡수돼 중복 안 됨 — 통보 타이밍 경합과 무관.
    from collections import deque
    from types import SimpleNamespace

    from kp_arb.gateways.ls_ws import Fill

    ob = OrderBook()
    sys = SimpleNamespace(order_book=ob, fills=deque(maxlen=200))
    ob.on_fill_applied.append(
        lambda o, q, p, fid: LiveSystem._record_fill(sys, o, q, p, fid))  # type: ignore[arg-type]
    ob.track("O1", OrderIntent(
        venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
        instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.1,
        order_type=OrderType.LIMIT, price=163.0))
    ob.apply_place_fill(Fill(fill_id="place-O1", order_id="O1", qty=0.1,
                             price=163.5, fee=0.0, ts=0.0))
    assert len(sys.fills) == 1 and sys.fills[0]["qty"] == 0.1  # 즉시체결 기록됨
    ob.on_fill(Fill(fill_id="tid-1", order_id="O1", qty=0.1, price=163.5, fee=0.0, ts=0.0))
    assert len(sys.fills) == 1  # 재통보 흡수 → 중복 없음


def test_record_cancel_captures_time_and_intent() -> None:
    # 취소내역 보관 — 취소된(잔여) 수량·종목·방향·시각을 담는다(주문 리스트 '취소' 행).
    from collections import deque
    from types import SimpleNamespace

    ob = OrderBook()
    order = ob.track("O9", OrderIntent(
        venue=Venue.HYPERLIQUID, underlying=Underlying.SK_HYNIX,
        instrument=Instrument.HL_PERP, side=Side.BUY, qty=0.05,
        order_type=OrderType.LIMIT, price=1400.0, source="자동M"))
    sys = SimpleNamespace(cancels=deque(maxlen=200), fills=deque(maxlen=200))
    LiveSystem._record_cancel(sys, order)
    assert len(sys.cancels) == 1
    c = sys.cancels[0]
    assert c["side"] == "buy" and c["qty"] == 0.05 and c["time"]  # 시각 채워짐
    assert c["source"] == "자동M"  # 출처 — 주문 리스트 '출처' 칸·필터(2026-09-11)
    LiveSystem._record_fill(sys, order, 0.05, 1400.0, "f1")
    assert sys.fills[0]["source"] == "자동M" and sys.fills[0]["qty"] == 0.05


async def test_guarded_ws_restarts_then_stops(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # graceful close(run 정상 반환) → 재시작(재연결) / WSClosed → 종료 / 예외 → 포기.
    orig_sleep = asyncio.sleep
    monkeypatch.setattr("kp_arb.bootstrap.asyncio.sleep", lambda *_: orig_sleep(0))
    calls = {"n": 0}

    async def make_run() -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            return          # graceful close → 재시작
        raise WSClosed      # 커넥터 종료 → 재시작 안 함

    await LiveSystem._guarded_ws("HL", make_run)
    assert calls["n"] == 3  # 정상반환 2회 재시작 후 WSClosed로 종료(무한 아님)


async def test_guarded_ws_gives_up_on_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 예외(설정·인증 등)면 재시작 안 하고 그 채널만 포기(무한 재시도 방지).
    calls = {"n": 0}

    async def make_run() -> None:
        calls["n"] += 1
        raise RuntimeError("auth 실패")

    await LiveSystem._guarded_ws("선물", make_run)  # 예외 삼키고 반환
    assert calls["n"] == 1  # 1회만 — 재시작 안 함


async def test_guarded_ws_reports_dead_channel() -> None:
    # 채널이 예외로 영구 중단되면 on_dead 콜백 → LiveSystem이 팝업용 실패로 기록.
    async def make_run() -> None:
        raise RuntimeError("auth 실패")

    system, _, _ = _system([])
    await LiveSystem._guarded_ws("주식", make_run, system._mark_ws_dead)
    assert system.startup_load_error == "WS 채널(주식) 중단"
    system._mark_ws_dead("HL")  # 먼저 기록된 실패는 덮지 않는다
    assert system.startup_load_error == "WS 채널(주식) 중단"


def test_startup_symbol_error_rules() -> None:
    # 시동 '종목' 판정 — 근·차근월물 누락·원달러 월물 없음을 작업명에 담는다(차근도 실패, §5.11).
    expected = [Underlying.SAMSUNG, Underlying.SK_HYNIX]
    fx = [("175W09", 202609)]
    near = {Underlying.SAMSUNG: "A1", Underlying.SK_HYNIX: "A2"}
    nxt = {Underlying.SAMSUNG: "B1", Underlying.SK_HYNIX: "B2"}
    assert startup_symbol_error(expected, near, fx, nxt) is None
    err = startup_symbol_error(expected, {Underlying.SAMSUNG: "A1"}, fx, nxt)
    assert err is not None and "근월물 없음" in err and "sk_hynix" in err
    err = startup_symbol_error(expected, near, fx, {Underlying.SAMSUNG: "B1"})
    assert err == "종목(주식선물 차근월물 없음: sk_hynix)"  # 근월물은 정상 → 차근만 문제
    err = startup_symbol_error(expected, near, [], nxt)
    assert err == "종목(원달러선물 월물 없음)"
    err = startup_symbol_error(expected, {}, [], {})
    assert err is not None and "근월물 없음" in err and "원달러선물 월물 없음" in err


def test_select_near_month_futures() -> None:
    symbols = select_near_month_futures(MASTER_ROWS)
    assert symbols == {
        Underlying.SAMSUNG: "A1167000",   # 202607 < 202608 < 202703
        Underlying.HYUNDAI: "A1667000",
        Underlying.SK_HYNIX: "A5067000",
    }


def test_select_ignores_spread_and_unknown() -> None:
    rows = [
        {"hname": "삼성전자   SP 2607-2", "shcode": "D116768S", "basecode": "A005930"},
        {"hname": "카카오     F 202607", "shcode": "A9997000", "basecode": "A035720"},
    ]
    assert select_near_month_futures(rows) == {}


# --- LiveSystem 시동 (mock 게이트웨이 + 가짜 WS) ---


class FakeConnection:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def _gen(self) -> AsyncIterator[str]:
        for frame in self.frames:
            yield frame
        raise WSClosed  # 프레임 소진 = 세션 종료(테스트) — _guarded_ws 재시작 루프를 끊는다

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen()


class FakeConnector:
    def __init__(self, frames: list[str]) -> None:
        self.conn = FakeConnection(frames)

    async def connect(self) -> FakeConnection:
        return self.conn


def _fill_frame(order_id: str) -> str:
    return json.dumps({"header": {"tr_cd": "SC1"},
                       "body": {"execno": "1", "ordno": order_id, "execqty": "10",
                                "execprc": "70000", "exectime": "100000000"}})


def _system(
    stock_frames: list[str], deriv_frames: list[str] | None = None
) -> tuple[LiveSystem, FakeConnector, FakeConnector | None]:
    gw = MockLSGateway()
    gw.seed_balance(Account.KR_STOCK, 5_000_000)
    gw.seed_position(Position(venue=Venue.LS, instrument=Instrument.KR_STOCK,
                              underlying=SAMSUNG, side=Side.BUY, qty=100,
                              avg_price=290_000, account=Account.KR_STOCK))
    stock_connector = FakeConnector(stock_frames)
    deriv_connector = FakeConnector(deriv_frames) if deriv_frames is not None else None
    system = LiveSystem(
        gateway=gw,  # type: ignore[arg-type]  # LSGateway 계약만 사용
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=LSWebSocketClient(stock_connector),
        deriv_ws=(LSWebSocketClient(deriv_connector)
                  if deriv_connector is not None else None),
    )
    return system, stock_connector, deriv_connector


def test_ws_statuses_collects_present_clients() -> None:
    # WS 세션 현황(Phase 8-3c) — 살아있는 채널만 모으고 없는 채널(HL)은 건너뛴다.
    system, _, _ = _system([], deriv_frames=[])
    statuses = system.ws_statuses()
    assert len(statuses) == 2  # LS 주식 + LS 선물 (HL 미설정 → 제외)
    assert all(s.venue == "LS" for s in statuses)
    assert [s.to_dict()["connected"] for s in statuses] == [False, False]  # 시동 전


async def test_refresh_snapshot_keeps_futures_orders_without_open_order_query() -> None:
    # 실측 2026-09-09: 선물 미체결 TR이 없어 빈 결과인데 "조회 성공"으로 보고 걸린 선주문 #20851을
    # 유령으로 지움 → 체결 통보 미아 → 자동M이 없는 주문 취소 되풀이. 조회가 되는 계좌(주식)만 정리.
    import time as _t

    from kp_arb.domain.models import OrderIntent

    system, _, _ = _system([])
    ob = system.order_book

    def intent(inst: Instrument, account: Account) -> OrderIntent:
        return OrderIntent(venue=Venue.LS, underlying=SAMSUNG, instrument=inst, side=Side.BUY,
                           qty=1, order_type=OrderType.LIMIT, price=1.0, account=account)

    fut = ob.track("D1", intent(Instrument.KR_STOCK_FUTURE, Account.KR_DERIV))
    stk = ob.track("S1", intent(Instrument.KR_STOCK, Account.KR_STOCK))
    fut.placed_ts = stk.placed_ts = _t.monotonic() - 60  # 유예(15초) 지난 옛 주문
    await system.refresh_snapshot()  # mock: 미체결 조회는 둘 다 빈 결과
    assert ob.order("D1") is not None   # 선물: 조회 불가 계좌 → 보존
    assert ob.order("S1") is None       # 주식: 실제 조회 결과에 없음 → 유령 정리


async def test_hl_reconnect_resync_leaves_ls_book_alone() -> None:
    # 사용자 확정 2026-09-09: 재연결 재동기는 끊긴 시장만. HL 재연결이 LS 주식 주문·포지션을
    # 건드리면 안 된다(옛: 전체 재동기 → 주식 미체결이 빈 결과라 유령 정리 대상).
    import time as _t

    system, _, _ = _system([])
    ob = system.order_book
    stk = ob.track("S1", OrderIntent(
        venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK, side=Side.BUY,
        qty=1, order_type=OrderType.LIMIT, price=1.0, account=Account.KR_STOCK))
    stk.placed_ts = _t.monotonic() - 60
    before = ob.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK)
    await system.refresh_snapshot(scope=system._RECONNECT_SCOPE["HL"])  # HL만
    assert ob.order("S1") is not None                                    # LS 주문 보존
    assert ob.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == before
    await system.refresh_snapshot(scope=system._RECONNECT_SCOPE["주식"])  # 주식 재연결
    assert ob.order("S1") is None                                        # 주식은 실제 조회 → 정리


def test_fx_spot_backup_due_only_when_never_or_long_silent() -> None:
    # 하나고시 대체는 CUR을 한 번도 못 받았거나 10분 넘게 조용할 때만 — 개장 전후 1~2분 간격
    # 체결에 60초 기준이 계속 걸려 출처가 널뛰던 것을 고침(2026-09-04 실측).
    from kp_arb.bootstrap import fx_spot_backup_due

    assert fx_spot_backup_due(0.0, 1000.0) is True          # 시동 후 미수신
    assert fx_spot_backup_due(1000.0, 1000.0 + 89) is False  # 89초 무수신 — 유지
    assert fx_spot_backup_due(1000.0, 1000.0 + 599) is False
    assert fx_spot_backup_due(1000.0, 1000.0 + 601) is True  # 10분 초과 — 대체


def test_set_fx_spot_window_changes_effective_rate_source() -> None:
    # 현물환율 사용시간을 설정창에서 바꾸면 HL 환산 환율 출처 판정이 즉시 그 창을 따른다.
    from datetime import datetime

    system, _, _ = _system([])
    system._apply_fx_spot(1386.1)
    at_9 = datetime(2026, 9, 4, 9, 0)
    assert system.usdkrw_effective(at_9)[1] == "현물"          # 기본 07:00~18:10 안
    system.set_fx_spot_window("10:00", "15:00")
    assert system.usdkrw_effective(at_9)[1] != "현물"          # 새 창 밖 → 이론가
    with pytest.raises(ValueError):
        system.set_fx_spot_window("25:00", "15:00")


def test_fx_spot_source_marked_ls() -> None:
    # 현물환율 출처 — LS 실시간 수신이면 "LS"(하나고시 백업과 구분해 상태줄에 표시).
    system, _, _ = _system([])
    assert system.usdkrw_spot_src is None
    system._apply_fx_spot(1386.1)
    assert system.usdkrw_spot == 1386.1 and system.usdkrw_spot_src == "LS"


def test_set_carry_rates_recomputes_fx_theory_immediately() -> None:
    # 금리 설정을 바꾸면 다음 선물 틱을 기다리지 않고 환율이론가를 즉시 다시 계산한다.
    system, _, _ = _system([])
    system._fx_futures = ("175W09", 202609)
    system._fx_months = [("175W09", 202609)]
    system._apply_fx_price("175W09", 1357.9)
    before = system.usdkrw_theory
    assert before is not None
    system.set_carry_rates(fx=0.004, eq=0.03)  # 1.0% → 0.4%
    after = system.usdkrw_theory
    assert after is not None and after < before  # 금리가 낮아졌으니 환산값도 내려간다


def test_fx_price_only_near_month_feeds_theory() -> None:
    # §9.1 — 근·차근 둘 다 저장하되, 환율이론가는 최근월물로만 갱신(차근에 섞이지 않게).
    system, _, _ = _system([])
    system._fx_futures = ("175W07", 202607)
    system._fx_months = [("175W07", 202607), ("175W08", 202608)]

    system._apply_fx_price("175W08", 1600.0)  # 차근월물 먼저
    assert system.fx_futures_price["175W08"] == 1600.0
    assert system.usdkrw_theory is None       # 차근은 이론가에 안 먹임
    assert system.usdkrw_futures is None

    system._apply_fx_price("175W07", 1530.0)  # 최근월물
    assert system.fx_futures_price["175W07"] == 1530.0
    assert system.usdkrw_futures == 1530.0
    assert system.usdkrw_theory is not None    # 최근월물만 이론가 갱신


async def test_ws_reconnect_triggers_resync() -> None:
    # Phase 8-4b — 재연결 훅이 OrderBook 재스냅샷(refresh_snapshot)을 백그라운드로 부른다.
    system, _, _ = _system([], deriv_frames=[])
    system._wire()  # on_reconnect 콜백 등록
    calls: list[object] = []

    async def spy(scope: object = None) -> None:
        calls.append(scope)

    system.refresh_snapshot = spy  # type: ignore[method-assign]
    system._stock_ws.on_reconnect[0]()  # 재연결 발화(동기) → 백그라운드 재동기 태스크
    for task in list(system._bg):
        await task
    assert calls == [{Account.KR_STOCK}]  # 끊긴 시장(주식 계좌)만 재동기(2026-09-09)


async def test_start_loads_snapshot_then_streams() -> None:
    intent = OrderIntent(venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
                         side=Side.BUY, qty=10, order_type=OrderType.MARKET)
    system, connector, _ = _system([])
    oid = await system.place(intent)  # 주문 등록(track)
    await system.start()

    # 1) 최초 스냅샷이 OrderBook에 로드됨
    assert system.order_book.balance(Account.KR_STOCK) == 5_000_000
    assert system.order_book.position_qty(SAMSUNG, Instrument.KR_STOCK, Account.KR_STOCK) == 100
    assert system.order_book.order(oid) is not None
    await system.wait()  # 프레임 소진 → 정상 종료

    # 구독 등록 확인: 시세(3종)+JIF+주식 체결통보 (선물 통보는 파생 WS 몫)
    trs = {json.loads(m)["body"]["tr_cd"] for m in connector.conn.sent}
    assert {"H1_", "UH1", "JIF", "SC0", "SC1"} <= trs  # NXT는 통합(UH1)로 수신
    assert "O01" not in trs


async def test_prices_seeded_before_subscribe() -> None:
    # 사용자 원칙(2026-09-03): 실시간이 안 와도 현재가가 0/None이면 안 된다 →
    # WS 결선(구독) **전에** REST 조회로 채워져 있어야 한다.
    system, _, _ = _system([])
    system._gw.seed_last_price(SAMSUNG, Instrument.KR_STOCK, 71_000.0)  # type: ignore[attr-defined]
    seen: dict[str, dict[object, float]] = {}
    orig_wire = system._wire

    def wire() -> None:
        seen["at_wire"] = dict(system.trades)  # 결선 시점에 이미 채워져 있나
        orig_wire()

    system._wire = wire  # type: ignore[method-assign]
    await system.start()
    await system.wait()
    assert seen["at_wire"].get((SAMSUNG, Instrument.KR_STOCK, "krx")) == 71_000.0


async def test_startup_init_ok_leaves_no_error() -> None:
    # 시동 초기화 성공(종목·잔고·포지션·주문 전부 OK) → startup_load_error 없음 + 스냅샷 로드.
    system, _, _ = _system([])
    await system._startup_init()
    assert system.startup_load_error is None
    assert system.order_book.balance(Account.KR_STOCK) == 5_000_000


async def test_startup_init_marks_balance_step_on_failure() -> None:
    # 잔고 조회가 실패하면 "잔고"로 남기고 즉시 중단(예외를 던지지 않아 코어는 계속 산다).
    system, _, _ = _system([])

    async def boom(_account: Account) -> float:
        raise RuntimeError("조회 실패")

    system._gw.get_balance = boom  # type: ignore[method-assign]
    await system._startup_init()
    assert system.startup_load_error == "잔고"


async def test_startup_init_marks_order_step_after_earlier_ok() -> None:
    # 앞 단계(잔고·포지션)는 성공하고 주문(미체결) 조회만 실패하면 "주문"으로 남는다.
    system, _, _ = _system([])

    async def boom(_account: Account) -> list[object]:
        raise RuntimeError("조회 실패")

    system._gw.get_open_orders = boom  # type: ignore[method-assign]
    await system._startup_init()
    assert system.startup_load_error == "주문"


async def test_startup_init_short_circuits_on_symbol_error() -> None:
    # 종목 실패(근월물 누락)가 이미 잡혀 있으면 잔고 조회까지 가지 않고 그대로 멈춘다.
    system, _, _ = _system([])
    system.startup_load_error = "종목(주식선물 근월물 없음: 하이닉스)"
    called = {"balance": False}
    orig = system._gw.get_balance

    async def spy(account: Account) -> float:
        called["balance"] = True
        return await orig(account)

    system._gw.get_balance = spy  # type: ignore[method-assign]
    await system._startup_init()
    assert system.startup_load_error == "종목(주식선물 근월물 없음: 하이닉스)"
    assert called["balance"] is False  # 종목 실패로 뒤 단계는 아예 실행 안 함


async def test_startup_init_marks_instrument_info_step() -> None:
    # HL 종목정보(소수자릿수·레버리지) 조회 실패도 시동 필수 로드(사용자 확정 2026-09-03) —
    # "종목정보"로 남기고 중단해 메인창 팝업으로 이어진다.
    from kp_arb.gateways.mock_hl import MockHLGateway

    hl = MockHLGateway()

    async def boom() -> dict[Underlying, dict[str, object]]:
        raise RuntimeError("meta 조회 실패")

    hl.get_instrument_meta = boom  # type: ignore[method-assign]
    system = LiveSystem(
        gateway=MockLSGateway(),  # type: ignore[arg-type]
        order_book=OrderBook(), session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=hl,  # hl_ws 불필요 — 시동 초기화(REST)만 검사
    )
    await system._startup_init()
    assert system.startup_load_error == "종목정보(HL 소수자릿수·레버리지)"


async def test_fill_frame_updates_order_book_realtime() -> None:
    intent = OrderIntent(venue=Venue.LS, underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
                         side=Side.BUY, qty=10, order_type=OrderType.MARKET)
    system, _, _ = _system([_fill_frame("LS-1")])  # MockLSGateway의 첫 주문번호
    oid = await system.place(intent)  # start 전에 track(체결 프레임과의 race 방지)
    assert oid == "LS-1"
    await system.start()
    await system.wait()  # 프레임 재생 완료

    order = system.order_book.order("LS-1")
    assert order is not None and order.status is OrderStatus.FILLED
    assert system.order_book.position_qty(SAMSUNG, Instrument.KR_STOCK,
                                          Account.KR_STOCK) == 110  # 100(스냅샷)+10(체결)


async def test_session_init_env_seeds_phase(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from kp_arb.domain.enums import SessionPhase

    monkeypatch.setenv("KP_SESSION_INIT", "regular")
    system, _, _ = _system([])
    await system.start()
    await system.wait()
    assert system.session.phase_for(SAMSUNG) is SessionPhase.REGULAR


async def test_session_init_invalid_stays_dead(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from kp_arb.domain.enums import SessionPhase

    monkeypatch.setenv("KP_SESSION_INIT", "bogus")
    system, _, _ = _system([])
    await system.start()
    await system.wait()
    assert system.session.phase_for(SAMSUNG) is SessionPhase.DEAD  # 보수 유지


async def test_hl_slot_snapshot_marks_and_fills() -> None:
    # HL 슬롯: 스냅샷 포지션 합류 + 마크 fan-out + HL 체결 → OrderBook.
    import json as _json

    from kp_arb.gateways.hl_ws import HLWebSocketClient
    from kp_arb.gateways.mock_hl import MockHLGateway

    hl_gw = MockHLGateway()
    hl_gw.seed_position(Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                                 underlying=SAMSUNG, side=Side.SELL, qty=0.1, avg_price=184.0))
    hl_fill = _json.dumps({"channel": "userFills", "data": {"fills": [
        {"coin": "xyz:SMSN", "px": "185.0", "sz": "0.2", "side": "A",
         "oid": 777, "tid": 1, "time": 1.0}]}})
    mark = _json.dumps({"channel": "activeAssetCtx",
                        "data": {"coin": "xyz:SMSN", "ctx": {"markPx": "184.5"}}})
    hl_ws = HLWebSocketClient(FakeConnector([mark, hl_fill]))

    gw = MockLSGateway()
    system = LiveSystem(
        gateway=gw,  # type: ignore[arg-type]
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=hl_gw,
        hl_ws=hl_ws,
    )
    hl_intent = OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                            instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.2,
                            order_type=OrderType.MARKET)
    marks: list[float] = []
    system.on_mark.append(lambda m: marks.append(m.price))
    system.order_book.track("777", hl_intent)  # HL 체결 매칭용
    await system.start()
    await system.wait()

    # 스냅샷: HL 포지션 합류 (숏 0.1)
    assert system.order_book.position_qty(SAMSUNG, Instrument.HL_PERP) == -0.1 - 0.2
    assert marks == [184.5]  # 마크 fan-out
    assert system.order_book.order("777").filled_qty == 0.2  # HL 체결 반영


async def test_hl_order_identified_by_cloid_before_place_returns() -> None:
    # DESIGN §HL cloid ①(실측 2026-09-11 10:18:35): 발주 응답(0.7~1.0초)보다 웹소켓 통보가 먼저
    # 오면 orderUpdates의 cloid로 oid를 식별해 즉시 장부에 등록·체결 반영·훅 통지. 응답이 뒤에
    # 오면 즉시체결 선반영은 이미 반영된 체결을 뺀 차이만(이중 반영 없음).
    import asyncio as _aio
    import json as _json

    from kp_arb.gateways.hl_ws import HLWebSocketClient
    from kp_arb.gateways.mock_hl import MockHLGateway

    CLOID = "0x" + "ab" * 16

    class SlowHL(MockHLGateway):
        def __init__(self) -> None:
            super().__init__()
            self.gate = _aio.Event()
            self.sent_cloid: str | None = None
            self._fill: tuple[float, float] | None = (0.2, 185.0)  # 응답에 실린 즉시체결

        def new_cloid(self) -> str | None:
            return CLOID

        async def place_order(self, intent: OrderIntent, cloid: str | None = None) -> str:
            self.sent_cloid = cloid
            await self.gate.wait()  # 응답 지연 — 그 사이 웹소켓 통보가 먼저 온다
            return "777"

        def pop_place_fill(self) -> tuple[float, float] | None:
            f, self._fill = self._fill, None
            return f

    hl_gw = SlowHL()
    open_upd = _json.dumps({"channel": "orderUpdates", "data": [
        {"order": {"coin": "xyz:SMSN", "side": "A", "limitPx": "185.0", "sz": "0.0",
                   "oid": 777, "timestamp": 1.0, "origSz": "0.2", "cloid": CLOID},
         "status": "open", "statusTimestamp": 1.0}]})
    hl_fill = _json.dumps({"channel": "userFills", "data": {"fills": [
        {"coin": "xyz:SMSN", "px": "185.0", "sz": "0.2", "side": "A",
         "oid": 777, "tid": 1, "time": 1.0}]}})
    # 실측 순서: 체결(userFills)이 orderUpdates보다 먼저 찍힘 — 체결은 보관됐다가 식별 때 반영
    system = LiveSystem(
        gateway=MockLSGateway(),  # type: ignore[arg-type]
        order_book=OrderBook(), session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=hl_gw, hl_ws=HLWebSocketClient(FakeConnector([hl_fill, open_upd])),
    )
    identified: list[tuple[str, str]] = []
    system.on_hl_identified.append(lambda c, o: identified.append((c, o)))
    intent = OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                         instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.2,
                         order_type=OrderType.LIMIT, price=185.0)
    task = _aio.create_task(system.place(intent))
    for _ in range(3):
        await _aio.sleep(0)  # place가 cloid를 대기 목록에 넣고 응답을 기다리는 상태
    assert hl_gw.sent_cloid == CLOID
    await system.start()
    await system.wait()  # 웹소켓 프레임 소진 — 응답 전 식별
    order = system.order_book.order("777")
    assert order is not None and order.filled_qty == 0.2 and identified == [(CLOID, "777")]
    hl_gw.gate.set()
    assert await task == "777"
    # 응답의 즉시체결 0.2는 이미 반영분과 차이 0 → 이중 반영 없음
    assert system.order_book.order("777").filled_qty == 0.2
    assert system.order_book.position_qty(SAMSUNG, Instrument.HL_PERP) == -0.2


def _failing_hl_system(grace_s: float) -> tuple[LiveSystem, object, str, list[str]]:
    """발주 응답 유실(통신 오류) + cloid 조회 실패를 흉내 내는 HL 게이트웨이로 시스템 조립."""
    from kp_arb.gateways.hl_ws import HLWebSocketClient
    from kp_arb.gateways.mock_hl import MockHLGateway

    CLOID = "0x" + "ef" * 16
    noted: list[str] = []

    class FailHL(MockHLGateway):
        lookup_oid: str | None = None  # 유예 끝 orderStatus(cloid) 조회 결과

        def new_cloid(self) -> str | None:
            return CLOID

        async def place_order(self, intent: OrderIntent, cloid: str | None = None) -> str:
            raise ConnectionError("Connection reset by peer")

        async def lookup_by_cloid(self, cloid: str) -> str | None:
            return self.lookup_oid

        def note_identified(self, oid: str, intent: OrderIntent) -> None:
            noted.append(oid)

    open_upd = json.dumps({"channel": "orderUpdates", "data": [
        {"order": {"coin": "xyz:SMSN", "side": "A", "limitPx": "185.0", "sz": "0.0",
                   "oid": 777, "timestamp": 1.0, "origSz": "0.2", "cloid": CLOID},
         "status": "open", "statusTimestamp": 1.0}]})
    hl_fill = json.dumps({"channel": "userFills", "data": {"fills": [
        {"coin": "xyz:SMSN", "px": "185.0", "sz": "0.2", "side": "A",
         "oid": 777, "tid": 1, "time": 1.0}]}})
    hl_gw = FailHL()
    system = LiveSystem(
        gateway=MockLSGateway(),  # type: ignore[arg-type]
        order_book=OrderBook(), session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=hl_gw, hl_ws=HLWebSocketClient(FakeConnector([hl_fill, open_upd])),
    )
    system.hl_pending_grace_s = grace_s
    return system, hl_gw, CLOID, noted


_HL_INTENT = OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                         instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.2,
                         order_type=OrderType.LIMIT, price=185.0)


async def test_failed_hl_place_adopts_late_cloid_notice_within_grace() -> None:
    # 검토 2026-09-11 §A(예방): 발주가 통신 오류로 끝나고 cloid 조회도 실패했는데 주문은 들어가 있던
    # 경우 — 유예 안에 그 cloid의 통보가 오면 살아 있는 주문으로 등록·체결 반영·알람·훅 통지.
    import asyncio as _aio

    import pytest

    system, _, cloid, noted = _failing_hl_system(grace_s=5.0)
    identified: list[tuple[str, str]] = []
    system.on_hl_identified.append(lambda c, o: identified.append((c, o)))
    with pytest.raises(ConnectionError):
        await system.place(_HL_INTENT)
    assert cloid in system._hl_failed and system._hl_pending == {}
    await system.start()
    await system.wait()  # 유예 안에 orderUpdates(cloid)·userFills 도착
    order = system.order_book.order("777")
    assert order is not None and order.filled_qty == 0.2
    assert identified == [(cloid, "777")] and noted == ["777"] and system.error_seq == 1
    assert cloid not in system._hl_failed
    for t in list(system._bg):
        t.cancel()  # 유예 끝 재조회 작업 정리
    await _aio.sleep(0)


async def test_failed_hl_place_rechecks_by_cloid_at_grace_end() -> None:
    # 웹소켓까지 끊겼으면 통보가 안 온다 — 유예 끝에 orderStatus(cloid)로 마지막 확인해 등록.
    import asyncio as _aio

    import pytest

    system, hl_gw, cloid, noted = _failing_hl_system(grace_s=0.02)
    hl_gw.lookup_oid = "888"
    with pytest.raises(ConnectionError):
        await system.place(_HL_INTENT)
    await _aio.sleep(0.1)  # 유예 지남 → 재조회
    order = system.order_book.order("888")
    assert order is not None and order.intent.qty == 0.2 and noted == ["888"]
    assert system.error_seq == 1 and cloid not in system._hl_failed
    # 조회에도 없으면 안 들어간 것으로 보고 조용히 끝(유예 뒤 통보는 무시)
    system2, _, cloid2, _ = _failing_hl_system(grace_s=0.02)
    with pytest.raises(ConnectionError):
        await system2.place(_HL_INTENT)
    await _aio.sleep(0.1)
    assert cloid2 not in system2._hl_failed and system2.error_seq == 0
    await system2.start()
    await system2.wait()
    assert system2.order_book.order("777") is None  # 유예 지난 통보 — 미아 보관(재동기가 안전망)


async def test_place_routes_hl_to_hl_gateway() -> None:
    from kp_arb.gateways.hl_ws import HLWebSocketClient
    from kp_arb.gateways.mock_hl import MockHLGateway

    hl_gw = MockHLGateway()
    system = LiveSystem(
        gateway=MockLSGateway(),  # type: ignore[arg-type]
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=hl_gw,
        hl_ws=HLWebSocketClient(FakeConnector([])),
    )
    await system.start()
    oid = await system.place(OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                                         instrument=Instrument.HL_PERP, side=Side.SELL,
                                         qty=0.1, order_type=OrderType.MARKET))
    assert oid.startswith("HL-") and len(hl_gw.placed) == 1


async def test_hl_daily_limit_blocks_over_limit() -> None:
    # DESIGN-settings §1 — place(길목)에서 당일 체결액 + 이 주문 금액 > 한도면 거부(수동·전략 공통).
    from kp_arb.gateways.hl_ws import HLWebSocketClient
    from kp_arb.gateways.mock_hl import MockHLGateway
    from kp_arb.limits import DailyLimitExceeded

    system = LiveSystem(
        gateway=MockLSGateway(),  # type: ignore[arg-type]
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=MockHLGateway(),
        hl_ws=HLWebSocketClient(FakeConnector([])),
    )
    await system.start()
    system.set_hl_daily_limit(1000.0)

    def _buy(qty: float) -> OrderIntent:
        return OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
                           instrument=Instrument.HL_PERP, side=Side.BUY, qty=qty,
                           order_type=OrderType.LIMIT, price=1500.0)

    assert await system.place(_buy(0.5))          # 0 + 750 ≤ 1000 → 통과
    with pytest.raises(DailyLimitExceeded):
        await system.place(_buy(1.0))             # 0 + 1500 > 1000 → 거부
    system.set_hl_daily_limit(0.0)                # 0 = 무제한
    assert await system.place(_buy(1.0))          # 이제 통과
    await system.wait()


async def test_amend_price_forbids_hl() -> None:
    # HL은 어떤 경우에도 정정 금지 — amend_price(유일 정정 라우팅)가 하드 거부한다.
    from kp_arb.gateways.hl_ws import HLWebSocketClient
    from kp_arb.gateways.mock_hl import MockHLGateway

    hl_gw = MockHLGateway()
    system = LiveSystem(
        gateway=MockLSGateway(),  # type: ignore[arg-type]
        order_book=OrderBook(),
        session=SessionService(),
        stock_ws=LSWebSocketClient(FakeConnector([])),
        hl_gateway=hl_gw,
        hl_ws=HLWebSocketClient(FakeConnector([])),
    )
    await system.start()
    oid = await system.place(OrderIntent(venue=Venue.HYPERLIQUID, underlying=SAMSUNG,
        instrument=Instrument.HL_PERP, side=Side.SELL, qty=0.1,
        order_type=OrderType.LIMIT, price=100.0))
    with pytest.raises(HLAmendForbidden):
        await system.amend_price(oid, 101.0)
    await system.wait()


async def test_attach_engine_uses_realtime_positions_and_place() -> None:
    # 엔진 연결: 포지션=OrderBook 실시간 값, 주문=place(등록 포함), 시세 콜백 연결.
    from collections.abc import Sequence as _Seq

    from kp_arb.domain.models import MarketState
    from kp_arb.strategy.base import Strategy

    captured: list[MarketState] = []

    class OneShotBuy(Strategy):
        """첫 호출에만 LS 매수 1건 — 이후 재주문 없음(중복 방지 확인용 아님, 단순화)."""

        def __init__(self) -> None:
            self.fired = False

        def evaluate(self, state: MarketState) -> _Seq[OrderIntent]:
            captured.append(state)
            if self.fired or state.underlying is not SAMSUNG:
                return []
            self.fired = True
            return [OrderIntent(venue=Venue.LS, underlying=SAMSUNG,
                                instrument=Instrument.KR_STOCK, side=Side.BUY,
                                qty=10, order_type=OrderType.MARKET)]

    system, _, _ = _system([_fill_frame("LS-1")])
    engine = system.attach_engine(OneShotBuy())
    await system.start()          # 일괄 조회: 삼성 100주 → OrderBook
    await system.place(OrderIntent(venue=Venue.LS, underlying=SAMSUNG,
                                   instrument=Instrument.KR_STOCK, side=Side.BUY,
                                   qty=10, order_type=OrderType.MARKET))  # "LS-1" 등록
    await system.wait()           # 체결 프레임 반영 → 110주

    await system.run_strategy_loop(engine, interval_s=0.0, max_cycles=1)

    # 엔진이 받은 MarketState의 포지션 = OrderBook 실시간 값(110주)
    samsung_states = [s for s in captured if s.underlying is SAMSUNG]
    assert samsung_states and samsung_states[0].positions[0].qty == 110
    # 전략 주문이 place 경유 → OrderBook에 자동 등록됨 ("LS-2")
    assert system.order_book.order("LS-2") is not None
    # 리스크 상태가 OrderBook의 "실시간" 잔고를 참조 — 체결(10주×70,000)이 즉시 차감됨
    assert engine.risk_state.account_available_funds[Account.KR_STOCK] == 5_000_000 - 700_000


async def test_strategy_loop_noop_places_nothing() -> None:
    from kp_arb.strategy.noop import NoopStrategy

    system, _, _ = _system([])
    engine = system.attach_engine(NoopStrategy())
    await system.start()
    await system.wait()
    await system.run_strategy_loop(engine, interval_s=0.0, max_cycles=3)
    assert system.order_book.open_orders() == []  # 주문 0건


async def test_deriv_ws_subscribes_futures_fills_only() -> None:
    system, _, deriv_connector = _system([], deriv_frames=[])
    await system.start()
    await system.wait()
    assert deriv_connector is not None
    trs = {json.loads(m)["body"]["tr_cd"] for m in deriv_connector.conn.sent}
    assert trs == {"O01", "C01", "H01"}  # 파생 WS는 선물 통보만
    types = {json.loads(m)["header"]["tr_type"] for m in deriv_connector.conn.sent}
    assert types == {"1"}  # 계좌 등록

def test_usdkrw_effective_spot_window() -> None:
    # 주간 창(07:50~18:10) 안이고 외환현물이 있으면 현물, 아니면 선물이론가.
    from datetime import datetime

    system, _, _ = _system([])
    system.usdkrw_theory = 1_500.0
    day = datetime(2026, 7, 20, 10, 0)
    assert system.usdkrw_effective(day) == (1_500.0, "선물이론")  # 현물 미수신 → 이론가
    system.usdkrw_spot = 1_498.5
    assert system.usdkrw_effective(datetime(2026, 7, 20, 7, 50)) == (1_498.5, "현물")
    assert system.usdkrw_effective(day) == (1_498.5, "현물")
    assert system.usdkrw_effective(datetime(2026, 7, 20, 18, 10)) == (1_500.0, "선물이론")


def test_disparity_board_computes_pairs() -> None:
    # DESIGN §6.1: HL 환산 disp vs 국내(SF/ETF) disp → 진입/청산 스프레드.
    from kp_arb.domain.enums import SessionPhase
    from kp_arb.domain.models import Quote
    from kp_arb.etf_theory import EtfTheoryInputs

    system, _, _ = _system([])
    system.futures_symbols[SAMSUNG] = "A1167000"
    system.futures_expiry[(SAMSUNG, Instrument.KR_STOCK_FUTURE)] = 202612  # 먼 만기 — 테스트 안정성
    system.etf_symbols[SAMSUNG] = "0193W0"
    system.usdkrw_theory = 1_500.0
    system.trades[(SAMSUNG, Instrument.KR_STOCK, "krx")] = 300_000.0  # 기초 현재가
    system.stock_change_pct[(SAMSUNG, "krx")] = 0.0  # 기초 등락률(drate) 0%
    system.session.seed_phase(SessionPhase.REGULAR)  # 정규장 공식 사용
    system.etf_theory[SAMSUNG] = EtfTheoryInputs(prev_nav=20_000.0, leverage=2.0)
    system.quotes[(SAMSUNG, Instrument.HL_PERP, "hl")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.HL_PERP,
        bid=201.0, ask=202.0, ts=0.0, market="hl",
    )
    system.quotes[(SAMSUNG, Instrument.KR_STOCK_FUTURE, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        bid=301_000.0, ask=302_000.0, ts=0.0,
    )
    system.quotes[(SAMSUNG, Instrument.KR_ETF, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_ETF,
        bid=20_000.0, ask=20_050.0, ts=0.0,
    )
    system.quotes[(SAMSUNG, Instrument.KR_STOCK, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
        bid=299_500.0, ask=300_500.0, ts=0.0,
    )

    board = system.disparity_board(1)  # 1호가만 있어 est=1호가로 축약(수치 동일)

    sf = board[(SAMSUNG, Instrument.KR_STOCK_FUTURE)]
    # HL 환산: bid 301,500 / ask 303,000, 기초 300,000 → disp +0.5% / +1.0%
    assert sf.hl.bid is not None and abs(sf.hl.bid - 0.005) < 1e-9
    assert sf.hl.ask is not None and abs(sf.hl.ask - 0.010) < 1e-9
    # SF 이론가 = 300,000 × (1 + 3.5% × 잔존일/365) > 300,000 → disp는 그 대비
    assert sf.kr.bid is not None and sf.spread.entry is not None
    # 국내 maker 기준(meme.xlsx): 진입 = HL매수d − 국내매수d / 청산 = HL매도d − 국내매도d
    assert sf.spread.entry == sf.hl.bid - sf.kr.bid
    assert sf.spread.exit == (sf.hl.ask or 0) - (sf.kr.ask or 0)

    etf = board[(SAMSUNG, Instrument.KR_ETF)]
    # ETF 이론가 = 20,000(기초 등락률 0) → ask 20,050 disp +0.25% (인프라 유지 확인용)
    assert etf.kr.ask is not None and abs(etf.kr.ask - 0.0025) < 1e-9
    assert etf.spread.exit == (etf.hl.ask or 0) - (etf.kr.ask or 0)

    st = board[(SAMSUNG, Instrument.KR_STOCK)]
    # 주식 쌍: 기준가 = 자기 현재가 300,000 (이론가 없음 — 옛 엑셀 현대차 AE62 패턴)
    assert st.kr.bid is not None and abs(st.kr.bid - (-500 / 300_000)) < 1e-12
    assert st.kr.ask is not None and abs(st.kr.ask - (500 / 300_000)) < 1e-12
    assert st.spread.entry == st.hl.bid - st.kr.bid  # 진입 공식 동일 (maker 기준)
    assert st.kr_last is not None and abs(st.kr_last) < 1e-12  # 현재가 괴리는 항상 0


def test_disparity_board_est_is_quantity_dependent() -> None:
    # 신호 = 발주 수량 기준 est-price. 큰 수량은 깊은 호가를 먹어 스프레드가 달라진다(§6.1-2).
    from kp_arb.domain.enums import SessionPhase
    from kp_arb.domain.models import Quote

    system, _, _ = _system([])
    system.futures_symbols[SAMSUNG] = "A1167000"
    system.futures_expiry[(SAMSUNG, Instrument.KR_STOCK_FUTURE)] = 202612
    system.usdkrw_theory = 1_500.0
    system.trades[(SAMSUNG, Instrument.KR_STOCK, "krx")] = 300_000.0
    system.stock_change_pct[(SAMSUNG, "krx")] = 0.0
    system.session.seed_phase(SessionPhase.REGULAR)
    # 1호가는 얕고(잔량 1), 2호가는 깊다(잔량 100) — 큰 수량이면 2호가까지 먹는다.
    system.quotes[(SAMSUNG, Instrument.HL_PERP, "hl")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.HL_PERP, bid=201.0, ask=202.0,
        ts=0.0, market="hl", bids=[(201.0, 1.0), (200.0, 100.0)],
        asks=[(202.0, 1.0), (203.0, 100.0)])
    system.quotes[(SAMSUNG, Instrument.KR_STOCK_FUTURE, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        bid=301_000.0, ask=302_000.0, ts=0.0,
        bids=[(301_000.0, 1.0), (300_000.0, 100.0)],
        asks=[(302_000.0, 1.0), (303_000.0, 100.0)])

    small = system.disparity_board(1)[(SAMSUNG, Instrument.KR_STOCK_FUTURE)]
    big = system.disparity_board(50)[(SAMSUNG, Instrument.KR_STOCK_FUTURE)]
    assert small.spread.entry is not None and big.spread.entry is not None
    assert small.spread.entry != big.spread.entry   # 수량 종속 = est 반영 증거
    assert small.spread.exit != big.spread.exit


def test_pair_signal_est_based() -> None:
    import pytest

    # 7-3a: 진입 = HL매수d(est) − 국내매수d / 청산 = HL매도d(est) − 국내매도d.
    # 주식 쌍(기준가=자기 현재가 300,000, 환율 1,500)으로 검산.
    from kp_arb.domain.models import Quote

    system, _, _ = _system([])
    system.usdkrw_theory = 1_500.0
    system.trades[(SAMSUNG, Instrument.KR_STOCK, "krx")] = 300_000.0
    system.quotes[(SAMSUNG, Instrument.KR_STOCK, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
        bid=299_500.0, ask=300_500.0, ts=0.0)
    system.quotes[(SAMSUNG, Instrument.HL_PERP, "hl")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.HL_PERP,
        bid=201.0, ask=202.0, ts=0.0, market="hl",
        bids=[(201.0, 3.0), (200.0, 100.0)],
        asks=[(202.0, 3.0), (203.0, 100.0)])

    entry, exit_ = system.pair_signal(SAMSUNG, Instrument.KR_STOCK, 5, 5)
    # est(매수쪽, 5계약) = (201×3 + 200×2)/5 = 200.6 → 환산 300,900 → HL disp +0.003
    # 국내 매수d = (299,500−300,000)/300,000 = −1/600 → entry = 0.003 + 1/600
    assert entry == pytest.approx(0.003 + 1 / 600)
    # est(매도쪽) = (202×3 + 203×2)/5 = 202.4 → 303,600 → +0.012, 국내 매도d = +1/600
    assert exit_ == pytest.approx(0.012 - 1 / 600)

    # 수량이 커지면 est가 나빠져 진입 신호는 줄어든다 (2호가까지 파고듦)
    entry_big, _ = system.pair_signal(SAMSUNG, Instrument.KR_STOCK, 50, 50)
    assert entry_big is not None and entry is not None and entry_big < entry


async def test_fx_auction_places_hedge_on_new_stock_future() -> None:
    # 원달러선물 동시호가 대응: 삼성 주식선물 신규주문 접수(O01) → KR_FX 대응주문 발주.
    from kp_arb.fx_auction import FxAuctionSettings
    o01 = json.dumps({"header": {"tr_cd": "O01"}, "body": {
        "fnoIsuno": "A1169000", "bnstp": "2", "ordqty": "20",
        "ordprc": "142150", "ordno": "2224", "orgordno": "0"}})
    system, _, _ = _system([], deriv_frames=[o01])
    system.futures_symbols = {SAMSUNG: "A1169000"}  # 코드→종목 매칭용
    system.start_fx_auction(FxAuctionSettings(
        windows=(("00:00", "23:59"),), fx_code="175X9000",  # 항상 시간창 안
        price=1421.5, tick=10, hedge_ratio=0.5))
    await system.start()
    await system.wait()
    if system._bg:  # 백그라운드 발주 태스크 완료 대기
        await asyncio.gather(*list(system._bg))
    # 삼성 매수 20계약 @142150 → 원달러선물 매도 1계약 @1420.5
    assert system._gw.fx_placed == [("175X9000", Side.SELL, 1, 1420.5)]
    assert system.fx_hedges and system.fx_hedges[0]["status"] == "접수"


async def test_fx_auction_ignores_when_stopped_or_amend() -> None:
    # 실행 안 함 + 정정(orgordno≠0)이면 대응 안 함.
    new = json.dumps({"header": {"tr_cd": "O01"}, "body": {
        "fnoIsuno": "A1169000", "bnstp": "2", "ordqty": "20",
        "ordprc": "142150", "ordno": "2224", "orgordno": "0"}})
    amend = json.dumps({"header": {"tr_cd": "O01"}, "body": {
        "fnoIsuno": "A1169000", "bnstp": "2", "ordqty": "20",
        "ordprc": "142150", "ordno": "2232", "orgordno": "2224"}})  # 정정
    system, _, _ = _system([], deriv_frames=[new, amend])
    system.futures_symbols = {SAMSUNG: "A1169000"}
    # start_fx_auction 호출 안 함 → 실행중 아님
    await system.start()
    await system.wait()
    if system._bg:
        await asyncio.gather(*list(system._bg))
    assert system._gw.fx_placed == []  # 미실행이라 대응 없음


def test_set_carry_rates_replaces_theory_rates() -> None:
    # 공통설정 이자율 주입 — 환율(fx)·주식선물(eq) 연이자율이 이론가 계산에 반영된다.
    system, _, _ = _system([])
    system.set_carry_rates(fx=0.02, eq=0.04)
    assert system._carry.fx == 0.02
    assert system._carry.stock_futures == 0.04


def test_monitor_snapshot_structure() -> None:
    # 시세 모니터 스냅샷 — 코어가 LS/HL 표 + 괴리보드 + 환율·잔고·장운영을 조립(모니터는 렌더만).
    from kp_arb.core_server import monitor_snapshot

    assert monitor_snapshot(None, 1, 0.0, 0.0) == {"connected": False}
    system, _, _ = _system([])
    snap = monitor_snapshot(system, 1, 0.0, 0.0)
    assert snap["connected"] is True
    assert set(snap) >= {"fx", "phase", "balances", "ls", "hl", "board"}
    assert all("theory" in r and "disp" in r for r in snap["ls"])   # LS 행: 이론가·괴리
    assert all("mark" in r and "oracle" in r for r in snap["hl"])   # HL 행: 마크·오라클
    # 시세 화면 HL 호가단위 콤보용 — 종목별 적용값 + 숫자 틱 표(가격 미수신이면 빈 목록)
    assert set(snap["hl_merge"]) == {u.value for u in Underlying}
    assert set(snap["hl_merge_ticks"]) == {u.value for u in Underlying}
    assert all(v == [] for v in snap["hl_merge_ticks"].values())  # HL 시세 없음 → 빈 표


def test_monitor_snapshot_merges_krx_nxt() -> None:
    # HTS처럼 통합: 매수는 높은 쪽(NXT), 매도는 낮은 쪽(KRX)을 코어가 골라 내려준다.
    from kp_arb.core_server import monitor_snapshot
    from kp_arb.domain.models import Quote

    system, _, _ = _system([])
    system.quotes[(SAMSUNG, Instrument.KR_STOCK, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
        bid=292_500, ask=293_000, ts=1.0, bid_qty=100, ask_qty=50, market="krx")
    system.quotes[(SAMSUNG, Instrument.KR_STOCK, "nxt")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK,
        bid=292_550, ask=293_050, ts=1.0, bid_qty=30, ask_qty=20, market="nxt")

    stock = next(r for r in monitor_snapshot(system, 1, 0.0, 0.0)["ls"]
                 if r["underlying"] == "samsung" and r["instrument"] == "kr_stock")
    assert stock["ask"] == 293_000 and stock["ask_qty"] == 50   # 매도: KRX가 낮음
    assert stock["bid"] == 292_550 and stock["bid_qty"] == 30   # 매수: NXT가 높음


def test_spread_csv_rows() -> None:
    # 코어가 상시 기록하는 괴리 분포 CSV — 진입/청산이 있는 쌍만, 13열 고정.
    from kp_arb.domain.enums import SessionPhase
    from kp_arb.domain.models import Quote
    from kp_arb.etf_theory import EtfTheoryInputs

    system, _, _ = _system([])
    assert system._spread_csv_rows("09:00:00") == []  # 데이터 없으면 빈 목록

    system.futures_symbols[SAMSUNG] = "A1167000"
    system.futures_expiry[(SAMSUNG, Instrument.KR_STOCK_FUTURE)] = 202612
    system.usdkrw_theory = 1_500.0
    system.trades[(SAMSUNG, Instrument.KR_STOCK, "krx")] = 300_000.0
    system.stock_change_pct[(SAMSUNG, "krx")] = 0.0
    system.session.seed_phase(SessionPhase.REGULAR)
    system.etf_theory[SAMSUNG] = EtfTheoryInputs(prev_nav=20_000.0, leverage=2.0)
    system.quotes[(SAMSUNG, Instrument.HL_PERP, "hl")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.HL_PERP,
        bid=201.0, ask=202.0, ts=0.0, market="hl")
    system.quotes[(SAMSUNG, Instrument.KR_STOCK_FUTURE, "krx")] = Quote(
        underlying=SAMSUNG, instrument=Instrument.KR_STOCK_FUTURE,
        bid=301_000.0, ask=302_000.0, ts=0.0)

    rows = system._spread_csv_rows("09:00:00")
    assert rows, "진입/청산이 있으면 최소 한 줄"
    sf = next(r for r in rows if r[2] == "SF")
    assert sf[0] == "09:00:00" and sf[1] == SAMSUNG.value  # 시각·기초
    assert len(sf) == 13  # time..kr_last_d 13열
