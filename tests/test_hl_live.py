"""HLSdkGateway 계약 테스트 — SDK 스텁 주입(실 네트워크·실 키 없음)."""
from __future__ import annotations

from typing import Any

import pytest

from kp_arb.domain.enums import Instrument, OrderType, Side, Underlying, Venue
from kp_arb.domain.models import OrderIntent
from kp_arb.gateways.hl import HLError
from kp_arb.gateways.hl_live import HL_SYMBOLS, HLSdkGateway

ADDR = "0x" + "a" * 40

META_CTXS = [
    {"universe": [
        {"name": "xyz:SMSN", "szDecimals": 3},
        {"name": "xyz:SKHX", "szDecimals": 3},
        {"name": "xyz:HYUNDAI", "szDecimals": 3},
    ]},
    [
        {"markPx": "184.1", "funding": "0.0001841299"},
        {"markPx": "1434.4", "funding": "0.0004326268"},
        {"markPx": "312.59", "funding": "0.0003316256"},
    ],
]


class StubExchange:
    def __init__(self) -> None:
        self.orders: list[tuple[Any, ...]] = []
        self.cancels: list[tuple[str, int]] = []
        # nonce 직렬화 검증용 — SDK처럼 호출 시각(ms)을 찍고, 동시 진행 수를 센다
        self.nonces: list[int] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.nonce_reject_times = 0   # 처음 n번은 "duplicate nonce" 거부 흉내
        self.reject_response: str | None = None  # 지정하면 그 사유로 항상 거부(nonce 아님)

    def _enter(self) -> None:
        import threading
        import time

        self.nonces.append(int(time.time() * 1000))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(0.003)  # 서버 왕복 흉내 — 겹치면 max_in_flight가 2 이상이 된다
        self._thread = threading.current_thread().name

    def _exit(self) -> None:
        self.in_flight -= 1

    def order(self, coin: str, is_buy: bool, sz: float, px: float,
              order_type: dict[str, Any], reduce_only: bool = False,
              cloid: Any = None) -> dict[str, Any]:
        self.cloids: list[Any] = getattr(self, "cloids", [])
        self.cloids.append(cloid)  # SDK Cloid 객체(없으면 None)
        if getattr(self, "raise_transport", False):  # 응답 유실(통신 오류) 흉내
            raise ConnectionError("Connection reset by peer")
        self._enter()
        try:
            return self._order(coin, is_buy, sz, px, order_type, reduce_only)
        finally:
            self._exit()

    def _order(self, coin: str, is_buy: bool, sz: float, px: float,
               order_type: dict[str, Any], reduce_only: bool) -> dict[str, Any]:
        self.orders.append((coin, is_buy, sz, px, order_type))
        self.last_reduce_only = reduce_only
        if self.reject_response is not None:
            return {"status": "err", "response": self.reject_response}
        if self.nonce_reject_times > 0:
            self.nonce_reject_times -= 1
            return {"status": "err", "response": "Invalid nonce: duplicate nonce 1789002894144"}
        if getattr(self, "fill_on_place", False):  # 발주 즉시체결(크로싱) 흉내
            statuses: list[dict[str, Any]] = [
                {"filled": {"totalSz": str(sz), "avgPx": "168.23", "oid": 485478010353}}]
        else:
            statuses = [{"resting": {"oid": 485478010353}}]
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}

    def cancel(self, coin: str, oid: int) -> dict[str, Any]:
        self._enter()
        try:
            self.cancels.append((coin, oid))
            return {"status": "ok",
                    "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        finally:
            self._exit()

    def bulk_cancel(self, reqs: list[dict[str, Any]]) -> dict[str, Any]:
        """여러 건 한 요청 — statuses는 건별 "success" 또는 {"error": …}(bulk_fail_oids)."""
        self._enter()
        try:
            self.bulk_calls: list[list[tuple[str, int]]] = getattr(self, "bulk_calls", [])
            self.bulk_calls.append([(r["coin"], r["oid"]) for r in reqs])
            fails = getattr(self, "bulk_fail_oids", set())
            statuses: list[Any] = [
                {"error": f"Order {r['oid']} was never placed"} if r["oid"] in fails
                else "success" for r in reqs]
            return {"status": "ok",
                    "response": {"type": "cancel", "data": {"statuses": statuses}}}
        finally:
            self._exit()

    def bulk_orders(self, reqs: list[dict[str, Any]]) -> dict[str, Any]:
        """여러 건 한 요청 — statuses는 보낸 순서대로 건별. bulk_status: {가격: 그 주문의 상태}
        (없으면 resting), bulk_whole_error: 요청 전체 거부 문구, bulk_raise: 통신 오류 흉내."""
        self.bulk_orders_calls: list[list[dict[str, Any]]] = getattr(
            self, "bulk_orders_calls", [])
        self.bulk_orders_calls.append([dict(r) for r in reqs])
        if getattr(self, "bulk_raise", False):
            raise ConnectionError("Connection reset by peer")
        self._enter()
        try:
            whole = getattr(self, "bulk_whole_error", None)
            if whole:
                return {"status": "err", "response": whole}
            custom: dict[float, dict[str, Any]] = getattr(self, "bulk_status", {})
            statuses = [custom.get(r["limit_px"], {"resting": {"oid": 9000 + i}})
                        for i, r in enumerate(reqs)]
            return {"status": "ok",
                    "response": {"type": "order", "data": {"statuses": statuses}}}
        finally:
            self._exit()

    def update_leverage(self, leverage: int, name: str, is_cross: bool) -> dict[str, Any]:
        self.leverage_calls: list[tuple[int, str, bool]] = getattr(self, "leverage_calls", [])
        self.leverage_calls.append((leverage, name, is_cross))
        if leverage > 20:  # 상한 초과 거부 흉내
            return {"status": "err", "response": "Invalid leverage"}
        return {"status": "ok", "response": {"type": "default"}}

    def modify_order(self, oid: int, coin: str, is_buy: bool, sz: float, px: float,
                     order_type: dict[str, Any], reduce_only: bool = False) -> dict[str, Any]:
        self.modifies: list[tuple[Any, ...]] = getattr(self, "modifies", [])
        self.modifies.append((oid, coin, is_buy, sz, px, reduce_only, order_type))
        if getattr(self, "cross_reject", False):  # HL always_place=false 크로싱 거부 흉내
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
                {"error": "Post only order would have immediately matched"}]}}}
        statuses = [{"resting": {"oid": oid + 1}}]
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}


