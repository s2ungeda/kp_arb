"""시세 모니터 — 코어(/monitor)를 읽어 그리는 얇은 클라이언트.

    python -m kp_arb.monitor

- 데이터는 코어(127.0.0.1:8787)의 /monitor 스냅샷에서 폴링(읽기 전용). 코어가 LS/HL 표와
  괴리 보드(진입/청산·예상체결·주문가)를 조립해 내려주고, 이 창은 렌더·입력만 한다.
- 네트워크는 뒷단 스레드가 하고, 화면은 after()로 저장된 결과만 읽는다(창 안 얼게 —
  CLAUDE.md: 화면 스레드에서 네트워크 호출 금지).

표 구성:
- LS: 종목 | 매도잔량 | 매도가 | 현재가 | 매수가 | 매수잔량 | 예상체결가 | 이론가(선물) | 괴리율%
- HL: 종목 | 매도가 | 현재가 | 오라클 | 매수가 | 마크 | 현-오라클% | 마크-오라클%
      | 펀딩전 | 펀딩피 | 남은시간
- 괴리 보드: 쌍 | 진입 | 청산 | 진입 est | 청산 est | 진입 주문가 | 청산 주문가
- 하단: 장운영상태 · 환율 · 계좌 잔고 · 마지막 수신 시각
"""
from __future__ import annotations

import time
from typing import Any

from . import win_state
from .core_client import core_request, watch_parent_exit
from .domain.enums import Underlying

_NAMES = {"samsung": "삼성전자", "sk_hynix": "SK하이닉스", "hyundai": "현대차"}
_KIND = {"kr_stock": "주식", "kr_stock_future": "선물", "kr_etf": "ETF"}

FUNDING_INTERVAL_S = 3600  # HL 펀딩은 매시 정각


def _fmt(value: float | None, *, decimals: int = 0) -> str:
    if value is None:
        return "-"
    return f"{value:,.{decimals}f}"


def _pct(value: float | None) -> str:
    """소수 비율(0.0123)을 % 문자열로 — 괴리 보드용(코어가 소수로 내려줌)."""
    return f"{value * 100:.3f}" if value is not None else "-"


def funding_countdown(now_epoch: float) -> str:
    """다음 펀딩(매시 정각)까지 남은 mm:ss."""
    remain = FUNDING_INTERVAL_S - int(now_epoch) % FUNDING_INTERVAL_S
    return f"{remain // 60:02d}:{remain % 60:02d}"


def ls_rows(snap: dict[str, Any]) -> list[tuple[str, ...]]:
    """LS 표 행 — 코어 스냅샷 그대로 렌더. 같은 종목은 첫 행(주식)에만 이름."""
    rows: list[tuple[str, ...]] = []
    last_u: str | None = None
    for r in snap.get("ls", []):
        u, inst = r["underlying"], r["instrument"]
        name = _NAMES.get(u, u) if u != last_u else ""
        last_u = u
        disp = r.get("disp")
        rows.append((
            f"{name} {_KIND.get(inst, inst)}".strip(),
            _fmt(r.get("ask_qty")), _fmt(r.get("ask")), _fmt(r.get("last")),
            _fmt(r.get("bid")), _fmt(r.get("bid_qty")), _fmt(r.get("expected")),
            _fmt(r.get("theory"), decimals=2),               # 엑셀과 동일 소수 2자리
            f"{disp:+.2f}" if disp is not None else "-",      # 괴리율%(코어 계산)
        ))
    return rows


def hl_rows(snap: dict[str, Any], now_epoch: float | None = None) -> list[tuple[str, ...]]:
    """HL 표 행 — 현/마크의 오라클 대비 %와 펀딩은 코어 스냅샷 값을 그대로 표시."""
    now = now_epoch if now_epoch is not None else time.time()
    countdown = funding_countdown(now)
    rows: list[tuple[str, ...]] = []
    for r in snap.get("hl", []):
        lvo, mvo = r.get("last_vs_oracle"), r.get("mark_vs_oracle")
        prev, nxt = r.get("funding_prev"), r.get("funding_next")
        rows.append((
            _NAMES.get(r["underlying"], r["underlying"]),
            _fmt(r.get("ask"), decimals=2), _fmt(r.get("last"), decimals=2),
            _fmt(r.get("oracle"), decimals=2), _fmt(r.get("bid"), decimals=2),
            _fmt(r.get("mark"), decimals=2),
            f"{lvo:+.3f}" if lvo is not None else "-",
            f"{mvo:+.3f}" if mvo is not None else "-",
            f"{prev * 100:.4f}%" if prev is not None else "-",
            f"{nxt * 100:.4f}%" if nxt is not None else "-",
            countdown,
        ))
    return rows


