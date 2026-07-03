"""시세 모니터 — 취급 종목 전체를 한눈에 보는 컴팩트 데스크톱 창.

    python -m kp_arb.monitor

- 창은 tkinter(파이썬 기본 포함 — 추가 설치 없음), 항상 위 고정.
- 데이터는 LiveSystem(백그라운드 스레드의 asyncio)에서 실시간 수신,
  화면은 0.3초마다 최신값을 읽어 갱신(읽기 전용 — 주문 없음).

표 구성(사용자 명세):
- LS: 종목 | 매도잔량 | 매도가 | 현재가 | 매수가 | 매수잔량 | 예상가
- HL: 종목 | 매도잔량 | 매도가 | 현재가(마크) | 매수가 | 매수잔량 | 펀딩전 | 펀딩피 | 남은시간
- 하단: 장운영상태 · 계좌 잔고 · 마지막 수신 시각
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .domain.enums import Account, Instrument, Underlying
from .domain.models import Quote
from .gateways.hl import Mark
from .gateways.ls_ws import ExpectedPrice, TradeTick

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
_LS_INSTRUMENTS = (Instrument.KR_STOCK, Instrument.KR_STOCK_FUTURE, Instrument.KR_ETF)

FUNDING_INTERVAL_S = 3600  # HL 펀딩은 매시 정각


def _fmt(value: float | None, *, decimals: int = 0) -> str:
    if value is None:
        return "-"
    return f"{value:,.{decimals}f}"


def funding_countdown(now_epoch: float) -> str:
    """다음 펀딩(매시 정각)까지 남은 mm:ss."""
    remain = FUNDING_INTERVAL_S - int(now_epoch) % FUNDING_INTERVAL_S
    return f"{remain // 60:02d}:{remain % 60:02d}"


@dataclass
class MonitorState:
    """실시간 콜백이 채우고 화면이 읽는 최신값 저장소(읽기 전용 표시용).

    호가는 시장(KRX/NXT)별로 보관하고, 표시는 HTS처럼 **통합**(두 시장 중
    더 좋은 호가 — 매수는 높은 쪽, 매도는 낮은 쪽)으로 계산한다.
    """

    quotes: dict[tuple[Underlying, Instrument, str], Quote] = field(default_factory=dict)
    trades: dict[tuple[Underlying, Instrument], float] = field(default_factory=dict)
    expected: dict[Underlying, float] = field(default_factory=dict)
    marks: dict[Underlying, float] = field(default_factory=dict)
    funding_next: dict[Underlying, float] = field(default_factory=dict)  # 예정(펀딩피)
    funding_prev: dict[Underlying, float] = field(default_factory=dict)  # 직전
    last_update: float = 0.0

    # --- 실시간 콜백 ---

    def on_quote(self, quote: Quote) -> None:
        self.quotes[(quote.underlying, quote.instrument, quote.market)] = quote
        self.last_update = time.time()

    def merged_quote(
        self, underlying: Underlying, instrument: Instrument
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """KRX+NXT 통합 최우선호가: (매도가, 매도잔량, 매수가, 매수잔량)."""
        krx = self.quotes.get((underlying, instrument, "krx"))
        nxt = self.quotes.get((underlying, instrument, "nxt"))
        candidates = [q for q in (krx, nxt) if q is not None]
        if not candidates:
            return None, None, None, None
        best_ask = min(candidates, key=lambda q: q.ask)   # 매도는 낮은 쪽이 우선
        best_bid = max(candidates, key=lambda q: q.bid)   # 매수는 높은 쪽이 우선
        return best_ask.ask, best_ask.ask_qty, best_bid.bid, best_bid.bid_qty

    def on_trade(self, tick: TradeTick) -> None:
        self.trades[(tick.underlying, tick.instrument)] = tick.price
        self.last_update = time.time()

    def on_expected(self, expected: ExpectedPrice) -> None:
        self.expected[expected.underlying] = expected.price
        self.last_update = time.time()

    def on_mark(self, mark: Mark) -> None:
        self.marks[mark.underlying] = mark.price
        self.last_update = time.time()

    def on_funding(self, underlying: Underlying, rate: float) -> None:
        self.funding_next[underlying] = rate

    # --- 화면 행 ---

    def ls_rows(self) -> list[tuple[str, ...]]:
        """LS 표: (종목, 매도잔량, 매도가, 현재가, 매수가, 매수잔량, 예상가)."""
        rows: list[tuple[str, ...]] = []
        for u in Underlying:
            name = _NAMES[u]
            for inst in _LS_INSTRUMENTS:
                ask, ask_qty, bid, bid_qty = self.merged_quote(u, inst)  # KRX+NXT 통합
                trade = self.trades.get((u, inst))
                expected = self.expected.get(u) if inst is Instrument.KR_STOCK else None
                rows.append((
                    f"{name} {_KIND[inst]}".strip(),
                    _fmt(ask_qty),
                    _fmt(ask),
                    _fmt(trade),
                    _fmt(bid),
                    _fmt(bid_qty),
                    _fmt(expected),
                ))
                name = ""  # 같은 종목은 첫 행에만 이름
        return rows

    def hl_rows(self, now_epoch: float | None = None) -> list[tuple[str, ...]]:
        """HL 표 행 — 현재가는 실제 체결가, 마크(청산·펀딩 기준가)는 별도 컬럼.

        (종목, 매도잔량, 매도가, 현재가, 매수가, 매수잔량, 마크, 펀딩전, 펀딩피, 남은시간)
        """
        now = now_epoch if now_epoch is not None else time.time()
        countdown = funding_countdown(now)
        rows: list[tuple[str, ...]] = []
        for u in Underlying:
            quote = self.quotes.get((u, Instrument.HL_PERP, "hl"))
            prev = self.funding_prev.get(u)
            nxt = self.funding_next.get(u)
            rows.append((
                _NAMES[u],
                _fmt(quote.ask_qty if quote else None, decimals=3),
                _fmt(quote.ask if quote else None, decimals=2),
                _fmt(self.trades.get((u, Instrument.HL_PERP)), decimals=2),  # 체결가만
                _fmt(quote.bid if quote else None, decimals=2),
                _fmt(quote.bid_qty if quote else None, decimals=3),
                _fmt(self.marks.get(u), decimals=2),  # 마크(기준가) 별도 표시
                f"{prev * 100:.4f}%" if prev is not None else "-",
                f"{nxt * 100:.4f}%" if nxt is not None else "-",
                countdown,
            ))
        return rows


def main() -> None:
    """창 실행 — LiveSystem을 뒤에서 돌리고 두 표를 주기 갱신."""
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
        async def _prev_funding_loop(system: LiveSystem) -> None:
            hl = system._hl  # noqa: SLF001 - 모니터 전용 읽기 접근
            if hl is None or not hasattr(hl, "get_prev_funding"):
                return
            while True:
                for u in Underlying:
                    try:
                        state.funding_prev[u] = await hl.get_prev_funding(u)
                    except Exception:  # noqa: BLE001 - 표시용, 실패해도 계속
                        pass
                await asyncio.sleep(FUNDING_INTERVAL_S / 4)

        async def _initial_prices(system: LiveSystem) -> None:
            # 창 오픈 시 최초 1회: LS 현재가(마감 후엔 종가) 조회로 표를 채운다.
            try:
                for key, price in (await system.price_snapshots()).items():
                    state.trades.setdefault(key, price)  # 실시간 체결이 오면 그쪽 우선
            except Exception:  # noqa: BLE001 - 표시용, 실패해도 실시간은 계속
                pass

        async def _run() -> None:
            import aiohttp

            async with aiohttp.ClientSession() as http:
                system = await bootstrap_live(http)
                system_ref["system"] = system
                system.on_quote.append(state.on_quote)
                system.on_trade.append(state.on_trade)
                system.on_expected.append(state.on_expected)
                system.on_mark.append(state.on_mark)
                system.on_funding.append(state.on_funding)
                await system.start()
                funding_task = asyncio.create_task(_prev_funding_loop(system))
                prices_task = asyncio.create_task(_initial_prices(system))
                try:
                    await system.wait()
                finally:
                    funding_task.cancel()
                    prices_task.cancel()

        asyncio.run(_run())

    threading.Thread(target=run_live, daemon=True).start()

    root = tk.Tk()
    root.title("kp-arb 시세")
    root.geometry("620x420")
    root.attributes("-topmost", True)  # 항상 위 (작은 시세창 용도)
    font = ("Malgun Gothic", 9)

    def make_tree(parent: tk.Misc, columns: list[tuple[str, str, int]],
                  height: int) -> ttk.Treeview:
        ids = [c[0] for c in columns]
        tree = ttk.Treeview(parent, columns=ids, show="headings", height=height)
        for cid, text, width in columns:
            tree.heading(cid, text=text)
            if cid == "name":
                tree.column(cid, width=width, anchor="w", stretch=False)
            else:
                tree.column(cid, width=width, anchor="e", stretch=False)
        tree.pack(fill="x", padx=4, pady=(2, 4))
        return tree

    tk.Label(root, text="LS (국내)", anchor="w", font=font).pack(fill="x", padx=4)
    ls_tree = make_tree(root, [
        ("name", "종목", 110), ("ask_qty", "매도잔량", 70), ("ask", "매도가", 80),
        ("last", "현재가", 80), ("bid", "매수가", 80), ("bid_qty", "매수잔량", 70),
        ("exp", "예상체결가", 85),
    ], height=9)

    tk.Label(root, text="HL (Hyperliquid)", anchor="w", font=font).pack(fill="x", padx=4)
    hl_tree = make_tree(root, [
        ("name", "종목", 85), ("ask_qty", "매도잔량", 58), ("ask", "매도가", 62),
        ("last", "현재가", 62), ("bid", "매수가", 62), ("bid_qty", "매수잔량", 58),
        ("mark", "마크", 62), ("fprev", "펀딩전", 60), ("fnext", "펀딩피", 60),
        ("cd", "남은시간", 52),
    ], height=3)

    status = tk.Label(root, text="연결 중 ...", anchor="w", font=font)
    status.pack(fill="x", padx=4, pady=(0, 4))

    def fill_tree(tree: ttk.Treeview, rows: list[tuple[str, ...]]) -> None:
        existing = tree.get_children()
        if len(existing) != len(rows):
            tree.delete(*existing)
            for row in rows:
                tree.insert("", "end", values=row)
        else:
            for item, row in zip(existing, rows, strict=True):
                tree.item(item, values=row)

    def refresh() -> None:
        fill_tree(ls_tree, state.ls_rows())
        fill_tree(hl_tree, state.hl_rows())
        system = system_ref.get("system")
        if system is not None:
            phase = system.session.phase_for(Underlying.SAMSUNG).value
            stock = system.order_book.balance(Account.KR_STOCK)
            deriv = system.order_book.balance(Account.KR_DERIV)
            age = time.time() - state.last_update if state.last_update else -1
            fresh = f"{age:.0f}s 전" if age >= 0 else "-"
            status.config(text=f"장운영: {phase} | 주식 {stock:,.0f} | "
                               f"선물 {deriv:,.0f} | 수신 {fresh}")
        root.after(300, refresh)

    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