class StubInfo:
    """실측 shape 픽스처를 돌려주는 /info 스텁."""

    def __init__(self, positions: list[dict[str, Any]] | None = None,
                 account_value: str = "19.6",
                 active_data: dict[str, Any] | None = None,
                 open_orders: list[dict[str, Any]] | None = None) -> None:
        self._positions = positions or []
        self._account_value = account_value
        self._active = active_data or {}  # coin -> activeAssetData 응답
        self._open_orders = open_orders or []  # frontendOpenOrders 행
        self.posts: list[dict[str, Any]] = []

    def post(self, path: str, body: dict[str, Any]) -> Any:
        self.posts.append(body)
        if body["type"] == "clearinghouseState":
            assert body["dex"] == "xyz"  # dex 스코프 필수
            return {"marginSummary": {"accountValue": self._account_value},
                    "assetPositions": self._positions}
        if body["type"] == "metaAndAssetCtxs":
            assert body["dex"] == "xyz"
            return META_CTXS
        if body["type"] == "activeAssetData":
            return self._active.get(body["coin"], {})  # 미설정 코인 → 빈 응답
        if body["type"] == "frontendOpenOrders":
            assert body["dex"] == "xyz"
            return self._open_orders
        if body["type"] == "orderStatus":  # 공식: oid 자리에 cloid(16바이트 hex) 가능
            return getattr(self, "order_status", {"status": "unknownOid"})
        raise AssertionError(f"unexpected info type {body['type']}")


