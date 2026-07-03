"""호가 추적 자동 주문 창 — 전략 이전의 주문 경로 테스트 도구.

    python -m kp_arb.peg_order

선택한 호가 단계(N호가)에 지정가를 걸고, 호가가 움직이면 따라 옮긴다:
- LS(국내, 모의): **정정**으로 가격 변경
- HL(해외, 실계정 주의!): **취소 후 신규**
체결이 완료되면 Run이 자동 해제된다. Run을 끄면 미체결을 취소한다.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .bootstrap import LiveSystem
from .domain.enums import Instrument, OrderType, Side, Underlying, Venue
from .domain.models import OrderIntent, Quote
from .order_book import OrderStatus
from .pegging import PegAction, decide, target_price

_NAMES = {
    "삼성전자": Underlying.SAMSUNG,
    "하이닉스": Underlying.SK_HYNIX,
    "현대차": Underlying.HYUNDAI,
}


@dataclass
class PegController:
    """페깅 1건의 상태와 한 스텝 실행. (UI와 분리 — 시세는 LiveSystem.quotes 공유 보관소)"""

    system: LiveSystem
    venue: Venue
    underlying: Underlying
    side: Side
    level: int
    qty: float
    order_id: str | None = None
    order_price: float | None = None

    def _instrument(self) -> Instrument:
        return Instrument.KR_STOCK if self.venue is Venue.LS else Instrument.HL_PERP

    def _quote(self) -> Quote | None:
        market = "krx" if self.venue is Venue.LS else "hl"
        return self.system.quotes.get((self.underlying, self._instrument(), market))

    def _intent(self, price: float) -> OrderIntent:
        return OrderIntent(
            venue=self.venue, underlying=self.underlying,
            instrument=self._instrument(), side=self.side, qty=self.qty,
            order_type=OrderType.LIMIT, price=price,
        )

    async def step(self) -> str:
        """한 번의 점검: 체결 확인 → 목표가 계산 → 신규/정정/취소후신규. 상태 문자열 반환."""
        system = self.system
        if self.order_id is not None:
            order = system.order_book.order(self.order_id)
            if order is not None and order.status is OrderStatus.FILLED:
                self.order_id = None
                return "filled"  # 전량 체결 → 페깅 종료(창이 Run 해제)

        target = target_price(self._quote(), self.side, self.level)
        decision = decide(venue=self.venue, current_price=self.order_price, target=target)

        if decision.action is PegAction.WAIT:
            return "호가 대기"
        if decision.action is PegAction.NONE:
            return f"유지 {self.order_price:,.0f}"
        assert decision.price is not None
        if decision.action is PegAction.PLACE:
            self.order_id = await system.place(self._intent(decision.price))
        elif decision.action is PegAction.AMEND:
            assert self.order_id is not None
            self.order_id = await system.amend_price(self.order_id, decision.price)
        else:  # CANCEL_PLACE (HL)
            assert self.order_id is not None
            await system.cancel(self.order_id)
            self.order_id = await system.place(self._intent(decision.price))
        self.order_price = decision.price
        return f"{decision.action.value} @ {decision.price:,.2f} (#{self.order_id})"

    async def stop(self) -> str:
        """Run 해제: 미체결이면 취소."""
        system = self.system
        if self.order_id is None:
            return "정지"
        order = system.order_book.order(self.order_id)
        if order is not None and order.is_open:
            await system.cancel(self.order_id)
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

    row1 = tk.Frame(root)
    row1.pack(fill="x", padx=8, pady=(10, 2))
    ttk.Combobox(row1, textvariable=venue_var, values=["LS", "HL"],
                 width=5, state="readonly").pack(side="left")
    ttk.Combobox(row1, textvariable=name_var, values=list(_NAMES),
                 width=10, state="readonly").pack(side="left", padx=6)
    tk.Checkbutton(row1, text="Run", variable=run_var, font=font,
                   command=lambda: on_run_toggle()).pack(side="right")

    row2 = tk.Frame(root)
    row2.pack(fill="x", padx=8, pady=2)
    ttk.Combobox(row2, textvariable=side_var, values=["매수", "매도"],
                 width=7, state="readonly").pack(side="left", padx=(60, 0))

    row3 = tk.Frame(root)
    row3.pack(fill="x", padx=8, pady=2)
    tk.Label(row3, text="주문가", font=font, width=6, anchor="w").pack(side="left")
    level_box = ttk.Combobox(row3, textvariable=level_var,
                             values=["1호가", "2호가", "3호가"], width=7,
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
            ctl = PegController(
                system=system, venue=venue,
                underlying=_NAMES[name_var.get()],
                side=Side.BUY if side_var.get() == "매수" else Side.SELL,
                level=int(level_var.get()[0]), qty=qty,
            )
            controller["active"] = ctl
            if venue is Venue.HYPERLIQUID:
                status_var.set("⚠ HL은 실계정입니다 — 시작")
        else:
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
        ctl = controller.get("active")
        if run_var.get() and ctl is not None and time.time() - last_step["t"] >= 0.5:
            last_step["t"] = time.time()

            async def _step() -> None:
                try:
                    result = await ctl.step()
                except Exception as exc:  # noqa: BLE001 - 표시 후 계속
                    result = f"오류: {exc}"
                status_var.set(result)
                if result == "filled":
                    status_var.set("전량 체결 — 정지")
                    run_var.set(False)
                    controller.clear()

            submit(_step())
        root.after(150, tick)

    tick()
    root.mainloop()


if __name__ == "__main__":
    main()
