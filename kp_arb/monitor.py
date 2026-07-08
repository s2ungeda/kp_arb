"""시세 모니터 — 취급 종목 전체를 한눈에 보는 컴팩트 데스크톱 창.

    python -m kp_arb.monitor

- 창은 tkinter(파이썬 기본 포함 — 추가 설치 없음), 항상 위 고정.
- 데이터는 LiveSystem(백그라운드 스레드의 asyncio)에서 실시간 수신,
  화면은 0.3초마다 최신값을 읽어 갱신(읽기 전용 — 주문 없음).

표 구성(사용자 명세):
- LS: 종목 | 매도잔량 | 매도가 | 현재가 | 매수가 | 매수잔량 | 예상가 | 이론가(선물·ETF) | 괴리율%
- HL: 종목 | 매도잔량 | 매도가 | 현재가(마크) | 매수가 | 매수잔량 | 펀딩전 | 펀딩피 | 남은시간
- 하단: 장운영상태 · 계좌 잔고 · 마지막 수신 시각
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .domain.enums import Account, Instrument, Underlying
from .domain.models import Quote
from .etf_theory import disparity_pct
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
    expected: dict[tuple[Underlying, Instrument], float] = field(default_factory=dict)
    marks: dict[Underlying, float] = field(default_factory=dict)
    oracles: dict[Underlying, float] = field(default_factory=dict)  # HL 오라클(지수가)
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
        """통합(uni)·KRX·NXT 중 최우선호가: (매도가, 매도잔량, 매수가, 매수잔량)."""
        candidates = [
            q for m in ("uni", "krx", "nxt")
            if (q := self.quotes.get((underlying, instrument, m))) is not None
        ]
        if not candidates:
            return None, None, None, None
        best_ask = min(candidates, key=lambda q: q.ask)   # 매도는 낮은 쪽이 우선
        best_bid = max(candidates, key=lambda q: q.bid)   # 매수는 높은 쪽이 우선
        return best_ask.ask, best_ask.ask_qty, best_bid.bid, best_bid.bid_qty

    def on_trade(self, tick: TradeTick) -> None:
        self.trades[(tick.underlying, tick.instrument)] = tick.price
        self.last_update = time.time()

    def on_expected(self, expected: ExpectedPrice) -> None:
        self.expected[(expected.underlying, expected.instrument)] = expected.price
        self.last_update = time.time()

    def on_mark(self, mark: Mark) -> None:
        self.marks[mark.underlying] = mark.price
        if mark.oracle is not None:
            self.oracles[mark.underlying] = mark.oracle
        self.last_update = time.time()

    def on_funding(self, underlying: Underlying, rate: float) -> None:
        self.funding_next[underlying] = rate

    # --- 화면 행 ---

    def ls_rows(
        self,
        theory: dict[tuple[Underlying, Instrument], float | None] | None = None,
    ) -> list[tuple[str, ...]]:
        """LS 표: (종목, 매도잔량, 매도가, 현재가, 매수가, 매수잔량, 예상가, 이론가, 괴리율%).

        이론가는 선물(캐리 합성)·ETF(iNAV) 행에 표시 — LiveSystem과 공용 계산.
        괴리율 = (현재가 − 이론가) ÷ 이론가 × 100.
        """
        rows: list[tuple[str, ...]] = []
        for u in Underlying:
            name = _NAMES[u]
            for inst in _LS_INSTRUMENTS:
                ask, ask_qty, bid, bid_qty = self.merged_quote(u, inst)  # KRX+NXT 통합
                trade = self.trades.get((u, inst))
                expected = self.expected.get((u, inst))  # 주식·선물·ETF 모두
                inst_theory = (theory or {}).get((u, inst))
                disp = disparity_pct(trade, inst_theory)
                rows.append((
                    f"{name} {_KIND[inst]}".strip(),
                    _fmt(ask_qty),
                    _fmt(ask),
                    _fmt(trade),
                    _fmt(bid),
                    _fmt(bid_qty),
                    _fmt(expected),
                    _fmt(inst_theory, decimals=2),  # 엑셀과 동일 소수 2자리
                    f"{disp:+.2f}" if disp is not None else "-",
                ))
                name = ""  # 같은 종목은 첫 행에만 이름
        return rows

    def hl_rows(
        self, now_epoch: float | None = None, fx: float | None = None
    ) -> list[tuple[str, ...]]:
        """HL 표 행 — 현재가는 실제 체결가, 마크(청산·펀딩 기준가)는 별도 컬럼.

        (종목, 매도잔량, 매도가, 현재가, 오라클, 매수가, 매수잔량, 마크, 원화환산,
         펀딩전, 펀딩피, 남은시간). 원화환산 = HL 현재가 × 환율이론가 (엑셀 AA7).
        """
        now = now_epoch if now_epoch is not None else time.time()
        countdown = funding_countdown(now)
        rows: list[tuple[str, ...]] = []
        for u in Underlying:
            quote = self.quotes.get((u, Instrument.HL_PERP, "hl"))
            prev = self.funding_prev.get(u)
            nxt = self.funding_next.get(u)
            last = self.trades.get((u, Instrument.HL_PERP))
            krw = last * fx if last is not None and fx is not None else None
            rows.append((
                _NAMES[u],
                _fmt(quote.ask_qty if quote else None, decimals=3),
                _fmt(quote.ask if quote else None, decimals=2),
                _fmt(last, decimals=2),               # 현재가 = 체결가만
                _fmt(self.oracles.get(u), decimals=2),  # 오라클(지수가, 엑셀 C7)
                _fmt(quote.bid if quote else None, decimals=2),
                _fmt(quote.bid_qty if quote else None, decimals=3),
                _fmt(self.marks.get(u), decimals=2),  # 마크(기준가) 별도 표시
                _fmt(krw),                            # 원화환산 (엑셀 AA7)
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
                await system.start()  # 초기 가격 시딩은 LiveSystem이 담당(합성 체결로 수신)
                funding_task = asyncio.create_task(_prev_funding_loop(system))
                try:
                    await system.wait()
                finally:
                    funding_task.cancel()

        asyncio.run(_run())

    threading.Thread(target=run_live, daemon=True).start()

    root = tk.Tk()
    root.title("kp-arb 시세")
    root.geometry("760x600")
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
        ("exp", "예상체결가", 85), ("theory", "이론가", 80), ("disp", "괴리율%", 58),
    ], height=9)

    tk.Label(root, text="HL (Hyperliquid)", anchor="w", font=font).pack(fill="x", padx=4)
    hl_tree = make_tree(root, [
        ("name", "종목", 85), ("ask_qty", "매도잔량", 58), ("ask", "매도가", 62),
        ("last", "현재가", 62), ("oracle", "오라클", 62),
        ("bid", "매수가", 62), ("bid_qty", "매수잔량", 58),
        ("mark", "마크", 62), ("krw", "원화환산", 76),
        ("fprev", "펀딩전", 60), ("fnext", "펀딩피", 60),
        ("cd", "남은시간", 52),
    ], height=3)

    tk.Label(root, text="괴리 보드 (%, HL vs 국내 — 진입=HL매수d−국내매도d)",
             anchor="w", font=font).pack(fill="x", padx=4)
    # 열별 글자색이 필요해서(진입 빨강/청산 파랑) 표 대신 라벨 격자 사용.
    BOARD_COLS: list[tuple[str, int, str]] = [  # (제목, 글자폭, 글자색)
        ("쌍", 14, "black"),
        ("주H차", 8, "black"),    # 엑셀 메인 I22 (HL 현재가 괴리)
        ("주선차", 8, "black"),   # 엑셀 메인 K19/M19 (국내 현재가 괴리)
        ("진입", 8, "red"),
        ("청산", 8, "blue"),
        ("HL매도d", 8, "black"), ("HL매수d", 8, "black"),
        ("국내매도d", 9, "black"), ("국내매수d", 9, "black"),
    ]
    board_frame = tk.Frame(root, bg="white")
    board_frame.pack(fill="x", padx=6, pady=(2, 4))
    for col, (title, width, _) in enumerate(BOARD_COLS):
        tk.Label(board_frame, text=title, font=font, width=width, bg="white",
                 anchor="w" if col == 0 else "e").grid(row=0, column=col, sticky="ew")
    board_labels: list[list[tk.Label]] = []

    def fill_board(rows: list[tuple[str, ...]]) -> None:
        if len(board_labels) != len(rows):  # 행 수 변화 시 격자 재구성
            for row_labels in board_labels:
                for label in row_labels:
                    label.destroy()
            board_labels.clear()
            for r in range(len(rows)):
                row_labels = []
                for col, (_, width, color) in enumerate(BOARD_COLS):
                    label = tk.Label(board_frame, font=font, width=width, fg=color,
                                     bg="white", anchor="w" if col == 0 else "e")
                    label.grid(row=r + 1, column=col, sticky="ew")
                    row_labels.append(label)
                board_labels.append(row_labels)
        for row_labels, row in zip(board_labels, rows, strict=True):
            for label, value in zip(row_labels, row, strict=True):
                label.config(text=value)

    status = tk.Label(root, text="연결 중 ...", anchor="w", font=font)
    status.pack(fill="x", padx=4, pady=(0, 4))

    def pct(value: float | None) -> str:
        return f"{value * 100:.3f}" if value is not None else "-"

    _PAIR_KIND = {Instrument.KR_STOCK_FUTURE: "SF", Instrument.KR_ETF: "ETF"}

    def board_rows(system: object) -> list[tuple[str, ...]]:
        from .bootstrap import LiveSystem

        assert isinstance(system, LiveSystem)
        rows: list[tuple[str, ...]] = []
        for (u, inst), pair in sorted(
            system.disparity_board().items(), key=lambda kv: (kv[0][0].value, kv[0][1].value)
        ):
            rows.append((
                f"{_NAMES[u]}-{_PAIR_KIND[inst]}",
                pct(pair.hl_last),                              # I22
                pct(pair.kr_last),                              # K19/M19
                pct(pair.spread.entry), pct(pair.spread.exit),  # K22/K24
                pct(pair.hl.ask), pct(pair.hl.bid),
                pct(pair.kr.ask), pct(pair.kr.bid),
            ))
        return rows

    last_csv = {"t": 0.0}

    def record_spreads(system: object) -> None:
        """1초 간격으로 스프레드를 CSV에 기록 — 임계값 결정용 분포 데이터."""
        import csv
        from pathlib import Path

        from .bootstrap import LiveSystem

        assert isinstance(system, LiveSystem)
        now = time.time()
        if now - last_csv["t"] < 1.0:
            return
        last_csv["t"] = now
        board = system.disparity_board()
        fx = system.usdkrw_theory
        lines = [
            (f"{time.strftime('%H:%M:%S', time.localtime(now))}",
             u.value, _PAIR_KIND[inst],
             pct(p.hl.ask), pct(p.hl.bid), pct(p.kr.ask), pct(p.kr.bid),
             pct(p.spread.entry), pct(p.spread.exit),
             f"{fx:.4f}" if fx is not None else "-",          # 환율이론가 (엑셀 I1 대응)
             _fmt(system.stock_last(u), decimals=0),          # 기초 현재가 (엑셀 D60/D58)
             pct(p.hl_last),                                  # HL 현재가 괴리 (메인 I22)
             pct(p.kr_last))                                  # 국내 현재가 괴리 (메인 K19/M19)
            for (u, inst), p in board.items()
            if p.spread.entry is not None or p.spread.exit is not None
        ]
        if not lines:
            return
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        path = log_dir / f"spread_{time.strftime('%Y%m%d')}.csv"
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["time", "underlying", "pair",
                                 "hl_ask_d", "hl_bid_d", "kr_ask_d", "kr_bid_d",
                                 "entry", "exit", "usdkrw_theory", "base_last",
                                 "hl_last_d", "kr_last_d"])
            writer.writerows(lines)

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
        system = system_ref.get("system")
        theory = None
        if system is not None:
            theory = {}
            for u in Underlying:
                theory[(u, Instrument.KR_ETF)] = system.etf_theory_price(u)
                theory[(u, Instrument.KR_STOCK_FUTURE)] = system.stock_futures_theory(u)
        fill_tree(ls_tree, state.ls_rows(theory))
        fill_tree(hl_tree, state.hl_rows(
            fx=system.usdkrw_theory if system is not None else None))
        if system is not None:
            fill_board(board_rows(system))
            record_spreads(system)
        if system is not None:
            phase = system.session.phase_for(Underlying.SAMSUNG).value
            stock = system.order_book.balance(Account.KR_STOCK)
            deriv = system.order_book.balance(Account.KR_DERIV)
            age = time.time() - state.last_update if state.last_update else -1
            fresh = f"{age:.0f}s 전" if age >= 0 else "-"
            fx_fut = system.usdkrw_futures
            fx_theory = system.usdkrw_theory
            fx_text = (f"환율 {fx_fut:,.1f} (이론 {fx_theory:,.2f})"
                       if fx_fut is not None and fx_theory is not None else "환율 -")
            status.config(text=f"장운영: {phase} | {fx_text} | 주식 {stock:,.0f} | "
                               f"선물 {deriv:,.0f} | 수신 {fresh}")
        root.after(300, refresh)

    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
