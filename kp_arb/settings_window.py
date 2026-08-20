"""전체 공통설정 창 — 코어 클라이언트 (DESIGN-settings §3).

HL 일일 한도(USDC) + 알람(체결·에러·WS끊김) 사운드 설정. 설정은 코어(core_state.json)에
저장(settings_global). 화면은 입력·표시만 — 전송·폴링은 뒷단 스레드, 화면은 결과만 읽는다.
"""
from __future__ import annotations

import queue
from typing import Any

from . import sound, win_state
from . import ui_theme as T
from .core_client import core_request, watch_parent_exit

_ALARMS: tuple[tuple[str, str], ...] = (
    ("sound_fill", "주문 체결 시"),
    ("sound_error", "에러 발생 시"),
    ("sound_ws", "WS 끊김"),
)


def _fmt_amount(v: float) -> str:
    """금액을 3자리 콤마로 — 지수표현(5e+09) 방지. 정수면 소수점 없음."""
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,.2f}"


def main() -> None:  # noqa: PLR0915 - 화면 조립은 한 함수가 읽기 쉽다
    """공통설정 창 실행."""
    import threading
    import time
    import tkinter as tk
    from tkinter import filedialog

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료(고아 방지)
    root = tk.Tk()
    root.title("공통설정")
    root.resizable(False, False)
    win_state.attach(root, "settings")
    T.apply_base(root)

    jobs: queue.Queue[tuple[dict[str, Any], str]] = queue.Queue()
    results: queue.Queue[tuple[str, dict[str, Any] | None]] = queue.Queue()
    state_box: dict[str, Any] = {"data": None, "loaded": False}

    def sender() -> None:
        while True:
            payload, label = jobs.get()
            results.put((label, core_request("/command", payload, timeout=10.0)))

    def poller() -> None:
        while True:
            state_box["data"] = core_request("/state", timeout=2.0)
            time.sleep(1.0)

    threading.Thread(target=sender, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()

    def send(payload: dict[str, Any], label: str) -> None:
        jobs.put((payload, label))

    # ===== UI =====
    form = tk.Frame(root)
    form.pack(fill="x", padx=10, pady=10)
    status = tk.Label(root, text="-", anchor="w", relief="groove", fg=T.C_MUTED)
    status.pack(side="bottom", fill="x", padx=6, pady=(2, 6))

    def set_status(text: str, err: bool = False) -> None:
        status.config(text=text[:90], fg=T.C_ERR if err else T.C_MUTED)

    # HL 일일 한도
    tk.Label(form, text="HL 일일 한도(USDC)").grid(row=0, column=0, sticky="w", pady=2)
    e_limit = tk.Entry(form, width=18, justify="right", font=T.FONT_NUM)
    e_limit.grid(row=0, column=1, sticky="w", padx=6, pady=2)
    tk.Label(form, text="0 = 무제한", fg=T.C_MUTED).grid(
        row=0, column=2, columnspan=2, sticky="w")

    def _reformat_limit(*_ev: Any) -> None:
        # 타이핑하면 3자리마다 콤마 자동(소수부는 그대로 보존). 저장 시엔 콤마 제거해 전송.
        raw = e_limit.get().replace(",", "")
        int_part, _, dec_part = raw.partition(".")
        if int_part in ("", "-"):
            return
        try:
            n = int(int_part)
        except ValueError:
            return
        text = f"{n:,}" + (f".{dec_part}" if "." in raw else "")
        if text == e_limit.get():   # 변화 없으면(방향키 등) 커서 안 건드림
            return
        e_limit.delete(0, "end")
        e_limit.insert(0, text)
        e_limit.icursor("end")

    e_limit.bind("<KeyRelease>", _reformat_limit)

    # 알람 3줄 — [체크박스] 이벤트명  [wav 경로]  [찾아보기] [듣기]
    tk.Label(form, text="알람 (wav)").grid(row=1, column=0, sticky="w", pady=(10, 2))
    rows: dict[str, dict[str, Any]] = {}
    for i, (key, name) in enumerate(_ALARMS, start=2):
        var = tk.BooleanVar(value=False)
        tk.Checkbutton(form, text=name, variable=var, width=10, anchor="w").grid(
            row=i, column=0, sticky="w", pady=1)
        e_path = tk.Entry(form, width=36, font=T.FONT_BASE)
        e_path.grid(row=i, column=1, sticky="w", padx=6, pady=1)

        def _browse(entry: tk.Entry = e_path) -> None:
            path = filedialog.askopenfilename(
                title="wav 선택", filetypes=[("WAV 파일", "*.wav"), ("모든 파일", "*.*")])
            if path:
                entry.delete(0, "end")
                entry.insert(0, path)

        def _play(entry: tk.Entry = e_path) -> None:
            err = sound.play_wav(entry.get().strip())
            set_status(f"재생 실패: {err}" if err else "재생", err=err is not None)

        tk.Button(form, text="찾아보기", font=T.FONT_SMALL, command=_browse).grid(
            row=i, column=2, padx=2)
        tk.Button(form, text="듣기", font=T.FONT_SMALL, command=_play).grid(
            row=i, column=3, padx=2)
        rows[key] = {"var": var, "entry": e_path}

    btn = tk.Button(root, text="저장", width=12, font=T.FONT_STRONG)
    btn.pack(pady=(0, 8))

    def do_save() -> None:
        try:
            limit = float(e_limit.get().replace(",", "").strip() or "0")
        except ValueError:
            set_status("한도는 숫자로 입력하세요", err=True)
            return
        payload: dict[str, Any] = {"cmd": "settings_global", "hl_daily_limit_usdc": limit}
        for key, r in rows.items():
            payload[key] = {"enabled": bool(r["var"].get()),
                            "path": r["entry"].get().strip()}
        send(payload, "저장")

    btn.config(command=do_save)

    # ===== 루프 (네트워크 없음 — 폴링 결과만 읽음) =====
    def _reschedule(fn: Any, ms: int) -> None:
        try:
            root.after(ms, fn)
        except tk.TclError:
            pass

    def drain_results() -> None:
        try:
            while True:
                label, result = results.get_nowait()
                if result is None:
                    set_status(f"{label} 실패 — 코어 미접속", err=True)
                elif not result.get("ok"):
                    set_status(f"{label} 거부 — {'; '.join(result.get('errors', []))}",
                               err=True)
                else:
                    set_status(f"{label}됨")
        except queue.Empty:
            pass
        _reschedule(drain_results, 200)

    def refresh() -> None:
        data = state_box.get("data") or {}
        settings = data.get("settings")
        if isinstance(settings, dict) and not state_box["loaded"]:
            state_box["loaded"] = True  # 최초 1회만 채움(사용자 편집 덮어쓰기 방지)
            e_limit.delete(0, "end")
            e_limit.insert(0, _fmt_amount(float(settings.get("hl_daily_limit_usdc", 0) or 0)))
            for key, r in rows.items():
                snd = settings.get(key) or {}
                r["var"].set(bool(snd.get("enabled", False)))
                r["entry"].delete(0, "end")
                r["entry"].insert(0, str(snd.get("path", "")))
        _reschedule(refresh, 500)

    drain_results()
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
