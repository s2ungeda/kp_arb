"""시세 모니터 — 취급 종목 전체를 한눈에 보는 컴팩트 데스크톱 창.

    python -m kp_arb.monitor

- 창은 tkinter(파이썬 기본 포함 — 추가 설치 없음). '항상 위'는 체크박스로 선택.
- 데이터는 LiveSystem(백그라운드 스레드의 asyncio)에서 실시간 수신,
  화면은 0.3초마다 최신값을 읽어 갱신(읽기 전용 — 주문 없음).

표 구성(사용자 명세):
- LS: 종목 | 매도잔량 | 매도가 | 현재가 | 매수가 | 매수잔량 | 예상가 | 이론가(선물) | 괴리율%
- HL: 종목 | 매도가 | 현재가 | 오라클 | 매수가 | 마크 | 현-오라클% | 마크-오라클%
      | 펀딩전 | 펀딩피 | 남은시간
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
# ETF는 취급 제외(2026-07-13) — 표시 기본은 주식+선물만.
_LS_INSTRUMENTS = (Instrument.KR_STOCK, Instrument.KR_STOCK_FUTURE)

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
        instruments: tuple[Instrument, ...] = _LS_INSTRUMENTS,
    ) -> list[tuple[str, ...]]:
        """LS 표: (종목, 매도잔량, 매도가, 현재가, 매수가, 매수잔량, 예상가, 이론가, 괴리율%).

        이론가는 선물(캐리 합성)·ETF(iNAV) 행에 표시 — LiveSystem과 공용 계산.
        괴리율 = (현재가 − 이론가) ÷ 이론가 × 100. 미취급 상품은 instruments로 제외.
        """
        rows: list[tuple[str, ...]] = []
        for u in Underlying:
            name = _NAMES[u]
            for inst in instruments:
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

    def hl_rows(self, now_epoch: float | None = None) -> list[tuple[str, ...]]:
        """HL 표 행 — 현재가는 실제 체결가, 마크(청산·펀딩 기준가)는 별도 컬럼.

        (종목, 매도가, 현재가, 오라클, 매수가, 마크, 현-오라클%, 마크-오라클%,
         펀딩전, 펀딩피, 남은시간). 오라클 대비 비율 = (값 − 오라클) ÷ 오라클 × 100.
        """
        now = now_epoch if now_epoch is not None else time.time()
        countdown = funding_countdown(now)
        rows: list[tuple[str, ...]] = []
        for u in Underlying:
            quote = self.quotes.get((u, Instrument.HL_PERP, "hl"))
            prev = self.funding_prev.get(u)
            nxt = self.funding_next.get(u)
            last = self.trades.get((u, Instrument.HL_PERP))
            mark = self.marks.get(u)
            oracle = self.oracles.get(u)
            last_vs_oracle = disparity_pct(last, oracle)
            mark_vs_oracle = disparity_pct(mark, oracle)
            rows.append((
                _NAMES[u],
                _fmt(quote.ask if quote else None, decimals=2),
                _fmt(last, decimals=2),               # 현재가 = 체결가만
                _fmt(oracle, decimals=2),             # 오라클(지수가, 엑셀 C7)
                _fmt(quote.bid if quote else None, decimals=2),
                _fmt(mark, decimals=2),               # 마크(기준가) 별도 표시
                f"{last_vs_oracle:+.3f}" if last_vs_oracle is not None else "-",
                f"{mark_vs_oracle:+.3f}" if mark_vs_oracle is not None else "-",
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
    from collections.abc import Callable

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
    font = ("Malgun Gothic", 9)

    # 항상 위 — 창이 커져서 기본은 해제, 필요할 때 체크로 켠다.
    topmost_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        root, text="항상 위", font=font, variable=topmost_var,
        command=lambda: root.attributes("-topmost", topmost_var.get()),
    ).pack(anchor="e", padx=6)

    def make_grid(
        title: str, cols: list[tuple[str, int, str]]
    ) -> Callable[[list[tuple[str, ...]]], None]:
        """라벨 격자 표 하나 만들고 채우기 함수 반환 — (제목, 글자폭, 글자색) 열 정의.

        셀 사이 1px 간격으로 배경(회색)이 비쳐 표 테두리처럼 보이고,
        열별 글자색(예: 진입 빨강/청산 파랑)을 지원한다.
        """
        tk.Label(root, text=title, anchor="w", font=font).pack(fill="x", padx=4)
        frame = tk.Frame(root, bg="#c8c8c8", bd=1, relief="solid")
        frame.pack(fill="x", padx=6, pady=(2, 4))
        for col, (head, width, _) in enumerate(cols):
            tk.Label(frame, text=head, font=font, width=width, bg="#f0f0f0",
                     anchor="w" if col == 0 else "e").grid(
                row=0, column=col, sticky="ew", padx=(0, 1), pady=(0, 1))
        grid_labels: list[list[tk.Label]] = []

        def fill(rows: list[tuple[str, ...]]) -> None:
            if len(grid_labels) != len(rows):  # 행 수 변화 시 격자 재구성
                for row_labels in grid_labels:
                    for label in row_labels:
                        label.destroy()
                grid_labels.clear()
                for r in range(len(rows)):
                    row_labels = []
                    for col, (_, width, color) in enumerate(cols):
                        label = tk.Label(frame, font=font, width=width, fg=color,
                                         bg="white", anchor="w" if col == 0 else "e")
                        label.grid(row=r + 1, column=col, sticky="ew",
                                   padx=(0, 1), pady=(0, 1))
                        row_labels.append(label)
                    grid_labels.append(row_labels)
            for row_labels, row in zip(grid_labels, rows, strict=True):
                for label, value in zip(row_labels, row, strict=True):
                    label.config(text=value)

        return fill

    fill_ls = make_grid("LS (국내)", [
        ("종목", 13, "black"), ("매도잔량", 9, "black"), ("매도가", 10, "black"),
        ("현재가", 10, "black"), ("매수가", 10, "black"), ("매수잔량", 9, "black"),
        ("예상체결가", 10, "black"), ("이론가", 11, "black"), ("괴리율%", 7, "black"),
    ])
    fill_hl = make_grid("HL (Hyperliquid)", [
        ("종목", 9, "black"), ("매도가", 8, "black"),
        ("현재가", 8, "black"), ("오라클", 8, "black"),
        ("매수가", 8, "black"), ("마크", 8, "black"),
        ("현-오라클%", 9, "black"), ("마크-오라클%", 10, "black"),
        ("펀딩전", 8, "black"), ("펀딩피", 8, "black"), ("남은시간", 7, "black"),
    ])
    # 구성요소(HL/국내 매도·매수 disp, 왕복비용)는 화면에서 제외 — CSV에는 계속 기록.
    fill_board = make_grid(
        "괴리 보드 (%) — 진입=HL매수d−국내매수d(국내 maker) · 순진입 ≥ 0 진입 / 순청산 ≤ 0 청산", [
            ("쌍", 13, "black"),
            ("주H차", 8, "black"),    # 엑셀 메인 I22 (HL 현재가 괴리)
            ("주선차", 8, "black"),   # 엑셀 메인 K19/M19 (국내 현재가 괴리)
            ("진입", 8, "red"),
            ("청산", 8, "blue"),
            ("순진입", 8, "darkred"),   # 진입 − 왕복호가비용/2 − 수수료 (수렴 시 기대 %)
            ("순청산", 8, "darkblue"),  # 청산 − 왕복호가비용/2 (≤0 = 수렴 완료)
        ])

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
                pct(pair.net_entry),                            # 순진입 (수렴 시 기대 %)
                pct(pair.net_exit),                             # 순청산 (≤0 = 수렴 완료)
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
             pct(p.kr_last),                                  # 국내 현재가 괴리 (메인 K19/M19)
             pct(p.net_entry),                                # 순진입
             pct(p.net_exit))                                 # 순청산
            for (u, inst), p in board.items()
            if p.spread.entry is not None or p.spread.exit is not None
        ]
        if not lines:
            return
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        path = log_dir / f"spread_{time.strftime('%Y%m%d')}.csv"
        new_file = not path.exists()
        try:
            with path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if new_file:
                    writer.writerow(["time", "underlying", "pair",
                                     "hl_ask_d", "hl_bid_d", "kr_ask_d", "kr_bid_d",
                                     "entry", "exit", "usdkrw_theory", "base_last",
                                     "hl_last_d", "kr_last_d", "net_entry", "net_exit"])
                writer.writerows(lines)
        except OSError:
            pass  # 파일을 엑셀 등이 잠근 상태 — 이번 기록은 건너뛰고 풀리면 재개

    def refresh() -> None:
        # 어떤 예외가 나도 다음 갱신 예약(finally)은 반드시 실행 —
        # 갱신 1회 실패로 화면이 통째로 멈추던 문제 방지.
        try:
            system = system_ref.get("system")
            theory = None
            if system is not None:
                theory = {
                    (u, Instrument.KR_STOCK_FUTURE): system.stock_futures_theory(u)
                    for u in Underlying
                }
            fill_ls(state.ls_rows(theory))
            fill_hl(state.hl_rows())
            if system is not None:
                fill_board(board_rows(system))
                record_spreads(system)
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
        except tk.TclError:
            return  # 창이 닫히는 중 — 다음 예약 없이 조용히 종료
        except Exception:  # noqa: BLE001 - 1회 실패는 기록만 하고 계속
            import logging

            logging.getLogger("kp_arb.monitor").exception("화면 갱신 실패 — 계속")
        finally:
            try:
                root.after(300, refresh)
            except tk.TclError:
                pass  # 창 닫힘 — 갱신 루프 종료

    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
