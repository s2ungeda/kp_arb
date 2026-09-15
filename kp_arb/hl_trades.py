"""HL 체결 창 — 선택한 종목의 HL 공개 체결 최근 30건(체결시각·매도/매수·체결가·체결수량).

    python -m kp_arb.hl_trades   (운영은 메인 메뉴 "HL 체결")

- 데이터는 코어가 HL WS trades로 받아 종목별 30건을 보관(LiveSystem.hl_trades)하고 실시간
  `trades` 채널(DESIGN §12.1 3차)로 밀어준다 — 메인이 공유 파일에 쓰고 이 창은 뒷단 스레드가
  0.1초마다 읽어 표만 그린다(사용자 2026-09-15: 별도 창, 30줄). 공유 파일이 없거나 낡으면
  GET /hl_trades 폴링으로 폴백(run_state_feed). 네트워크는 화면 스레드에서 하지 않는다.
  (처음 0.3초 HTTP 폴링은 HL 홈페이지 체결 탭보다 0.5초쯤 늦어 보여 채널로 바꿈 — 사용자 실측.)
- 매도/매수는 HL 앱 표기 그대로(B=매수, A=매도 — 사용자 확정 2026-09-15). 색은 화면 규약
  (DESIGN-ui §3: 매수=빨강, 매도=파랑). 최신 체결이 맨 위.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import ui_theme as T
from . import win_state
from .core_client import box_is_live, run_state_feed, watch_parent_exit

_NAMES = {"samsung": "삼성전자", "sk_hynix": "SK하이닉스", "hyundai": "현대차"}
_CODES = {v: k for k, v in _NAMES.items()}
_SIDE_KO = {"buy": "매수", "sell": "매도"}


def rows_for(data: Any, underlying: str) -> list[dict[str, Any]]:
    """채널·폴백 본문({"trades": {종목: [...]}})에서 이 창 종목의 행만(최신이 위). (순수 함수)"""
    if not isinstance(data, dict):
        return []
    trades = data.get("trades")
    rows = trades.get(underlying) if isinstance(trades, dict) else None
    return list(rows) if isinstance(rows, list) else []


def trade_row_values(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """코어 행 → 표 칸(시각·매매·체결가·수량). 가격은 원값 자릿수 그대로, 수량은 소수 3자리까지
    (HL은 소수 수량). (순수 함수)"""
    price = row.get("price")
    qty = row.get("qty")
    return (
        str(row.get("time") or "-"),
        _SIDE_KO.get(str(row.get("side") or ""), "-"),
        f"{float(price):g}" if price is not None else "-",
        f"{float(qty):.3f}".rstrip("0").rstrip(".") if qty is not None else "-",
    )


def side_tag(row: dict[str, Any]) -> str:
    """행 색 태그 — 매수/매도/중립(순수 함수)."""
    side = str(row.get("side") or "")
    return side if side in ("buy", "sell") else "zero"


def rows_signature(rows: list[dict[str, Any]]) -> tuple[Any, ...]:
    """다시 그릴 필요가 있는지 판단할 서명 — (시각, 가격, 수량, 방향) 튜플들(순수 함수)."""
    return tuple((r.get("ts"), r.get("price"), r.get("qty"), r.get("side")) for r in rows)


def main() -> None:
    """창 실행 — 코어 /hl_trades 를 뒷단 스레드로 읽고, 화면은 그 결과만 그린다."""
    import tkinter as tk
    from tkinter import ttk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료(고아 방지)
    root = tk.Tk()
    from .core_client import log_screen_timing
    log_screen_timing(root, __name__)  # 시동 계측: 화면 시작·표시 시각(screen 로그)
    root.title("HL 체결")
    root.geometry("330x740")  # 폭은 좁게(사용자 2026-09-15)
    win_state.attach(root, "hl_trades", keep_size=True)
    T.apply_base(root)

    box: dict[str, Any] = {"data": None}  # run_state_feed가 채움(data·ok_ts·fails)
    pick: dict[str, str] = {"underlying": "sk_hynix"}

    # --- 상단: 종목 콤보 + 상태 ---
    top = tk.Frame(root)
    top.pack(fill="x", padx=6, pady=(6, 2))
    tk.Label(top, text="종목").pack(side="left")
    combo = ttk.Combobox(top, values=list(_NAMES.values()), state="readonly", width=12,
                         font=T.FONT_LABEL)
    combo.set(_NAMES[pick["underlying"]])
    combo.pack(side="left", padx=(4, 8))
    lbl_state = tk.Label(top, text="코어 확인 중 ...", anchor="w", fg=T.C_MUTED)
    lbl_state.pack(side="left", fill="x", expand=True)

    def on_pick(_e: object = None) -> None:
        pick["underlying"] = _CODES.get(combo.get(), pick["underlying"])

    combo.bind("<<ComboboxSelected>>", on_pick)

    # --- 표: 체결시각 | 매매 | 체결가 | 수량 (최신이 위, 30줄) ---
    cols = ("time", "side", "price", "qty")
    tree = ttk.Treeview(root, columns=cols, show="headings", height=30, selectmode="none")
    tree.heading("time", text="체결시각")
    tree.column("time", width=98, anchor="center", stretch=False)
    tree.heading("side", text="매매")
    tree.column("side", width=46, anchor="center", stretch=False)
    tree.heading("price", text="체결가")
    tree.column("price", width=82, anchor="e", stretch=False)
    tree.heading("qty", text="체결수량")
    tree.column("qty", width=78, anchor="e", stretch=True)
    tree.tag_configure("buy", foreground=T.C_BUY)
    tree.tag_configure("sell", foreground=T.C_SELL)
    tree.tag_configure("zero", foreground=T.C_ZERO)
    tree.pack(fill="both", expand=True, padx=6, pady=(2, 6))

    # --- 실시간 채널(trades) 공유 파일 0.1초 읽기, 없으면 HTTP 폴백 — 뒷단 스레드 ---
    # 공유 파일은 50ms마다 확인(다른 창 100ms) — 체결 표라 조금 더 촘촘히(사용자 09-15 "살짝 느림")
    threading.Thread(
        target=lambda: run_state_feed(box, log_tag="HL체결", channel="trades",
                                      fallback_path="/hl_trades", interval_s=0.05, poll_s=0.3),
        daemon=True).start()

    shown: dict[str, Any] = {"sig": None}

    def refresh() -> None:
        try:
            u = pick["underlying"]
            rows = rows_for(box.get("data"), u)
            sig = (u, rows_signature(rows))
            if sig != shown["sig"]:  # 바뀔 때만 다시 그림
                shown["sig"] = sig
                tree.delete(*tree.get_children())
                for r in rows:
                    tree.insert("", "end", values=trade_row_values(r), tags=(side_tag(r),))
            if not box_is_live(box, time.time()):
                lbl_state.config(text="코어 미접속 (메인에서 코어 시작)", fg=T.C_ERR)
            elif not rows:
                lbl_state.config(text="체결 대기 중 (HL WS 수신 전)", fg=T.C_MUTED)
            else:
                lbl_state.config(text=f"최근 {len(rows)}건 · {rows[0].get('time', '')}",
                                 fg=T.C_MUTED)
        except Exception:  # noqa: BLE001 - 갱신 1회 실패로 화면을 죽이지 않음
            pass
        finally:
            try:
                root.after(50, refresh)
            except tk.TclError:
                pass

    refresh()
    while True:
        try:
            root.mainloop()
            break
        except KeyboardInterrupt:
            try:
                root.winfo_exists()
            except tk.TclError:
                break


if __name__ == "__main__":
    main()