def board_rows(snap: dict[str, Any]) -> list[tuple[str, ...]]:
    """괴리 보드 행 — 진입/청산은 소수→%, est(USD)·주문가(원)는 코어 계산값."""
    rows: list[tuple[str, ...]] = []
    for r in sorted(snap.get("board", []),
                    key=lambda x: (x["underlying"], x["instrument"])):
        name = _NAMES.get(r["underlying"], r["underlying"])
        kind = _KIND.get(r["instrument"], r["instrument"])
        rows.append((
            f"{name}-{kind}",
            _pct(r.get("entry")), _pct(r.get("exit")),
            _fmt(r.get("est_bid"), decimals=4), _fmt(r.get("est_ask"), decimals=4),
            _fmt(r.get("px_entry"), decimals=0), _fmt(r.get("px_exit"), decimals=0),
        ))
    return rows


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """창 실행 — 코어 /monitor를 뒷단 스레드로 폴링, 화면은 그 결과만 그린다."""
    import queue
    import threading
    import tkinter as tk
    from collections.abc import Callable
    from tkinter import ttk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료(고아 방지)

    state_box: dict[str, Any] = {"data": None, "ts": 0.0}
    params: dict[str, Any] = {"qty": 1, "en": 0.0, "ex": 0.0}  # UI가 갱신, 폴러가 읽음
    jobs: queue.Queue[dict[str, Any]] = queue.Queue()

    def poller() -> None:
        while True:
            q = params  # 평범한 dict 읽기(UI 스레드가 갱신) — 한 틱 지연은 무해
            snap = core_request(
                f"/monitor?qty={q['qty']}&en={q['en']}&ex={q['ex']}", timeout=2.0)
            if snap is not None:
                state_box["data"] = snap
                state_box["ts"] = time.time()
            time.sleep(0.3)

    def sender() -> None:  # 호가단위 머지 명령 전송(네트워크는 뒷단)
        while True:
            core_request("/command", jobs.get(), timeout=5.0)

    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=sender, daemon=True).start()

    root = tk.Tk()
    root.title("시세")
    root.geometry("760x600")
    win_state.attach(root, "monitor")  # 마지막 창 위치 복원·저장
    font = ("Malgun Gothic", 9)

    topmost_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        root, text="항상 위", font=font, variable=topmost_var,
        command=lambda: root.attributes("-topmost", topmost_var.get()),
    ).pack(anchor="e", padx=6)

    def make_grid(
        title: str, cols: list[tuple[str, int, str]]
    ) -> Callable[[list[tuple[str, ...]]], None]:
        """라벨 격자 표 하나 만들고 채우기 함수 반환 — (제목, 글자폭, 글자색) 열 정의."""
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

    # HL 호가단위 머지(종목별) — 코어에 재구독 명령. est·호가창에 적용, 1호가 표시는 원시 유지.
    # 배수는 최소 호가단위 기준(184달러대: 원시 0.01 → 2배 0.02, 5배 0.05, 10배 0.1, 100배 1)
    agg_choices = {"원시": (None, None), "2배": (5, 2), "5배": (5, 5),
                   "10배": (4, None), "100배": (3, None)}
    agg_row = tk.Frame(root)
    agg_row.pack(fill="x", padx=4, pady=(2, 0))
    tk.Label(agg_row, text="HL 호가단위:", font=font).pack(side="left")

    def agg_handler(u: Underlying, combo: ttk.Combobox) -> Callable[[object], None]:
        def _apply(_event: object) -> None:
            n_sig_figs, mantissa = agg_choices[combo.get()]
            jobs.put({"cmd": "manual_hl_merge", "underlying": u.value,
                      "n_sig_figs": n_sig_figs, "mantissa": mantissa})
        return _apply

    for agg_u in Underlying:
        tk.Label(agg_row, text=_NAMES[agg_u.value], font=font).pack(side="left", padx=(8, 2))
        agg_combo = ttk.Combobox(agg_row, values=list(agg_choices), width=5,
                                 state="readonly", font=font)
        agg_combo.set("원시")
        agg_combo.pack(side="left")
        agg_combo.bind("<<ComboboxSelected>>", agg_handler(agg_u, agg_combo))

    # est 입력 — 수량(국내: 주식 쌍=주 1:1, 선물 쌍=계약 1:10 환산)·기준값(%). 코어가 계산.
    est_input = tk.Frame(root)
    est_input.pack(fill="x", padx=4, pady=(4, 0))
    tk.Label(est_input, text="수량(주식=주·선물=계약)", font=font).pack(side="left")
    ent_sets = tk.Entry(est_input, width=4, justify="right", font=font)
    ent_sets.insert(0, "1")
    ent_sets.pack(side="left", padx=(2, 8))
    tk.Label(est_input, text="진입기준%", font=font).pack(side="left")
    ent_s_en = tk.Entry(est_input, width=6, justify="right", font=font)
    ent_s_en.insert(0, "0")
    ent_s_en.pack(side="left", padx=(2, 8))
    tk.Label(est_input, text="청산기준%", font=font).pack(side="left")
    ent_s_ex = tk.Entry(est_input, width=6, justify="right", font=font)
    ent_s_ex.insert(0, "0")
    ent_s_ex.pack(side="left", padx=(2, 8))
    tk.Label(est_input, text="(LS주문가 = 기준값이 체결로 보장되는 maker 가격)",
             font=font, fg="gray40").pack(side="left")

    fill_board = make_grid(
        "괴리 보드 (%) — 진입=HL매수d−국내매수d(국내 maker)", [
            ("쌍", 13, "black"),
            ("진입", 8, "red"), ("청산", 8, "blue"),
            ("진입 est", 10, "red"), ("청산 est", 10, "blue"),   # HL est-pr 평균 체결가(USD)
            ("진입 주문가", 10, "darkred"), ("청산 주문가", 10, "darkblue"),  # 역산 LS maker(원)
        ])

    status = tk.Label(root, text="연결 중 ...", anchor="w", font=font)
    status.pack(fill="x", padx=4, pady=(0, 4))

    def _read_params() -> None:
        """UI 스레드에서 입력칸을 읽어 params 갱신(폴러가 다음 폴에 사용)."""
        try:
            params["qty"] = max(0, int(ent_sets.get().strip() or 0))
        except ValueError:
            params["qty"] = 1
        for key, entry in (("en", ent_s_en), ("ex", ent_s_ex)):
            try:
                params[key] = float(entry.get().strip() or 0.0)
            except ValueError:
                params[key] = 0.0

    def refresh() -> None:
        # 어떤 예외가 나도 다음 갱신 예약(finally)은 반드시 실행 — 1회 실패로 화면이 멈추지 않게.
        try:
            _read_params()
            snap = state_box.get("data")
            if not snap or not snap.get("connected"):
                status.config(text="코어 미접속 — 재시도 중 ...")
                return
            fill_ls(ls_rows(snap))
            fill_hl(hl_rows(snap))
            fill_board(board_rows(snap))
            fx = snap.get("fx") or {}
            bal = snap.get("balances") or {}
            # 환율 3개를 나란히 — 쓰는 쪽에 [ ]. 엑셀 시세!N11(현물CUR)·N12(선물역산)과 같은 배치.
            src = fx.get("src")
            spot, theory, fut = fx.get("spot"), fx.get("theory"), fx.get("futures")
            spot_src = fx.get("spot_src") or "-"

            def _n(v: Any, d: int = 2) -> str:
                return f"{v:,.{d}f}" if isinstance(v, (int, float)) else "-"

            spot_txt = f"현물CUR({spot_src}) {_n(spot)}"
            theory_txt = f"선물역산 {_n(theory)}"
            if src == "현물":
                spot_txt = f"[{spot_txt}]"
            elif src == "선물이론":
                theory_txt = f"[{theory_txt}]"
            fx_text = f"환율: {spot_txt} · {theory_txt} · 선물원값 {_n(fut, 1)}"
            age = time.time() - state_box["ts"] if state_box["ts"] else -1
            fresh = f"{age:.0f}s 전" if age >= 0 else "-"
            halt = snap.get("halt") or {}  # §8 정지 사유(주식/선물, 없으면 null)
            hp = [f"{m}:{halt[k]}" for k, m in (("stock", "주식"), ("futures", "선물"))
                  if halt.get(k)]
            halt_text = f" ⚠정지[{' '.join(hp)}]" if hp else ""
            status.config(
                text=f"장운영: {snap.get('phase', '-')}{halt_text} | {fx_text} | "
                     f"주식 {bal.get('stock', 0):,.0f} | 선물 {bal.get('deriv', 0):,.0f} | "
                     f"수신 {fresh}")
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
    # 콘솔로 Ctrl-C 신호가 흘러들어도(창 닫기 과정 등) 화면을 죽이지 않는다. 종료는 창 닫기(X)로.
    while True:
        try:
            root.mainloop()
            break  # 창이 닫혀 정상 종료
        except KeyboardInterrupt:
            try:
                root.winfo_exists()
            except tk.TclError:
                break  # 창도 이미 닫힘 — 종료


if __name__ == "__main__":
    main()