def _gw(info: StubInfo | None = None) -> tuple[HLSdkGateway, StubExchange, StubInfo]:
    ex, inf = StubExchange(), info or StubInfo()
    return HLSdkGateway(ex, inf, account_address=ADDR), ex, inf


def _intent(side: Side = Side.SELL, *, order_type: OrderType = OrderType.LIMIT,
            price: float | None = 180.0) -> OrderIntent:
    return OrderIntent(venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
                       instrument=Instrument.HL_PERP, side=side, qty=0.1,
                       order_type=order_type, price=price)


async def test_limit_order_uses_dex_symbol_and_parses_oid() -> None:
    gw, ex, _ = _gw()
    oid = await gw.place_order(_intent())
    assert oid == "485478010353"
    coin, is_buy, sz, px, otype = ex.orders[0]
    assert coin == "xyz:SMSN"  # 실측 심볼(SAMSUNG 아님)
    assert is_buy is False and sz == 0.1 and px == 180.0
    assert otype == {"limit": {"tif": "Gtc"}}


async def test_place_sends_cloid_when_given() -> None:
    # DESIGN §HL cloid: 우리가 만든 16바이트 hex를 주문에 실어 보낸다(SDK Cloid). 없으면 안 실음.
    gw, ex, _ = _gw()
    cloid = gw.new_cloid()
    assert cloid is not None and cloid.startswith("0x") and len(cloid) == 34
    assert gw.new_cloid() != cloid  # 난수부 — 같은 ms에도 겹치지 않음
    oid = await gw.place_order(_intent(), cloid=cloid)
    assert oid == "485478010353" and ex.cloids[-1].to_raw() == cloid
    await gw.place_order(_intent())
    assert ex.cloids[-1] is None


async def test_place_recovers_by_cloid_when_response_is_lost() -> None:
    # 통신 오류로 응답을 못 받으면 orderStatus(cloid)로 들어간 주문인지 확인 — 들어갔으면 oid를
    # 돌려 정상 발주로 잇는다(재발주 중복 방지). 거래소 거부(HLError)는 조회 없이 그대로 거부.
    gw, ex, inf = _gw()
    ex.raise_transport = True
    cloid = gw.new_cloid()
    inf.order_status = {"status": "order", "order": {
        "order": {"coin": "xyz:SMSN", "side": "A", "limitPx": "180.0", "sz": "0.1",
                  "oid": 485478010353, "timestamp": 1789084276518, "origSz": "0.1",
                  "cloid": cloid},
        "status": "open", "statusTimestamp": 1789084276518}}
    oid = await gw.place_order(_intent(), cloid=cloid)
    assert oid == "485478010353"
    assert inf.posts[-1] == {"type": "orderStatus", "user": ADDR, "oid": cloid}
    assert gw.pop_place_fill() is None      # 체결은 userFills가 전담
    await gw.cancel_order(oid)              # 취소 문맥(coin)도 채워짐
    assert ex.cancels[-1] == ("xyz:SMSN", 485478010353)
    # 조회에도 없으면(unknownOid) 원래 통신 오류를 그대로 올린다
    inf.order_status = {"status": "unknownOid"}
    with pytest.raises(ConnectionError):
        await gw.place_order(_intent(), cloid=gw.new_cloid())
    # cloid 없이 낸 주문은 조회할 수 없어 바로 오류
    with pytest.raises(ConnectionError):
        await gw.place_order(_intent())


async def test_lookup_by_cloid_and_note_identified() -> None:
    # 발주 실패 유예 끝 재조회(lookup_by_cloid)와 응답 전 식별 주문의 취소 문맥 선등록
    # (note_identified)
    gw, ex, inf = _gw()
    cloid = gw.new_cloid()
    assert await gw.lookup_by_cloid(cloid) is None            # unknownOid
    inf.order_status = {"status": "order", "order": {
        "order": {"coin": "xyz:SMSN", "side": "A", "limitPx": "180.0", "sz": "0.1",
                  "oid": 485478010353, "origSz": "0.1", "cloid": cloid}, "status": "open"}}
    assert await gw.lookup_by_cloid(cloid) == "485478010353"
    gw.note_identified("999", _intent(Side.SELL, price=180.0))
    await gw.cancel_order("999")                              # 문맥이 있어 취소 가능
    assert ex.cancels[-1] == ("xyz:SMSN", 999)


