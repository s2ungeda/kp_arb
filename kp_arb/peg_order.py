"""호가 추적 자동 주문 창 — 전략 이전의 주문 경로 테스트 도구.

    python -m kp_arb.peg_order

선택한 호가 단계(N호가)에 지정가를 걸고, 호가가 움직이면 따라 옮긴다:
- LS(국내, 모의): **정정**으로 가격 변경
- HL(해외, 실계정 주의!): **취소 후 신규**
체결되면 그 수량만큼 **반대 방향으로 전환**해 계속 추적한다
(매수→매도→매수→… 무한 반복). Run을 끄면 미체결을 취소하고 멈춘다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .bootstrap import LiveSystem
from .domain.enums import Instrument, OrderType, Side, Underlying, Venue
from .domain.models import OrderIntent, Quote
from .gateways.ls import OrderGoneError
from .gateways.ls_rest import RateLimitError, RestError
from .gateways.ls_ws import Fill
from .logs import setup_logging
from .order_book import OrderStatus
from .pegging import PegAction, decide, target_price

_log = logging.getLogger("kp_arb.peg_order")

_NAMES = {
    "삼성전자": Underlying.SAMSUNG,
    "하이닉스": Underlying.SK_HYNIX,
    "현대차": Underlying.HYUNDAI,
}

# LS 상품 선택(주식/ETF/선물). HL은 perp 고정.
_LS_MARKETS = {
    "주식": Instrument.KR_STOCK,
    "ETF": Instrument.KR_ETF,
    "선물": Instrument.KR_STOCK_FUTURE,
}


@dataclass
class PegController:
    """페깅 1건의 상태와 한 스텝 실행. (UI와 분리 — 시세는 LiveSystem.quotes 공유 보관소)"""

    system: LiveSystem
    venue: Venue
    underlying: Underlying
    instrument: Instrument  # LS: 주식/ETF/선물 중 선택, HL: HL_PERP
    side: Side
    level: int
    qty: float
    order_id: str | None = None
    order_price: float | None = None
    busy: bool = False  # 주문/정정 진행 중 겹침 방지
    pending: bool = False  # 진행 중 새 호가 도착 — 끝나면 최신 호가로 즉시 한 번 더
    flip_on_fill: bool = True  # 체결 시 그 수량만큼 반대 방향으로 전환해 무한 반복

    def _quote(self) -> Quote | None:
        market = "krx" if self.venue is Venue.LS else "hl"
        return self.system.quotes.get((self.underlying, self.instrument, market))

    def _intent(self, price: float) -> OrderIntent:
        return OrderIntent(
            venue=self.venue, underlying=self.underlying,
            instrument=self.instrument, side=self.side, qty=self.qty,
            order_type=OrderType.LIMIT, price=price,
        )

    async def step(self) -> str:
        """한 번의 점검: 체결 확인 → 목표가 계산 → 신규/정정/취소후신규. 상태 문자열 반환."""
        system = self.system
        note = ""
        if self.order_id is not None:
            order = system.order_book.order(self.order_id)
            if order is not None and order.status is OrderStatus.FILLED:
                _log.info("전량 체결 #%s @ %s", self.order_id, self.order_price)
                self.order_id = None
                self.order_price = None
                if not self.flip_on_fill:
                    return "filled"  # 전환 없이 종료(창이 Run 해제)
                # 체결 수량만큼 반대 방향으로 전환 — 기다리지 않고 같은 단계에서
                # 바로 아래 신규 주문까지 이어간다 (Run을 끌 때까지 무한 반복).
                self.side = Side.SELL if self.side is Side.BUY else Side.BUY
                self.qty = order.filled_qty
                label = "매도" if self.side is Side.SELL else "매수"
                _log.info("%s 전환: %s주, 즉시 주문", label, order.filled_qty)
                note = f"체결→{label} "

        target = target_price(self._quote(), self.side, self.level)
        decision = decide(venue=self.venue, current_price=self.order_price, target=target)

        if decision.action is PegAction.WAIT:
            return note + "호가 대기"
        if decision.action is PegAction.NONE:
            return f"유지 {self.order_price:,.0f}"
        assert decision.price is not None
        old_id, old_price = self.order_id, self.order_price
        try:
            if decision.action is PegAction.PLACE:
                self.order_id = await system.place(self._intent(decision.price))
            elif decision.action is PegAction.AMEND:
                assert self.order_id is not None
                self.order_id = await system.amend_price(self.order_id, decision.price)
            else:  # CANCEL_PLACE (HL)
                assert self.order_id is not None
                await system.cancel(self.order_id)
                self.order_id = await system.place(self._intent(decision.price))
        except RateLimitError:
            # 초당 요청 한도(주문 TR 2회/초) — 흔한 정상 흐름. 다음 호가에서 재시도.
            return "요청 한도 대기"
        except OrderGoneError:
            # 잔량 없음 = 이미 체결(또는 취소)된 주문 — 체결과의 경합으로 종종
            # 일어나는 정상 흐름. 다음 점검에서 체결이 확인되면 매도 전환/종료.
            _log.info("정정 불필요(잔량 없음) #%s — 체결 확인으로 이어감", old_id)
            return "체결 확인 중"
        except RestError as exc:
            _log.warning("주문 거부: %s", exc)
            return "거부 — 재시도"
        self.order_price = decision.price
        _log.info(
            "%s %s %s %s %d호가: %s @ %s → #%s @ %s",
            decision.action.value, self.venue.value, self.underlying.value,
            self.side.value, self.level,
            old_id or "-", old_price if old_price is not None else "-",
            self.order_id, decision.price,
        )
        return f"{note}{decision.action.value} @ {decision.price:,.2f} (#{self.order_id})"

    async def stop(self) -> str:
        """Run 해제: 미체결이면 취소."""
        system = self.system
        if self.order_id is None:
            return "정지"
        order = system.order_book.order(self.order_id)
        if order is not None and order.is_open:
            _log.info("Run 해제 — 미체결 취소 #%s @ %s", self.order_id, self.order_price)
            try:
                await system.cancel(self.order_id)
            except OrderGoneError:
                _log.info("취소 불필요(잔량 없음) #%s", self.order_id)
        self.order_id = None
        self.order_price = None
        return "취소·정지"


def main() -> None:
    """창 실행."""
    import threading
    import tkinter as tk
    from tkinter import ttk

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from .bootstrap import bootstrap_live

    setup_logging("peg_order")  # logs/peg_order_YYYYMMDD.log
    system_ref: dict[str, object] = {}

    def run_live() -> None:
        async def _run() -> None:
            import aiohttp

            async with aiohttp.ClientSession() as http:
                system = await bootstrap_live(http)
                system_ref["system"] = system
                system_ref["loop"] = asyncio.get_running_loop()
                await system.start()
                await system.wait()

        try:
            asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001 - 상태줄에 표시
            _log.exception("연결 실패")
            system_ref["error"] = f"{type(exc).__name__}: {exc}"

    threading.Thread(target=run_live, daemon=True).start()

    root = tk.Tk()
    root.title("호가 주문")
    root.geometry("330x200")
    root.attributes("-topmost", True)
    font = ("Malgun Gothic", 10)

    venue_var = tk.StringVar(value="LS")
    name_var = tk.StringVar(value="하이닉스")
    side_var = tk.StringVar(value="매수")
    level_var = tk.StringVar(value="2호가")
    qty_var = tk.StringVar(value="1")
    run_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="연결 중 ...")
    controller: dict[str, PegController] = {}

    market_var = tk.StringVar(value="주식")

    row1 = tk.Frame(root)
    row1.pack(fill="x", padx=8, pady=(10, 2))
    venue_box = ttk.Combobox(row1, textvariable=venue_var, values=["LS", "HL"],
                             width=5, state="readonly")
    venue_box.pack(side="left")
    ttk.Combobox(row1, textvariable=name_var, values=list(_NAMES),
                 width=10, state="readonly").pack(side="left", padx=6)
    # LS일 때만 보이는 상품 콤보(주식/ETF/선물). HL은 perp 고정이라 숨김.
    market_box = ttk.Combobox(row1, textvariable=market_var,
                              values=list(_LS_MARKETS), width=5, state="readonly")
    market_box.pack(side="left")
    tk.Checkbutton(row1, text="Run", variable=run_var, font=font,
                   command=lambda: on_run_toggle()).pack(side="right")

    def on_venue_change(_event: object = None) -> None:
        if venue_var.get() == "LS":
            market_box.pack(side="left")
        else:
            market_box.pack_forget()

    venue_box.bind("<<ComboboxSelected>>", on_venue_change)

    row2 = tk.Frame(root)
    row2.pack(fill="x", padx=8, pady=2)
    ttk.Combobox(row2, textvariable=side_var, values=["매수", "매도"],
                 width=7, state="readonly").pack(side="left", padx=(60, 0))

    row3 = tk.Frame(root)
    row3.pack(fill="x", padx=8, pady=2)
    tk.Label(row3, text="주문가", font=font, width=6, anchor="w").pack(side="left")
    level_box = ttk.Combobox(row3, textvariable=level_var,
                             values=[f"{i}호가" for i in range(1, 6)], width=7,
                             state="readonly")
    level_box.pack(side="left")

    row4 = tk.Frame(root)
    row4.pack(fill="x", padx=8, pady=2)
    tk.Label(row4, text="수량", font=font, width=6, anchor="w").pack(side="left")
    tk.Entry(row4, textvariable=qty_var, width=10, font=font).pack(side="left")

    tk.Label(root, textvariable=status_var, anchor="w", font=("Malgun Gothic", 9),
             fg="gray25").pack(fill="x", padx=8, pady=(8, 4))

    def submit(coro: object) -> None:
        loop = system_ref.get("loop")
        if loop is not None:
            asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]

    quote_handlers: dict[str, Callable[[Quote], None]] = {}
    fill_handlers: dict[str, Callable[[Fill], None]] = {}

    def detach_handlers() -> None:
        sys_obj = system_ref.get("system")
        qh = quote_handlers.pop("quote", None)
        fh = fill_handlers.pop("fill", None)
        if isinstance(sys_obj, LiveSystem):
            if qh is not None and qh in sys_obj.on_quote:
                sys_obj.on_quote.remove(qh)
            if fh is not None and fh in sys_obj.on_fill:
                sys_obj.on_fill.remove(fh)

    async def step_and_show(ctl: PegController) -> None:
        if ctl.busy:
            ctl.pending = True  # 진행 중 — 끝나는 즉시 최신 호가로 한 번 더
            return
        ctl.busy = True
        try:
            while True:
                ctl.pending = False
                try:
                    result = await ctl.step()
                except Exception as exc:  # noqa: BLE001 - 표시 후 계속
                    _log.exception("페깅 스텝 실패")
                    result = f"오류: {exc}"
                status_var.set(result)
                if result == "filled":
                    status_var.set("전량 체결 — 정지")
                    run_var.set(False)
                    controller.clear()
                    detach_handlers()
                    return
                if result == "요청 한도 대기":
                    # 초당 한도 — 다음 호가를 기다리지 않고 잠시 후 바로 재시도.
                    await asyncio.sleep(0.3)
                    ctl.pending = True
                if not ctl.pending or controller.get("active") is not ctl:
                    return
        finally:
            ctl.busy = False

    def on_run_toggle() -> None:
        if run_var.get():
            system = system_ref.get("system")
            if system is None:
                status_var.set("아직 연결 전입니다")
                run_var.set(False)
                return
            venue = Venue.LS if venue_var.get() == "LS" else Venue.HYPERLIQUID
            try:
                qty = float(qty_var.get())
            except ValueError:
                status_var.set("수량이 숫자가 아닙니다")
                run_var.set(False)
                return
            assert isinstance(system, LiveSystem)
            underlying = _NAMES[name_var.get()]
            if venue is Venue.LS:
                instrument = _LS_MARKETS[market_var.get()]
            else:
                instrument = Instrument.HL_PERP
            # 상품 가용성 확인 (예: 현대차는 단일종목 ETF 없음)
            if instrument is Instrument.KR_ETF and underlying not in system.etf_symbols:
                status_var.set(f"{name_var.get()}: ETF 종목이 없습니다")
                run_var.set(False)
                return
            if (instrument is Instrument.KR_STOCK_FUTURE
                    and underlying not in system.futures_symbols):
                status_var.set(f"{name_var.get()}: 선물 종목이 없습니다")
                run_var.set(False)
                return
            ctl = PegController(
                system=system, venue=venue,
                underlying=underlying, instrument=instrument,
                side=Side.BUY if side_var.get() == "매수" else Side.SELL,
                level=int(level_var.get()[0]), qty=qty,
            )
            controller["active"] = ctl

            def quote_handler(q: Quote) -> None:
                # 라이브 루프 스레드에서 호출 — 해당 종목 호가가 올 때마다 즉시 따라붙는다.
                if controller.get("active") is not ctl:
                    return
                if q.underlying is not ctl.underlying or q.instrument is not ctl.instrument:
                    return
                asyncio.ensure_future(step_and_show(ctl))  # noqa: RUF006

            def fill_handler(f: Fill) -> None:
                # 체결통보 수신 즉시 반응 — 다음 호가를 기다리지 않고 전환 주문을 낸다.
                if controller.get("active") is not ctl or f.order_id != ctl.order_id:
                    return
                asyncio.ensure_future(step_and_show(ctl))  # noqa: RUF006

            quote_handlers["quote"] = quote_handler
            fill_handlers["fill"] = fill_handler
            system.on_quote.append(quote_handler)
            system.on_fill.append(fill_handler)
            if venue is Venue.HYPERLIQUID:
                status_var.set("⚠ HL은 실계정입니다 — 시작")
        else:
            detach_handlers()
            stopped = controller.get("active")
            controller.clear()
            if stopped is not None:
                submit(stopped.stop())
            status_var.set("정지")

    last_step = {"t": 0.0}

    def tick() -> None:
        # 준비 완료/실패 표시 (백그라운드 연결이 끝나면 한 번 갱신)
        if status_var.get() == "연결 중 ...":
            if system_ref.get("error") is not None:
                status_var.set(f"연결 실패: {system_ref['error']}")
            elif system_ref.get("system") is not None:
                status_var.set("준비 완료 — Run 체크로 시작")
        # 평소에는 호가 수신 즉시 반응(quote_handler). 여기는 1초 주기 예비 점검만
        # (호가가 뜸한 시간대에도 체결 확인·최초 주문이 되도록).
        ctl = controller.get("active")
        if run_var.get() and ctl is not None and time.time() - last_step["t"] >= 1.0:
            last_step["t"] = time.time()
            submit(step_and_show(ctl))
        root.after(150, tick)

    tick()
    root.mainloop()


if __name__ == "__main__":
    main()
