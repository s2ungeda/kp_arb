"""HL 라이브 게이트웨이 — hyperliquid-python-sdk 어댑터 (DESIGN.md §5.2).

[라이브 실측 v6.10]
- 빌더 dex = ``xyz``(trade.xyz). **심볼: 삼성=``xyz:SMSN`` / 하이닉스=``xyz:SKHX`` /
  현대차=``xyz:HYUNDAI``** (szDecimals 3, maxLev 10, 펀딩 배수 0.5).
- HIP-3는 dex별 마진 분리 — 조회·주문 모두 ``dex="xyz"`` 스코프.
- 에이전트 키로 서명(SDK가 EIP-712 처리), 계정은 메인 주소(``account_address``).
- 주문 왕복(접수 oid→취소) 실계정 검증 완료.

SDK는 동기(requests) — asyncio에서는 ``asyncio.to_thread``로 감싼다.
**nonce(실측 2026-09-10):** SDK는 주문·취소·정정·레버리지 액션마다 nonce를 벽시계 밀리초로
스스로 찍고 호출자가 정할 수 없다. 같은 ms에 두 액션이 시작되면 ``Invalid nonce: duplicate nonce``로
뒤의 것이 거부된다 → ``_exchange_action``이 이런 액션을 잠금으로 한 번에 하나만, 직전과 1ms 이상
떨어뜨려 보내고, nonce 거부(주문이 안 들어간 거부)만 2회 재전송한다.
비밀: ``HL_AGENT_KEY``(에이전트 프라이빗 키)·``HL_ACCOUNT_ADDRESS``(메인 주소) — keyring/env.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .. import order_log
from ..config import ConfigError, SecretProvider, default_secrets
from ..domain.enums import Instrument, OrderType, Side, Underlying, Venue
from ..domain.models import OrderIntent, Position
from .base import HLGateway, placed_at_from_ms, placed_epoch_from_ms
from .hl import HLError
from .ls import OrderGoneError

HL_DEX = "xyz"
NONCE_RETRIES = 2  # nonce 충돌 거부 재전송 횟수(주문 미접수 거부라 중복 주문 없음)


def _safe_float(v: Any) -> float | None:
    """표시용 안전 파싱 — 없거나 숫자가 아니면 None (잔고표 상세)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    """정수 안전 파싱 — 없거나 숫자가 아니면 None (szDecimals 등)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _is_nonce_reject(resp: Any) -> bool:
    """서버가 nonce 때문에 액션을 안 받은 거부인가(실측 "Invalid nonce: duplicate nonce N").
    이 거부는 주문이 접수되지 않은 것이라 다시 보내도 중복이 안 생긴다."""
    if not isinstance(resp, dict) or resp.get("status") == "ok":
        return False
    return "nonce" in str(resp.get("response", "")).lower()


def _lev_from_active_asset(resp: Any) -> dict[str, Any] | None:
    """activeAssetData 응답 → {'leverage','leverage_cross'} (없으면 None).

    clearinghouseState는 **보유 코인만** 레버리지를 주지만, activeAssetData는
    포지션이 없어도 계좌의 코인별 설정 레버리지를 돌려준다(§D 캡션 보정).
    """
    if not isinstance(resp, dict):
        return None
    lev = resp.get("leverage") or {}
    val = _safe_float(lev.get("value"))
    if val is None:
        return None
    return {"leverage": val, "leverage_cross": lev.get("type") == "cross"}

# 실측 확정 심볼 (perpDexs/metaAndAssetCtxs, 2026-07-02)
HL_SYMBOLS: dict[Underlying, str] = {
    Underlying.SAMSUNG: "xyz:SMSN",
    Underlying.SK_HYNIX: "xyz:SKHX",
    Underlying.HYUNDAI: "xyz:HYUNDAI",
}


class HLSdkGateway(HLGateway):
    """hyperliquid-python-sdk의 Exchange/Info를 HLGateway 계약에 맞춘 어댑터."""

    def __init__(
        self,
        exchange: Any,
        info: Any,
        *,
        account_address: str,
        symbols: Mapping[Underlying, str] | None = None,
    ) -> None:
        self._ex = exchange
        self._info = info
        self._address = account_address
        self._symbols: dict[Underlying, str] = dict(symbols or HL_SYMBOLS)
        self._by_symbol = {v: k for k, v in self._symbols.items()}
        self._order_coin: dict[str, str] = {}  # oid -> coin (취소에 필요)
        # oid -> (coin, is_buy, sz, px) — 정정(modify)에 원주문 정보(종목·방향·수량·가격).
        # reduce/post는 정정 시 **호출부가 명시적으로 전달**한다(원주문 상속 안 함, 사용자 확정).
        self._order_ctx: dict[str, tuple[str, bool, float, float]] = {}
        # 발주 응답이 즉시체결(filled)이면 (체결수량, 평균가) — place() 직후 꺼내 OrderBook에
        # 반영한다(userFills 놓쳐도 미체결로 안 남게). pop_place_fill로 1회 소비.
        self._last_place_fill: tuple[float, float] | None = None
        # nonce를 쓰는 액션(주문·취소·정정·레버리지)은 한 번에 하나만, 직전과 1ms 이상 간격으로
        # 보낸다(실측 2026-09-10: 같은 ms에 두 후주문 → "duplicate nonce" 거부 → 체결차 중지 오탐).
        self._action_lock = asyncio.Lock()
        self._last_action_ms = 0
        self.connected = False

    @classmethod
    def from_secrets(
        cls,
        secrets: SecretProvider | None = None,
        *,
        symbols: Mapping[Underlying, str] | None = None,
    ) -> HLSdkGateway:
        """keyring/env의 에이전트 키·메인 주소로 SDK 클라이언트 조립."""
        from eth_account import Account as EthAccount
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        provider = secrets if secrets is not None else default_secrets()
        agent_key = provider.get("HL_AGENT_KEY")
        address = provider.get("HL_ACCOUNT_ADDRESS")
        if not agent_key or not address:
            raise ConfigError("missing HL_AGENT_KEY / HL_ACCOUNT_ADDRESS")
        wallet = EthAccount.from_key(agent_key)
        exchange = Exchange(
            wallet, constants.MAINNET_API_URL,
            account_address=address, perp_dexs=[HL_DEX],
        )
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        return cls(exchange, info, account_address=address, symbols=symbols)

    async def connect(self) -> None:
        # 연결 검증: xyz dex 계정 상태 1회 조회.
        await self.get_margin()
        self.connected = True

    # --- 주문 ---

    async def _exchange_action(self, what: str, fn: Callable[..., Any],
                               *args: Any, **kwargs: Any) -> dict[str, Any]:
        """nonce가 붙는 SDK 액션 한 건 — 직렬화 + 1ms 간격 + nonce 거부 재전송.

        SDK가 스레드 안에서 nonce(벽시계 ms)를 찍으므로, 잠금을 응답까지 쥐어 두 액션의 시작 시각이
        겹치지 않게 하고, 직전 액션과 같은 ms면 1ms 기다린다. 그래도 서버가 nonce 거부를 주면(주문은
        안 들어간 상태) 새 nonce로 최대 NONCE_RETRIES회 다시 보낸다. 그 밖의 거부·예외는 그대로
        돌려준다(호출부의 _check_ok/_parse_oid가 처리).
        """
        log = order_log.logger_for(Venue.HYPERLIQUID)
        async with self._action_lock:
            for attempt in range(NONCE_RETRIES + 1):
                now_ms = int(time.time() * 1000)
                while now_ms <= self._last_action_ms:
                    await asyncio.sleep(0.001)
                    now_ms = int(time.time() * 1000)
                self._last_action_ms = now_ms
                resp = await asyncio.to_thread(fn, *args, **kwargs)
                if not _is_nonce_reject(resp) or attempt == NONCE_RETRIES:
                    return resp  # type: ignore[no-any-return]
                log.warning("HL nonce 충돌 재전송 %d/%d (%s) — %s",
                            attempt + 1, NONCE_RETRIES, what, resp.get("response"))
        return resp  # type: ignore[no-any-return]  # (도달 안 함 — mypy용)

    def new_cloid(self) -> str | None:
        """클라이언트 주문번호 — 공식 "128 bit hex string"(0x + 32자리). 앞 6바이트는 시각(ms),
        뒤 10바이트는 난수라 재시동·다중 창에서도 겹치지 않는다(DESIGN §HL cloid)."""
        return f"0x{int(time.time() * 1000):012x}{secrets.token_hex(10)}"

    async def place_order(self, intent: OrderIntent, cloid: str | None = None) -> str:
        if intent.venue is not Venue.HYPERLIQUID:
            raise ValueError("HLSdkGateway only handles Hyperliquid orders")
        from hyperliquid.utils.types import Cloid

        coin = self._symbol(intent.underlying)
        is_buy = intent.side is Side.BUY
        if intent.order_type is OrderType.LIMIT:
            if intent.price is None:
                raise HLError("limit order requires price")
            # post_only는 HL의 Alo(Add-Liquidity-Only=메이커 전용) tif.
            tif = "Alo" if intent.post_only else "Gtc"
            order_type: dict[str, Any] = {"limit": {"tif": tif}}
            price = float(intent.price)
        else:
            # HL은 순수 시장가가 없음 — IOC 지정가(마크 대비 슬리피지 허용)로 대응.
            price = await self._market_px(coin, is_buy)
            order_type = {"limit": {"tif": "Ioc"}}
        order_log.order_requested(intent, price=price)  # 보내기 직전(응답 전) — 단계 추적
        self._log_wire(coin, is_buy, float(intent.qty), price, order_type, intent.reduce_only,
                       cloid)
        log = order_log.logger_for(Venue.HYPERLIQUID)
        sent_ms = int(time.time() * 1000)
        try:
            resp = await self._exchange_action(
                "order", self._ex.order, coin, is_buy, float(intent.qty), price, order_type,
                reduce_only=intent.reduce_only,
                cloid=Cloid.from_str(cloid) if cloid else None,
            )
            oid = self._parse_oid(resp)
        except HLError as exc:  # 거래소 거부 — 주문이 안 들어간 것. 거래소별 파일에 남긴다
            order_log.order_rejected(intent, exc)
            raise
        except Exception as exc:
            # 통신 오류·타임아웃 — 들어갔는지 모름. cloid로 orderStatus를 물어 들어간 주문이면
            # 정상 발주로 잇는다(재발주 중복 방지, DESIGN §HL cloid ②). 못 찾으면 거부로.
            recovered = await self._recover_by_cloid(cloid) if cloid else None
            if recovered is None:
                order_log.order_rejected(intent, exc)
                raise
            oid, resp = recovered
            log.warning("HL 발주 응답 유실(%s: %s) — orderStatus(cloid %s)로 복구 #%s",
                        type(exc).__name__, exc, cloid, oid)
        self._order_coin[oid] = coin
        self._order_ctx[oid] = (coin, is_buy, float(intent.qty), price)
        self._last_place_fill = self._parse_place_fill(resp)  # 즉시체결이면 (수량, 평균가)
        order_log.order_placed(intent, oid, resp)  # 원응답(filled/resting·수량) 포함
        # 왕복 시간(요청 직전→응답) — 한국에서 0.7~1.0초 실측(DESIGN v6.17), 지연 위치 진단용
        log.info("HL 발주 왕복 %d ms #%s cloid=%s", int(time.time() * 1000) - sent_ms, oid,
                 cloid or "-")
        return oid

    async def lookup_by_cloid(self, cloid: str) -> str | None:
        """cloid로 orderStatus 조회 → 들어간 주문이면 oid(발주 실패 유예 끝 재확인, §HL cloid ③)."""
        found = await self._recover_by_cloid(cloid)
        return found[0] if found is not None else None

    def note_identified(self, oid: str, intent: OrderIntent) -> None:
        """응답 전 식별된 주문의 취소·정정 문맥 — place_order 반환 전엔 비어 있어 그 찰나의 취소가
        'unknown order_id'로 거부됐다(검토 2026-09-11 §A 선택 항목). 응답이 오면 같은 값으로
        덮인다."""
        coin = self._symbol(intent.underlying)
        self._order_coin.setdefault(oid, coin)
        self._order_ctx.setdefault(
            oid, (coin, intent.side is Side.BUY, float(intent.qty), float(intent.price or 0.0)))

    async def _recover_by_cloid(self, cloid: str) -> tuple[str, dict[str, Any]] | None:
        """응답을 못 받은 발주를 orderStatus(cloid)로 확인 — 들어갔으면 (oid, 응답), 아니면 None.
        공식 문서: orderStatus의 oid 자리에 16바이트 hex cloid를 넣을 수 있다."""
        try:
            resp = await self._post_info(
                {"type": "orderStatus", "user": self._address, "oid": cloid})
        except Exception as exc:  # noqa: BLE001 - 조회도 실패 → 복구 불가(원래 예외로)
            order_log.logger_for(Venue.HYPERLIQUID).warning(
                "HL orderStatus(cloid %s) 조회 실패 — %s", cloid, exc)
            return None
        if not isinstance(resp, dict) or resp.get("status") != "order":
            return None
        try:
            oid = str(resp["order"]["order"]["oid"])
        except (KeyError, TypeError):
            return None
        return oid, resp

    def _log_wire(self, coin: str, is_buy: bool, sz: float, price: float,
                  order_type: dict[str, Any], reduce_only: bool,
                  cloid: str | None = None) -> None:
        """HL로 나가는 주문 패킷(action)을 그대로 hl_order 로그에 남긴다(사용자 요청 2026-09-07).

        SDK가 서명 직전에 만드는 wire 형식({a,b,p,s,r,t}, grouping)을 같은 함수로 재구성 —
        nonce·서명만 뺀 것이라 서버가 받는 본문과 같다. 로그 실패가 발주를 막으면 안 되므로 삼킨다.
        """
        try:
            from hyperliquid.utils.signing import (
                order_request_to_order_wire,
                order_wires_to_order_action,
            )
            from hyperliquid.utils.types import Cloid

            req: dict[str, Any] = {"coin": coin, "is_buy": is_buy, "sz": sz, "limit_px": price,
                                   "order_type": order_type, "reduce_only": reduce_only}
            if cloid:
                req["cloid"] = Cloid.from_str(cloid)  # wire의 "c" 칸
            wire = order_request_to_order_wire(req, self._ex.info.name_to_asset(coin))
            action = order_wires_to_order_action([wire])
            order_log.logger_for(Venue.HYPERLIQUID).info(
                "HL 요청패킷 %s", json.dumps(action, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 로그용
            order_log.logger_for(Venue.HYPERLIQUID).warning("HL 요청패킷 기록 실패: %s", exc)

    @staticmethod
    def _parse_place_fill(resp: dict[str, Any]) -> tuple[float, float] | None:
        """발주 응답의 즉시체결(filled) → (체결수량, 평균가). resting(미체결)이면 None."""
        try:
            status = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            return None
        f = status.get("filled") if isinstance(status, dict) else None
        if not f:
            return None
        sz, px = _safe_float(f.get("totalSz")), _safe_float(f.get("avgPx"))
        return (sz, px) if sz and px is not None else None

    def pop_place_fill(self) -> tuple[float, float] | None:
        """직전 발주의 즉시체결값(있으면) — 1회 소비. place()가 OrderBook 반영에 쓴다."""
        f = self._last_place_fill
        self._last_place_fill = None
        return f

    async def amend_order(
        self,
        order_id: str,
        *,
        qty: float | None = None,
        price: float | None = None,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> str:
        """정정(modify) — 서버가 취소+신규를 액션 한 번으로 처리. 새 oid 반환.

        reduce_only·post_only는 **정정 화면이 명시적으로** 넘긴다(원주문 상속 안 함) — 안 넘겨
        벗겨지면 소액 reduce 주문이 'Attempted to modify to invalid new order'로 거부된다(실측).
        """
        ctx = self._order_ctx.get(order_id)
        if ctx is None:
            raise HLError(f"unknown order_id {order_id} (context required for modify)")
        coin, is_buy, sz, px = ctx
        new_sz = float(qty) if qty is not None else sz
        new_px = float(price) if price is not None else px
        tif = "Alo" if post_only else "Gtc"  # post_only = 메이커 전용(Alo)
        # 실제로 거래소에 보내는 tif·인자를 남긴다 — 'post 안 했는데 post로 나감' 진단용.
        order_log.logger_for(Venue.HYPERLIQUID).info(
            "정정요청 #%s coin=%s buy=%s sz=%s px=%s tif=%s reduce=%s",
            order_id, coin, is_buy, new_sz, new_px, tif, reduce_only)
        resp = await self._exchange_action(
            "modify", self._ex.modify_order, int(order_id), coin, is_buy, new_sz, new_px,
            {"limit": {"tif": tif}}, reduce_only,
        )
        try:
            oid = self._parse_oid(resp)
        except HLError as exc:
            low = str(exc).lower()
            # 실측: 이미 체결/취소된 주문 정정 → 체결 경합. 정상 흐름의 거부로 구분.
            if "cannot modify canceled or filled" in low:
                raise OrderGoneError(str(exc)) from exc
            # HL modify(always_place=false)는 **크로싱(즉시 체결) Gtc를 post-only(ALO)로 강제**
            # → 거부(문서 확인). taker 정정은 modify로 불가 — 명확히 안내(취소 후 신규).
            if "immediately matched" in low:
                raise HLError(
                    "즉시체결 가격으로는 정정 불가(HL 사양) — 취소 후 신규 주문하세요") from exc
            raise
        self._order_coin[oid] = coin
        self._order_ctx[oid] = (coin, is_buy, new_sz, new_px)
        order_log.order_amended(Venue.HYPERLIQUID, order_id, oid, qty, price,
                                reduce_only=reduce_only, post_only=post_only)
        return oid

    async def cancel_order(self, order_id: str) -> None:
        coin = self._order_coin.get(order_id)
        if coin is None:
            raise HLError(f"unknown order_id {order_id} (coin required for cancel)")
        resp = await self._exchange_action("cancel", self._ex.cancel, coin, int(order_id))
        self._check_ok(resp)
        order_log.order_canceled(Venue.HYPERLIQUID, order_id)

    async def update_leverage(
        self, underlying: Underlying, leverage: int, *, is_cross: bool
    ) -> None:
        """레버리지·마진모드 변경(updateLeverage) — **주문과 별개** 액션 (§1-3).

        코인별로 계정에 저장된다. 상한(maxLeverage) 초과·포지션 보유 중 증거금 부족이면 거부.
        SDK 시그니처: update_leverage(leverage, name, is_cross).
        """
        coin = self._symbol(underlying)
        resp = await self._exchange_action(
            "leverage", self._ex.update_leverage, int(leverage), coin, is_cross)
        self._check_ok(resp)  # 거부 시 사유와 함께 HLError

    # --- 조회 ---

    async def _clearinghouse(self) -> dict[str, Any]:
        resp: dict[str, Any] = await self._post_info(
            {"type": "clearinghouseState", "user": self._address, "dex": HL_DEX})
        return resp

    def _parse_positions(
        self, state: dict[str, Any]
    ) -> tuple[list[Position], dict[Underlying, dict[str, Any]]]:
        """clearinghouseState 한 응답 → (포지션 목록, 종목별 상세). 둘 다 여기서 뽑는다."""
        positions: list[Position] = []
        details: dict[Underlying, dict[str, Any]] = {}
        for asset in state.get("assetPositions", []):
            pos = asset.get("position", {})
            underlying = self._by_symbol.get(str(pos.get("coin", "")))
            szi = _safe_float(pos.get("szi"))
            if underlying is None or not szi:  # 미보유(0/None) 제외
                continue
            positions.append(Position(
                venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                underlying=underlying, side=Side.BUY if szi > 0 else Side.SELL,
                qty=abs(szi), avg_price=float(pos["entryPx"]), account=None))
            lev = pos.get("leverage") or {}
            details[underlying] = {
                "margin": _safe_float(pos.get("marginUsed")),
                "cum_funding": _safe_float((pos.get("cumFunding") or {}).get("sinceOpen")),
                "liq": _safe_float(pos.get("liquidationPx")),
                "position_value": _safe_float(pos.get("positionValue")),
                "unrealized_pnl": _safe_float(pos.get("unrealizedPnl")),
                "leverage": _safe_float(lev.get("value")),
                "leverage_cross": lev.get("type") == "cross",  # D: 교차/격리
                "max_leverage": _safe_float(pos.get("maxLeverage")),
            }
        return positions, details

    async def get_positions(self) -> Sequence[Position]:
        return self._parse_positions(await self._clearinghouse())[0]

    async def get_positions_and_details(
        self,
    ) -> tuple[Sequence[Position], dict[Underlying, dict[str, Any]]]:
        """포지션 + 상세를 clearinghouseState **1회**로 (refresh 왕복 절감)."""
        return self._parse_positions(await self._clearinghouse())

    async def get_open_orders(self) -> Sequence[Any]:
        """미체결 스냅샷(frontendOpenOrders, dex 스코프) → TrackedOrder."""
        from ..order_book import OrderStatus, TrackedOrder

        rows = await self._post_info(
            {"type": "frontendOpenOrders", "user": self._address, "dex": HL_DEX}
        )
        orders: list[TrackedOrder] = []
        for row in rows if isinstance(rows, list) else []:
            underlying = self._by_symbol.get(str(row.get("coin", "")))
            if underlying is None:
                continue
            is_buy = str(row.get("side")) == "B"
            coin, orig_sz, limit_px = str(row["coin"]), float(row["origSz"]), float(
                row["limitPx"])
            intent = OrderIntent(
                venue=Venue.HYPERLIQUID,
                underlying=underlying,
                instrument=Instrument.HL_PERP,
                side=Side.BUY if is_buy else Side.SELL,
                qty=orig_sz,
                order_type=OrderType.LIMIT,
                price=limit_px,
            )
            filled = orig_sz - float(row["sz"])  # sz = 잔여
            oid = str(row["oid"])
            self._order_coin[oid] = coin  # 취소 가능하도록 coin 기억
            # 정정(modify)엔 원주문 컨텍스트가 필요 — 스냅샷 로드분도 채운다(재시작 후 정정 가능).
            # reduce/post는 정정 시 화면이 명시 전달하므로 여기선 종목·방향·수량·가격만.
            self._order_ctx[oid] = (coin, is_buy, orig_sz, limit_px)
            orders.append(
                TrackedOrder(
                    order_id=oid,
                    intent=intent,
                    status=OrderStatus.PARTIAL if filled > 0 else OrderStatus.ACCEPTED,
                    filled_qty=filled,
                    placed_at=placed_at_from_ms(row.get("timestamp")),  # 접수시각(거래소 값)
                    placed_epoch=placed_epoch_from_ms(row.get("timestamp")),  # 정렬용
                )
            )
        return orders

    async def get_margin(self) -> float:
        """xyz dex 계정 가치(USDC). HIP-3는 dex별 마진 분리."""
        state = await self._post_info(
            {"type": "clearinghouseState", "user": self._address, "dex": HL_DEX}
        )
        return float(state.get("marginSummary", {}).get("accountValue", 0.0))

    async def get_position_details(self) -> dict[Underlying, dict[str, Any]]:
        """clearinghouseState 포지션 상세(종목별) — 마진·누적펀딩·청산가·레버리지 등(B2·D)."""
        return self._parse_positions(await self._clearinghouse())[1]

    async def get_instrument_meta(self) -> dict[Underlying, dict[str, Any]]:
        """metaAndAssetCtxs universe → 코인별 code·szDecimals·maxLeverage (시동 종목정보).

        정적 메타라 시동 시 1회 조회로 충분 — 포지션 없어도 maxLeverage를 안다(§5.10).
        """
        meta, _ctxs = await self._post_info({"type": "metaAndAssetCtxs", "dex": HL_DEX})
        out: dict[Underlying, dict[str, Any]] = {}
        for asset in meta.get("universe", []):
            name = str(asset.get("name", ""))
            underlying = self._by_symbol.get(name)
            if underlying is None:
                continue
            out[underlying] = {
                "code": name,
                "sz_decimals": _safe_int(asset.get("szDecimals")),
                "max_leverage": _safe_float(asset.get("maxLeverage")),
            }
        return out

    async def get_leverage_settings(self) -> dict[Underlying, dict[str, Any]]:
        """모든 HL 종목의 레버리지·마진모드(activeAssetData) — **포지션 무관**, 캡션 보정용(§D).

        clearinghouseState가 미보유 코인엔 레버리지를 안 줘, 주문창 캡션이 기본값(5x)에
        멈추는 걸 막는다. 한 종목 조회 실패는 그 종목만 건너뛴다(표시용, 비핵심).
        """
        out: dict[Underlying, dict[str, Any]] = {}
        for underlying, coin in self._symbols.items():
            try:
                resp = await self._post_info(
                    {"type": "activeAssetData", "user": self._address, "coin": coin})
            except Exception:  # noqa: BLE001 - 표시 보정 조회 실패가 스냅샷을 막지 않게
                continue
            parsed = _lev_from_active_asset(resp)
            if parsed is not None:
                out[underlying] = parsed
        return out

    async def get_funding(self, underlying: Underlying) -> float:
        coin = self._symbol(underlying)
        meta, ctxs = await self._post_info({"type": "metaAndAssetCtxs", "dex": HL_DEX})
        for asset, ctx in zip(meta["universe"], ctxs, strict=True):
            if asset["name"] == coin:
                return float(ctx.get("funding", 0.0))
        raise HLError(f"{coin} not in {HL_DEX} universe")

    async def get_prev_funding(self, underlying: Underlying) -> float:
        """직전(가장 최근 적용된) 펀딩률. fundingHistory 최근 1건."""
        import time

        coin = self._symbol(underlying)
        start = int((time.time() - 3 * 3600) * 1000)  # 최근 3시간이면 충분(펀딩은 매시)
        rows = await self._post_info(
            {"type": "fundingHistory", "coin": coin, "startTime": start}
        )
        if isinstance(rows, list) and rows:
            return float(rows[-1].get("fundingRate", 0.0))
        return 0.0

    async def get_mark(self, underlying: Underlying) -> float:
        coin = self._symbol(underlying)
        meta, ctxs = await self._post_info({"type": "metaAndAssetCtxs", "dex": HL_DEX})
        for asset, ctx in zip(meta["universe"], ctxs, strict=True):
            if asset["name"] == coin:
                return float(ctx["markPx"])
        raise HLError(f"{coin} not in {HL_DEX} universe")

    # --- 내부 ---

    async def _post_info(self, body: dict[str, Any]) -> Any:
        return await asyncio.to_thread(self._info.post, "/info", body)

    async def _market_px(self, coin: str, is_buy: bool, *, slippage: float = 0.01) -> float:
        underlying = self._by_symbol[coin]
        mark = await self.get_mark(underlying)
        raw = mark * (1 + slippage) if is_buy else mark * (1 - slippage)
        return float(f"{raw:.5g}")  # HL 유효숫자 5자리 제한

    def _symbol(self, underlying: Underlying) -> str:
        try:
            return self._symbols[underlying]
        except KeyError as exc:
            raise HLError(f"no HL symbol configured for {underlying}") from exc

    def _check_ok(self, resp: dict[str, Any]) -> None:
        if resp.get("status") != "ok":
            raise HLError(f"HL rejected: {resp.get('response')}")

    def _parse_oid(self, resp: dict[str, Any]) -> str:
        self._check_ok(resp)
        try:
            status = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise HLError("cannot parse HL order response") from exc
        for key in ("resting", "filled"):
            if key in status:
                return str(status[key]["oid"])
        raise HLError(f"HL order not accepted: {status}")