async def test_place_immediate_fill_exposed_via_pop() -> None:
    # 발주 즉시체결(응답 filled)이면 (체결수량, 평균가)를 pop_place_fill로 1회 노출한다
    # — place()가 이걸 OrderBook에 반영해 미체결로 안 남게 한다.
    gw, ex, _ = _gw()
    ex.fill_on_place = True
    await gw.place_order(_intent(Side.SELL, price=165.0))  # qty 0.1
    assert gw.pop_place_fill() == (0.1, 168.23)  # (수량, 평균가)
    assert gw.pop_place_fill() is None            # 1회 소비


async def test_place_resting_has_no_place_fill() -> None:
    gw, _, _ = _gw()
    await gw.place_order(_intent(Side.SELL, price=180.0))  # resting(미체결)
    assert gw.pop_place_fill() is None


async def test_market_or_priceless_order_is_rejected() -> None:
    # 사용자 확정 2026-09-04·재확인 09-14: HL 주문은 지정가만. 옛 시장가→IOC(마크±1%) 경로 삭제.
    gw, ex, _ = _gw()
    # (지정가인데 가격 없음은 OrderIntent 모델 검증에서 먼저 막힌다 — 게이트웨이까지 못 온다.)
    with pytest.raises(HLError):
        await gw.place_order(_intent(Side.BUY, order_type=OrderType.MARKET, price=None))
    assert ex.orders == []  # 거래소로 나간 주문 없음


async def test_cancel_requires_tracked_coin() -> None:
    gw, ex, _ = _gw()
    oid = await gw.place_order(_intent())
    await gw.cancel_order(oid)
    assert ex.cancels == [("xyz:SMSN", 485478010353)]
    with pytest.raises(HLError):
        await gw.cancel_order("999")  # 미지 주문 — coin을 모름


async def test_cancel_orders_bulk_one_request_with_per_order_status() -> None:
    # 일괄 취소(사용자 2026-09-28): 여러 건을 cancel 액션 하나(cancels 배열)로 — 요청 1번.
    # 건별 결과는 statuses 순서대로, coin을 모르는 번호는 보내지 않고 사유만 돌려준다.
    gw, ex, _ = _gw()
    oid = await gw.place_order(_intent())
    res = await gw.cancel_orders([oid, "999"])
    assert ex.bulk_calls == [[("xyz:SMSN", 485478010353)]]  # 한 요청, 미지 주문은 제외
    assert res[0] is None and res[1] is not None and "999" in res[1]
    # 건별 거부 — 그 자리에 사유, 요청은 여전히 한 번
    ex.bulk_fail_oids = {485478010353}
    res2 = await gw.cancel_orders([oid])
    assert len(ex.bulk_calls) == 2 and res2[0] is not None and "never placed" in res2[0]
    assert await gw.cancel_orders([]) == []  # 빈 목록은 요청 없음
    assert len(ex.bulk_calls) == 2


def _alo(price: float, side: Side = Side.SELL) -> OrderIntent:
    return OrderIntent(venue=Venue.HYPERLIQUID, underlying=Underlying.SAMSUNG,
                       instrument=Instrument.HL_PERP, side=side, qty=0.1,
                       order_type=OrderType.LIMIT, price=price, post_only=True)


