"""시세 모니터 — 취급 종목 전체를 한눈에 보는 컴팩트 데스크톱 창.

    python -m kp_arb.monitor

- 창은 tkinter(파이썬 기본 포함 — 추가 설치 없음), 작게(약 400px 폭) 유지.
- 데이터는 LiveSystem(백그라운드 스레드의 asyncio)에서 실시간 수신,
  화면은 0.3초마다 최신값을 읽어 갱신(읽기 전용 — 주문 없음).
- 행: 종목별 주식/선물/ETF 호가 + HL 마크. 하단: 세션 상태·계좌 잔고·수신 시각.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .domain.enums import Account, Instrument, Underlying
from .domain.models import Quote
from .gateways.hl import Mark

_NAMES = {
    Underlying.SAMSUNG: "삼성전자",
    Underlying.SK_HYNIX: "SK하이닉스",
    Underlying.HYUNDAI: "현대차",
}
_KIND = {
    Instrument.KR_STOCK: "주식",
    Instrument.KR_STOCK_FUTURE: "선물",
    Instrument.KR_ETF: "ETF",
}


@dataclass
class MonitorState:
    """실시간 콜백이 채우고 화면이 읽는 최신값 저장소(읽기 전용 표시용)."""

    quotes: dict[tuple[Underlying, Instrument], tuple[float, float]] = field(
        default_factory=dict
    )
    marks: dict[Underlying, float] = field(default_factory=dict)
    last_update: float = 0.0

    def on_quote(self, quote: Quote) -> None:
        self.quotes[(quote.underlying, quote.instrument)] = (quote.bid, quote.ask)
        self.last_update = time.time()

    def on_mark(self, mark: Mark) -> None:
        self.marks[mark.underlying] = mark.price
        self.last_update = time.time()

    def rows(self) -> list[tuple[str, str, str, str, str]]:
        """화면 표 행: (종목, 구분, 매수, 매도, 중간/마크)."""
        out: list[tuple[str, str, str, str, str]] = []
        for u in Underlying:
            name = _NAMES[u]
            for inst in (Instrument.KR_STOCK, Instrument.KR_STOCK_FUTURE,
                         Instrument.KR_ETF):
                bid_ask = self.quotes.get((u, inst))
                if bid_ask is None:
                    out.append((name, _KIND[inst], "-", "-", "-"))
                else:
                    bid, ask = bid_ask
                    out.append((name, _KIND[inst], f"{bid:,.0f}", f"{ask:,.0f}",
                                f"{(bid + ask) / 2:,.0f}"))
                name = ""  # 같은 종목은 첫 행에만 이름 표시
            mark = self.marks.get(u)
            out.append(("", "HL", "-", "-", f"{mark:,.2f}" if mark else "-"))
        return out


def main() -> None:
    """창 실행 — LiveSystem을 뒤에서 돌리고 표를 주기 갱신."""
    import asyncio
    import threading
    import tkinter as tk
    from tkinter import ttk

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from .bootstrap import LiveSystem, bootstrap_live

    state = MonitorState()
    system_ref: dict[str, LiveSystem] = {}

    def run_live() -> None:
        async def _run() -> None:
            import aiohttp

            async with aiohttp.ClientSession() as http:
                system = await bootstrap_live(http)
                system_ref["system"] = system
                system.on_quote.append(state.on_quote)
                system.on_mark.append(state.on_mark)
                await system.start()
                await system.wait()

        asyncio.run(_run())

    threading.Thread(target=run_live, daemon=True).start()

    root = tk.Tk()
    root.title("kp-arb 시세")
    root.geometry("400x360")
    root.attributes("-topmost", True)  # 항상 위 (작은 시세창 용도)

    columns = ("kind", "bid", "ask", "mid")
    tree = ttk.Treeview(root, columns=columns, show="tree headings", height=12)
    tree.heading("#0", text="종목")
    tree.column("#0", width=90, anchor="w")
    for col, text, width in (("kind", "구분", 50), ("bid", "매수", 80),
                             ("ask", "매도", 80), ("mid", "중간/마크", 90)):
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="e")
    tree.pack(fill="both", expand=True, padx=4, pady=4)

    status = tk.Label(root, text="연결 중 ...", anchor="w", font=("Malgun Gothic", 9))
    status.pack(fill="x", padx=4, pady=(0, 4))

    def refresh() -> None:
        rows = state.rows()
        existing = tree.get_children()
        if len(existing) != len(rows):
            tree.delete(*existing)
            for row in rows:
                tree.insert("", "end", text=row[0], values=row[1:])
        else:
            for item, row in zip(existing, rows, strict=True):
                tree.item(item, text=row[0], values=row[1:])

        system = system_ref.get("system")
        if system is not None:
            phase = system.session.phase_for(Underlying.SAMSUNG).value
            stock = system.order_book.balance(Account.KR_STOCK)
            deriv = system.order_book.balance(Account.KR_DERIV)
            age = time.time() - state.last_update if state.last_update else -1
            fresh = f"{age:.0f}s 전" if age >= 0 else "-"
            status.config(text=f"세션 {phase} | 주식 {stock:,.0f} | "
                               f"선물 {deriv:,.0f} | 수신 {fresh}")
        root.after(300, refresh)

    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
