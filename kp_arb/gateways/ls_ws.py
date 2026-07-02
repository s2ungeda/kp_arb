"""LS Open API WebSocket 클라이언트 (DESIGN.md §5.1).

블록 1-4 범위: 실시간 호가(H1_/NH1) + 체결(SC0~SC4) + 장운영(JIF) 구독,
연결 끊김 시 자동 재연결·재구독, on_quote / on_fill / on_market_status 이벤트 노출.

라이브 없음: 실제 WS는 주입된 ``WSConnector``/``WSConnection``(Protocol) 뒤로 격리.
테스트는 가짜 WS 서버(녹화 프레임)만 사용한다.

[라이브 정합 v6.3/v6.6] 실측 프레임 기준:
- H1_/NH1 body: ``bidho1``/``offerho1``(1호가, 문자열), ``hotime``(HHMMSS), ``shcode``(종목코드).
- JIF는 **시장 단위**(tr_key="0" 전체) — 종목코드로 구독하면 아무 프레임도 오지 않는다.
  body = ``{jangubun(시장구분), jstatus(상태코드)}``. 해석은 SessionService.
- **체결통보(SC0~SC4)는 tr_type="1"(계좌 등록)** — "3"으로 보내면 ACK만 오고 등록되지 않는다.
  SC1(체결) body 실필드: ``ordno``(주문번호)·``execno``(체결번호)·``execqty``·``execprc``·
  ``exectime``(HHMMSSmmm). SC0=접수, SC2=정정, SC3=취소, SC4=거부 → ``OrderEvent``로 분화.
- **선물 통보(v6.7 실측)**: O01=접수(``ordno`` 패딩 없음), C01=체결(``ordno`` 10자리 zero-pad,
  ``chevol``·``chetime``·``cheprice``는 **원화의 1/100 단위**), H01=정정취소(원주문=``ordordno``).
  주문번호 패딩이 프레임마다 달라 ``_norm_ordno``로 정규화해 매칭한다.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from pydantic import BaseModel

from ..domain.enums import Instrument, Underlying
from ..domain.models import Quote

QUOTE_TRS: frozenset[str] = frozenset({"H1_", "NH1"})
FUTURES_QUOTE_TR = "JH0"   # 주식선물 호가 (body 필드는 H1_와 동일 가정 — 장중 실확인 예정)
STOCK_TRADE_TR = "S3_"     # 주식 체결 (현재가 — body 'price' 가정, 장중 실확인 예정)
FUTURES_TRADE_TR = "JC0"   # 주식선물 체결 (동일 가정)
EXPECTED_TR = "YS3"        # 예상체결 (실측: yeprice/shcode — 장전 동시호가에 흐름)
FILL_TRS: frozenset[str] = frozenset({"SC1", "C01"})  # 체결: 주식 SC1 / 선물 C01
ORDER_EVENT_TRS: dict[str, str] = {
    "SC0": "ack", "SC2": "amend", "SC3": "cancel", "SC4": "reject",  # 주식
    "O01": "ack", "H01": "cancel",                                    # 선물(H01=정정취소 공용)
}
STOCK_FILL_TRS: tuple[str, ...] = ("SC0", "SC1", "SC2", "SC3", "SC4")     # 주식계좌 토큰 WS
FUTURES_FILL_TRS: tuple[str, ...] = ("O01", "C01", "H01")                  # 선물옵션계좌 토큰 WS
ACCOUNT_TRS: tuple[str, ...] = STOCK_FILL_TRS + FUTURES_FILL_TRS
STATUS_TR = "JIF"


def _norm_ordno(raw: object) -> str:
    """주문번호 정규화 — 프레임마다 zero-pad가 달라("10963" vs "0000010996") 매칭용."""
    text = str(raw).strip()
    try:
        return str(int(text))
    except ValueError:
        return text


class Fill(BaseModel):
    """체결 이벤트(DESIGN.md §10 fills). 추후 StateStore에서 재사용 가능."""

    fill_id: str
    order_id: str
    qty: float
    price: float
    fee: float = 0.0
    ts: float


class MarketStatus(BaseModel):
    """장운영(JIF) 이벤트. SessionService(블록 2-1)가 SessionPhase로 해석."""

    tr_key: str
    body: dict[str, Any] = {}


class OrderEvent(BaseModel):
    """주문 이벤트(SC0 접수 / SC2 정정 / SC3 취소 / SC4 거부). OrderBook 상태 전이용."""

    kind: str            # "ack" | "amend" | "cancel" | "reject"
    order_id: str        # ordno
    org_order_id: str | None = None  # 정정/취소 통보의 원주문(orgordno)
    body: dict[str, Any] = {}


class TradeTick(BaseModel):
    """체결(현재가) 이벤트 — 주식 S3_ / 선물 JC0."""

    underlying: Underlying
    instrument: Instrument
    price: float
    ts: float = 0.0


class ExpectedPrice(BaseModel):
    """예상체결가 이벤트(YS3) — 동시호가 시간대."""

    underlying: Underlying
    price: float
    ts: float = 0.0


class WSConnection(Protocol):
    """단일 WS 세션. 구독 메시지 전송 + 프레임 비동기 수신."""

    async def send(self, message: str) -> None: ...
    def __aiter__(self) -> AsyncIterator[str]: ...


class WSConnector(Protocol):
    """WS 세션 팩토리. 재연결 시마다 새 세션을 생성."""

    async def connect(self) -> WSConnection: ...


class LSWebSocketClient:
    """LS WS 클라이언트. 구독 상태를 보존하고 끊기면 재연결·재구독한다."""

    def __init__(
        self,
        connector: WSConnector,
        *,
        token: str = "",
        etf_symbols: dict[Underlying, str] | None = None,
        max_reconnects: int = 3,
        reconnect_backoff_s: float = 0.0,
    ) -> None:
        self._connector = connector
        self._token = token
        # ETF 종목코드(config.yaml 주입) — 호가 구독·해석에 사용.
        self._etf_symbols = dict(etf_symbols or {})
        self._etf_underlying = {v: k for k, v in self._etf_symbols.items()}
        # 선물 종목코드(t8401 자동 조회값 주입) — 선물 호가 구독·해석에 사용.
        self._futures_underlying: dict[str, Underlying] = {}
        self._max_reconnects = max_reconnects
        self._reconnect_backoff_s = reconnect_backoff_s
        self._subs: list[tuple[str, str, str]] = []  # (tr_cd, tr_key, tr_type) 희망 구독 상태
        self._conn: WSConnection | None = None
        self.on_quote: list[Callable[[Quote], None]] = []
        self.on_trade: list[Callable[[TradeTick], None]] = []          # 체결(현재가)
        self.on_expected: list[Callable[[ExpectedPrice], None]] = []   # 예상체결가
        self.on_fill: list[Callable[[Fill], None]] = []
        self.on_order_event: list[Callable[[OrderEvent], None]] = []
        self.on_market_status: list[Callable[[MarketStatus], None]] = []
        self.on_raw: list[Callable[[str], None]] = []  # 진단: 모든 원시 프레임

    # --- 구독 등록(희망 상태). 실제 전송은 connect 시 _resubscribe ---

    def subscribe_quotes(self, underlying: Underlying) -> None:
        codes = [underlying.krx_code]
        etf = self._etf_symbols.get(underlying)
        if etf is not None:
            codes.append(etf)  # 단일종목 레버리지 ETF 호가도 함께 구독
        for code in codes:
            self._add("H1_", code)
            self._add("NH1", code)

    def subscribe_futures_quotes(self, symbols: dict[Underlying, str]) -> None:
        """주식선물 호가(JH0)+체결(JC0) 구독. symbols = t8401 자동 조회 결과."""
        for underlying, code in symbols.items():
            self._futures_underlying[code] = underlying
            self._add(FUTURES_QUOTE_TR, code)
            self._add(FUTURES_TRADE_TR, code)

    def subscribe_trades(self, underlying: Underlying) -> None:
        """주식 체결(S3_, 현재가)과 예상체결(YS3) 구독."""
        self._add(STOCK_TRADE_TR, underlying.krx_code)
        self._add(EXPECTED_TR, underlying.krx_code)

    def subscribe_fills(self) -> None:
        """주식+선물 체결통보 전부 구독(단일 연결용 — 계좌 통보는 해당 토큰 계좌 것만 온다)."""
        self.subscribe_stock_fills()
        self.subscribe_futures_fills()

    def subscribe_stock_fills(self) -> None:
        # 계좌 이벤트는 tr_type "1"로 등록해야 수신된다(실측 — "3"은 ACK만 옴).
        for tr in STOCK_FILL_TRS:
            self._add(tr, "", tr_type="1")

    def subscribe_futures_fills(self) -> None:
        for tr in FUTURES_FILL_TRS:
            self._add(tr, "", tr_type="1")

    def subscribe_market_status(self) -> None:
        # JIF는 시장 단위 — tr_key "0"(전체). 종목코드 구독은 무응답(실측).
        self._add(STATUS_TR, "0")

    def _add(self, tr_cd: str, tr_key: str, *, tr_type: str = "3") -> None:
        if (tr_cd, tr_key, tr_type) not in self._subs:
            self._subs.append((tr_cd, tr_key, tr_type))

    # --- 실행 루프 ---

    async def run(self) -> None:
        """연결 → 재구독 → 프레임 디스패치. 끊기면 재연결, 깨끗이 끝나면 종료."""
        attempts = 0
        while True:
            conn = await self._connector.connect()
            self._conn = conn
            await self._resubscribe(conn)
            try:
                async for raw in conn:
                    self._dispatch(raw)
            except ConnectionError:
                attempts += 1
                if attempts > self._max_reconnects:
                    raise
                if self._reconnect_backoff_s > 0:
                    await asyncio.sleep(self._reconnect_backoff_s)
                continue
            else:
                return  # 스트림이 정상 종료됨

    async def _resubscribe(self, conn: WSConnection) -> None:
        for tr_cd, tr_key, tr_type in self._subs:
            await conn.send(self._register_msg(tr_cd, tr_key, tr_type))

    def _register_msg(self, tr_cd: str, tr_key: str, tr_type: str) -> str:
        # tr_type: 1=계좌 등록(SC*), 3=시세 등록(H1_/JIF 등)
        return json.dumps(
            {
                "header": {"token": self._token, "tr_type": tr_type},
                "body": {"tr_cd": tr_cd, "tr_key": tr_key},
            }
        )

    # --- 프레임 파싱/디스패치 ---

    def _dispatch(self, raw: str) -> None:
        msg = json.loads(raw)
        for raw_handler in self.on_raw:
            raw_handler(raw)
        tr_cd = msg.get("header", {}).get("tr_cd")
        if not isinstance(msg.get("body"), dict):
            return  # 등록 ACK/시스템 프레임(body 없음) — 데이터 아님, 무시
        if tr_cd in QUOTE_TRS:
            quote = self._parse_quote(msg)
            if quote is not None:
                for handler in self.on_quote:
                    handler(quote)
        elif tr_cd == FUTURES_QUOTE_TR:
            fut_quote = self._parse_futures_quote(msg)
            if fut_quote is not None:
                for handler in self.on_quote:
                    handler(fut_quote)
        elif tr_cd in (STOCK_TRADE_TR, FUTURES_TRADE_TR):
            tick = self._parse_trade(tr_cd, msg)
            if tick is not None:
                for trade_handler in self.on_trade:
                    trade_handler(tick)
        elif tr_cd == EXPECTED_TR:
            expected = self._parse_expected(msg)
            if expected is not None:
                for expected_handler in self.on_expected:
                    expected_handler(expected)
        elif tr_cd in FILL_TRS:
            fill = self._parse_fill(tr_cd, msg)
            for fill_handler in self.on_fill:
                fill_handler(fill)
        elif tr_cd in ORDER_EVENT_TRS:
            event = self._parse_order_event(tr_cd, msg)
            for event_handler in self.on_order_event:
                event_handler(event)
        elif tr_cd == STATUS_TR:
            status = self._parse_status(msg)
            for status_handler in self.on_market_status:
                status_handler(status)
        # 알 수 없는 tr_cd는 무시

    def _parse_quote(self, msg: dict[str, Any]) -> Quote | None:
        body = msg["body"]
        code = str(body.get("shcode") or msg.get("header", {}).get("tr_key", ""))
        # 종목코드로 주식 vs ETF 판별(둘 다 아니면 무시).
        etf_underlying = self._etf_underlying.get(code)
        if etf_underlying is not None:
            instrument, underlying = Instrument.KR_ETF, etf_underlying
        else:
            stock_underlying = Underlying.from_krx_code(code)
            if stock_underlying is None:
                return None
            instrument, underlying = Instrument.KR_STOCK, stock_underlying
        # 실측 필드: bidho1/offerho1(1호가), bidrem1/offerrem1(잔량), hotime(HHMMSS).
        return Quote(
            underlying=underlying,
            instrument=instrument,
            bid=float(body["bidho1"]),
            ask=float(body["offerho1"]),
            ts=float(body["hotime"]),
            bid_qty=float(body.get("bidrem1", 0) or 0),
            ask_qty=float(body.get("offerrem1", 0) or 0),
        )

    def _parse_futures_quote(self, msg: dict[str, Any]) -> Quote | None:
        # JH0 body 필드는 H1_와 동일(bidho1/offerho1/hotime/shcode) 가정 — 장중 실확인 예정.
        body = msg["body"]
        code = str(body.get("shcode") or msg.get("header", {}).get("tr_key", ""))
        underlying = self._futures_underlying.get(code)
        if underlying is None:
            return None
        try:
            return Quote(
                underlying=underlying,
                instrument=Instrument.KR_STOCK_FUTURE,
                bid=float(body["bidho1"]),
                ask=float(body["offerho1"]),
                ts=float(body.get("hotime", 0) or 0),
                bid_qty=float(body.get("bidrem1", 0) or 0),
                ask_qty=float(body.get("offerrem1", 0) or 0),
            )
        except (KeyError, ValueError):
            return None  # 필드 가정이 다르면 조용히 무시(on_raw로 실프레임 확인)

    def _parse_trade(self, tr_cd: str, msg: dict[str, Any]) -> TradeTick | None:
        # S3_/JC0 체결가 필드는 'price' 가정(장중 실확인 예정) — 다르면 무시+on_raw로 확인.
        body = msg["body"]
        code = str(body.get("shcode") or msg.get("header", {}).get("tr_key", ""))
        if tr_cd == FUTURES_TRADE_TR:
            underlying = self._futures_underlying.get(code)
            instrument = Instrument.KR_STOCK_FUTURE
        else:
            etf_u = self._etf_underlying.get(code)
            underlying = etf_u if etf_u is not None else Underlying.from_krx_code(code)
            instrument = Instrument.KR_ETF if etf_u is not None else Instrument.KR_STOCK
        if underlying is None or "price" not in body:
            return None
        try:
            return TradeTick(underlying=underlying, instrument=instrument,
                             price=float(body["price"]),
                             ts=float(body.get("chetime", 0) or 0))
        except ValueError:
            return None

    def _parse_expected(self, msg: dict[str, Any]) -> ExpectedPrice | None:
        # YS3 실측 필드: yeprice(예상체결가), shcode.
        body = msg["body"]
        code = str(body.get("shcode") or msg.get("header", {}).get("tr_key", ""))
        underlying = Underlying.from_krx_code(code)
        if underlying is None or "yeprice" not in body:
            return None
        try:
            return ExpectedPrice(underlying=underlying, price=float(body["yeprice"]))
        except ValueError:
            return None

    def _parse_fill(self, tr_cd: str, msg: dict[str, Any]) -> Fill:
        body = msg["body"]
        if tr_cd == "C01":
            # 선물 체결 실측: chevol/chetime, cheprice는 원화의 1/100(실측: 3000.00=300,000원).
            return Fill(
                fill_id=str(body.get("yakseq") or body.get("seq", "")),
                order_id=_norm_ordno(body["ordno"]),
                qty=float(body["chevol"]),
                price=float(body["cheprice"]) * 100.0,
                fee=0.0,
                ts=float(body["chetime"]),
            )
        # 주식 SC1 실측 필드: ordno/execno/execqty/execprc/exectime(HHMMSSmmm).
        return Fill(
            fill_id=str(body["execno"]),
            order_id=_norm_ordno(body["ordno"]),
            qty=float(body["execqty"]),
            price=float(body["execprc"]),
            fee=0.0,  # 수수료 필드(cmsnamtexecamt)는 모의에서 공백 — 라이브 확정 시 반영
            ts=float(body["exectime"]),
        )

    def _parse_order_event(self, tr_cd: str, msg: dict[str, Any]) -> OrderEvent:
        body = msg["body"]
        # 원주문 필드: 주식(SC*)=orgordno / 선물(H01)=ordordno.
        org = _norm_ordno(body.get("orgordno") or body.get("ordordno") or "")
        return OrderEvent(
            kind=ORDER_EVENT_TRS[tr_cd],
            order_id=_norm_ordno(body.get("ordno", "")),
            org_order_id=org if org not in ("", "0") else None,
            body=body,
        )

    def _parse_status(self, msg: dict[str, Any]) -> MarketStatus:
        return MarketStatus(tr_key=msg.get("header", {}).get("tr_key", ""), body=msg["body"])