async def test_place_orders_one_request_and_per_order_results() -> None:
    # 묶음 발주(exec §7D, 사용자 2026-09-30): ALO 세 건이 요청 하나(orders 배열)로, 결과는 보낸
    # 순서대로 건별 — 접수·즉시체결·주문별 거부(ALO 겹침)가 한 응답에 섞여 온다.
    gw, ex, _ = _gw()
    ex.bulk_status = {
        181.0: {"filled": {"totalSz": "0.1", "avgPx": "181.2", "oid": 7002}},
        182.0: {"error": "Post only order would have immediately matched, bbo was 1@2."},
    }
    cl = "0x" + "1" * 32
    res = await gw.place_orders([_alo(180.0), _alo(181.0), _alo(182.0)], [cl, None, None])
    assert len(ex.bulk_orders_calls) == 1 and not ex.orders      # 요청 하나, 한 건 발주 없음
    sent = ex.bulk_orders_calls[0]
    assert [r["limit_px"] for r in sent] == [180.0, 181.0, 182.0]
    assert all(r["order_type"] == {"limit": {"tif": "Alo"}} for r in sent)
    assert "cloid" in sent[0] and "cloid" not in sent[1]
    assert [r.order_id for r in res] == ["9000", "7002", None]
    assert res[1].fill == (0.1, 181.2) and res[0].fill is None
    assert res[2].error is not None and "immediately matched" in res[2].error
    assert not res[2].whole                                       # 주문별 거부
    await gw.cancel_order("9000")                                 # 접수된 주문은 취소 문맥이 있다
    assert ex.cancels == [("xyz:SMSN", 9000)]


async def test_place_orders_whole_reject_and_lost_response() -> None:
    # 요청 전체 거부 → 전부 whole(누구 탓인지 모름). 응답 유실은 자동 재전송 없이 cloid로 접수
    # 여부만 조회 — 들어간 주문은 접수로 잇고, 못 찾은 주문은 결과 모름(whole).
    gw, ex, _ = _gw()
    ex.bulk_whole_error = "Insufficient margin to place order."
    res = await gw.place_orders([_alo(180.0), _alo(181.0)])
    assert all(r.order_id is None and r.whole for r in res)
    assert res[0].error is not None and "Insufficient margin" in res[0].error
    assert len(ex.bulk_orders_calls) == 1                          # 다시 보내지 않는다

    info = StubInfo()
    info.order_status = {"status": "order", "order": {"order": {"oid": 5150}}}
    gw2, ex2, _ = _gw(info)
    ex2.bulk_raise = True
    cl = "0x" + "2" * 32
    res2 = await gw2.place_orders([_alo(180.0), _alo(181.0)], [cl, None])
    assert len(ex2.bulk_orders_calls) == 1                         # 재전송 없음
    assert res2[0].order_id == "5150"                              # cloid 조회로 접수 확인
    assert res2[1].order_id is None and res2[1].whole              # cloid 없는 건은 결과 모름
    assert res2[1].error is not None and "ConnectionError" in res2[1].error


async def test_place_orders_splits_alo_and_gtc_and_skips_invalid() -> None:
    # 공식 문서: ALO만 든 묶음을 검증자가 먼저 처리 → ALO와 GTC는 따로 묶는다. 지정가가 아닌
    # 주문은 보내지 않고 그 자리에 사유.
    gw, ex, _ = _gw()
    gtc = _intent(price=183.0)                                     # post_only 아님
    bad = _intent(order_type=OrderType.MARKET, price=None)
    res = await gw.place_orders([_alo(180.0), gtc, bad, _alo(181.0)])
    tifs = sorted(
        (call[0]["order_type"]["limit"]["tif"], [r["limit_px"] for r in call])
        for call in ex.bulk_orders_calls)
    assert tifs == [("Alo", [180.0, 181.0]), ("Gtc", [183.0])]
    assert res[2].order_id is None and res[2].error is not None and "지정가" in res[2].error
    assert all(r.order_id is not None for r in (res[0], res[1], res[3]))


def test_nonce_clock_never_repeats_even_within_same_ms() -> None:
    # 벽시계가 같은 ms에 머물러도(또는 뒤로 가도) 번호는 겹치지 않고 커지기만 한다.
    from kp_arb.gateways.hl_live import NonceClock

    now = {"ms": 1_000}
    clock = NonceClock(now_ms=lambda: now["ms"])
    assert [clock.next() for _ in range(3)] == [1_000, 1_001, 1_002]
    now["ms"] = 900  # 시계가 뒤로 감(시간 동기화)
    assert clock.next() == 1_003
    now["ms"] = 5_000  # 시계가 앞서면 시계를 따른다
    assert clock.next() == 5_000


def test_nonce_clock_is_unique_across_threads() -> None:
    import threading

    from kp_arb.gateways.hl_live import NonceClock

    clock = NonceClock(now_ms=lambda: 1_000)  # 전부 같은 ms — 가장 불리한 경우
    got: list[int] = []

    def work() -> None:
        for _ in range(200):
            got.append(clock.next())

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 1_600 and len(set(got)) == 1_600


def test_install_nonce_clock_replaces_sdk_timestamp() -> None:
    # SDK(0.24.0)는 주문·취소·정정·레버리지마다 hyperliquid.exchange.get_timestamp_ms()로 nonce를
    # 찍는다 — 그 자리에 발급기를 끼운다. 네트워크 호출 없음(모듈 속성만 확인하고 되돌린다).
    import hyperliquid.exchange as sdk_exchange

    from kp_arb.gateways.hl_live import NonceClock, install_nonce_clock

    original = sdk_exchange.get_timestamp_ms
    try:
        assert install_nonce_clock(NonceClock(now_ms=lambda: 7_000))
        assert [sdk_exchange.get_timestamp_ms() for _ in range(3)] == [7_000, 7_001, 7_002]
    finally:
        sdk_exchange.get_timestamp_ms = original


async def test_hl_actions_sent_back_to_back_with_nonce_clock() -> None:
    # 사용자 2026-09-30: 앞 주문의 응답을 기다리지 않고 바로 이어 보낸다(운영 09-29 HL선 — 두
    # 세트의 진입·청산 네 건이 줄을 서 마지막 주문이 약 1.5초 묵은 가격으로 나감).
    import asyncio

    from kp_arb.gateways.hl_live import NonceClock

    ex, inf = StubExchange(), StubInfo()
    gw = HLSdkGateway(ex, inf, account_address=ADDR, nonce_clock=NonceClock())
    first = await gw.place_order(_intent())
    await asyncio.gather(gw.place_order(_intent()), gw.place_order(_intent(Side.BUY)),
                         gw.place_order(_intent()), gw.cancel_order(first))
    assert ex.max_in_flight >= 2          # 응답 전에 다음 요청이 나갔다
    assert len(ex.orders) == 4 and ex.cancels == [("xyz:SMSN", int(first))]


async def test_hl_actions_are_serialized_with_1ms_gap() -> None:
    # 옛 방식(번호 발급기를 SDK에 못 붙였을 때만) — 실측 2026-09-10: 같은 ms에 두 후주문 → SDK
    # nonce(벽시계 ms) 겹침 → "duplicate nonce" 거부. 한 번에 하나만, 직전과 1ms 이상 떨어뜨린다.
    import asyncio

    gw, ex, _ = _gw()
    first = await gw.place_order(_intent())
    await asyncio.gather(gw.place_order(_intent()), gw.place_order(_intent(Side.BUY)),
                         gw.place_order(_intent()), gw.cancel_order(first))
    assert ex.max_in_flight == 1                      # 겹쳐 돈 적 없음
    assert len(ex.nonces) == 5
    assert ex.nonces == sorted(ex.nonces) and len(set(ex.nonces)) == 5  # 전부 다른 ms


async def test_nonce_reject_is_retried_and_order_placed_once() -> None:
    # nonce 거부는 주문이 안 들어간 것 — 새 nonce로 다시 보내면 되고 중복 주문이 안 생긴다.
    gw, ex, _ = _gw()
    ex.nonce_reject_times = 1
    oid = await gw.place_order(_intent())
    assert oid == "485478010353" and len(ex.orders) == 2  # 거부 1회 + 재전송 성공


async def test_nonce_reject_gives_up_after_retries() -> None:
    from kp_arb.gateways.hl_live import NONCE_RETRIES

    gw, ex, _ = _gw()
    ex.nonce_reject_times = 10
    with pytest.raises(HLError, match="nonce"):
        await gw.place_order(_intent())
    assert len(ex.orders) == NONCE_RETRIES + 1


async def test_non_nonce_reject_is_not_retried() -> None:
    # 증거금 부족 같은 진짜 거부는 그대로 — 엔진의 ㄹ2(후주문 거부 → 중지) 규칙 유지.
    gw, ex, _ = _gw()
    ex.reject_response = "Insufficient margin"
    with pytest.raises(HLError, match="Insufficient margin"):
        await gw.place_order(_intent())
    assert len(ex.orders) == 1


async def test_positions_parsed_from_xyz_dex() -> None:
    info = StubInfo(positions=[
        {"position": {"coin": "xyz:SMSN", "szi": "-0.1", "entryPx": "184.0"}},
        {"position": {"coin": "xyz:NVDA", "szi": "5", "entryPx": "1.0"}},   # 대상 외
        {"position": {"coin": "xyz:SKHX", "szi": "0", "entryPx": "0"}},     # 0 → skip
    ])
    gw, _, _ = _gw(info)
    positions = await gw.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.underlying is Underlying.SAMSUNG and p.side is Side.SELL
    assert p.qty == 0.1 and p.account is None


async def test_margin_and_funding_and_mark() -> None:
    gw, _, _ = _gw()
    assert await gw.get_margin() == 19.6
    assert await gw.get_funding(Underlying.SK_HYNIX) == pytest.approx(0.0004326268)
    assert await gw.get_mark(Underlying.HYUNDAI) == pytest.approx(312.59)


async def test_update_leverage_calls_sdk() -> None:
    gw, ex, _ = _gw()
    await gw.update_leverage(Underlying.SAMSUNG, 10, is_cross=True)
    assert ex.leverage_calls == [(10, "xyz:SMSN", True)]  # (배수, 심볼, 교차)


async def test_update_leverage_raises_on_reject() -> None:
    from kp_arb.gateways.hl import HLError

    gw, _, _ = _gw()
    with pytest.raises(HLError):
        await gw.update_leverage(Underlying.SAMSUNG, 50, is_cross=False)  # 상한 초과


async def test_position_details_parsed() -> None:
    # clearinghouseState 상세(마진·누적펀딩·청산가·레버리지) — 잔고표(B2)·레버리지(D)
    info = StubInfo(positions=[
        {"position": {"coin": "xyz:SMSN", "szi": "-0.1", "entryPx": "184.0",
                      "marginUsed": "12.3", "liquidationPx": "250.5",
                      "positionValue": "18.4", "unrealizedPnl": "-0.6",
                      "cumFunding": {"sinceOpen": "-0.05"}, "maxLeverage": "20",
                      "leverage": {"type": "cross", "value": "5"}}},
        {"position": {"coin": "xyz:SKHX", "szi": "0", "entryPx": "0"}},  # 미보유 → skip
    ])
    gw, _, _ = _gw(info)
    details = await gw.get_position_details()
    assert set(details) == {Underlying.SAMSUNG}
    d = details[Underlying.SAMSUNG]
    assert d["margin"] == 12.3 and d["liq"] == 250.5
    assert d["cum_funding"] == -0.05 and d["max_leverage"] == 20.0
    assert d["leverage"] == 5.0 and d["leverage_cross"] is True


async def test_leverage_settings_from_active_asset_data() -> None:
    # 포지션 없어도 activeAssetData로 코인별 설정 레버리지를 읽는다(§D 캡션 보정).
    info = StubInfo(active_data={
        "xyz:SMSN": {"leverage": {"type": "cross", "value": 10}},
        "xyz:SKHX": {"leverage": {"type": "isolated", "value": 20, "rawUsd": "0.0"}},
        # xyz:HYUNDAI 응답 없음(빈 dict) → 결과에서 빠짐
    })
    gw, _, _ = _gw(info)
    out = await gw.get_leverage_settings()
    assert out[Underlying.SAMSUNG] == {"leverage": 10.0, "leverage_cross": True}
    assert out[Underlying.SK_HYNIX] == {"leverage": 20.0, "leverage_cross": False}
    assert Underlying.HYUNDAI not in out


def test_lev_from_active_asset_parsing() -> None:
    from kp_arb.gateways.hl_live import _lev_from_active_asset
    assert _lev_from_active_asset(
        {"leverage": {"type": "cross", "value": 10}}
    ) == {"leverage": 10.0, "leverage_cross": True}
    assert _lev_from_active_asset(
        {"leverage": {"type": "isolated", "value": 20, "rawUsd": "0.0"}}
    ) == {"leverage": 20.0, "leverage_cross": False}
    assert _lev_from_active_asset({}) is None       # 레버리지 없음
    assert _lev_from_active_asset(None) is None      # 이상 응답


async def test_snapshot_orders_allow_amend() -> None:
    # get_open_orders(스냅샷)로 로드된 주문도 정정 가능해야 한다 — _order_ctx를 채워야
    # "context required for modify" 거부가 안 난다(코어 재시작 후 정정, 특히 매도).
    info = StubInfo(open_orders=[
        {"coin": "xyz:SMSN", "side": "A", "origSz": "0.14", "sz": "0.14",
         "limitPx": "185.0", "oid": 777, "timestamp": 1789084276518}])
    gw, ex, _ = _gw(info)
    (snap,) = await gw.get_open_orders()  # place_order 없이 스냅샷만 로드
    # 시동 조회 주문도 접수시각이 채워진다(거래소 timestamp → 현지 HH:MM:SS, 사용자 2026-09-11)
    import time as _t

    assert snap.placed_at == _t.strftime("%H:%M:%S", _t.localtime(1789084276.518))
    new_oid = await gw.amend_order("777", qty=0.14, price=184.0)
    assert new_oid == "778"  # 예외 없이 정정 — 새 oid
    assert ex.modifies[0][:3] == (777, "xyz:SMSN", False)  # 매도(is_buy=False) 보존


async def test_amend_uses_explicit_reduce_and_post() -> None:
    # 정정 시 reduce_only·post_only는 **명시 인자**로 전달(원주문 상속 안 함). 안 넘기면
    # 벗겨져 소액 reduce 주문이 'Attempted to modify to invalid new order'로 거부(실측).
    gw, ex, _ = _gw()
    oid = await gw.place_order(_intent(Side.SELL, price=163.0))  # 원주문 옵션 무관
    await gw.amend_order(oid, qty=0.054, price=163.1, reduce_only=True, post_only=True)
    *_, reduce_only, order_type = ex.modifies[-1]
    assert reduce_only is True                        # 명시 reduce 전달
    assert order_type == {"limit": {"tif": "Alo"}}    # post_only → Alo


async def test_crossing_amend_rejected_clearly() -> None:
    # HL modify가 크로싱 Gtc를 ALO로 강제해 거부하면(always_place=false), 명확히 안내하고
    # 거부한다(폴백 없음 — 사용자 확정 "정정 안 되면 빼도 됨"). 신규 주문은 안 낸다.
    gw, ex, _ = _gw()
    ex.cross_reject = True  # modify가 'immediately matched'로 거부
    oid = await gw.place_order(_intent(Side.BUY, price=166.0))
    with pytest.raises(HLError, match="취소 후 신규"):
        await gw.amend_order(oid, qty=0.1, price=171.0)
    assert len(ex.orders) == 1 and not ex.cancels  # 폴백 없음(신규·취소 안 함)


async def test_amend_default_is_gtc_no_reduce() -> None:
    gw, ex, _ = _gw()
    oid = await gw.place_order(_intent(Side.SELL, price=163.0))
    await gw.amend_order(oid, qty=0.054, price=163.1)  # 옵션 미지정
    *_, reduce_only, order_type = ex.modifies[-1]
    assert reduce_only is False and order_type == {"limit": {"tif": "Gtc"}}


def test_default_symbols_are_measured_values() -> None:
    assert HL_SYMBOLS[Underlying.SAMSUNG] == "xyz:SMSN"
    assert HL_SYMBOLS[Underlying.SK_HYNIX] == "xyz:SKHX"
    assert HL_SYMBOLS[Underlying.HYUNDAI] == "xyz:HYUNDAI"
