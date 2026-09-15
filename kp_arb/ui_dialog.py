"""화면 공통 팝업 — **항상 그 화면(부모 창) 중앙**에 띄운다 (사용자 확정 2026-09-04, DESIGN-ui §7).

tk의 messagebox는 위치를 우리가 정할 수 없어(OS가 놓는 자리) 창이 구석에 있으면 팝업이 엉뚱한
곳에 뜬다. 그래서 확인/예·아니오 팝업을 Toplevel로 직접 만들어 부모 창 중앙에 놓는다.
모달(grab) + 닫힐 때까지 대기. 부모가 아직 안 그려졌으면 모니터 중앙으로 대신한다.
"""
from __future__ import annotations

from functools import partial
from typing import Any


def centered_geometry(win_w: int, win_h: int,
                      px: int, py: int, pw: int, ph: int,
                      bounds: tuple[int, int, int, int] | None = None) -> str:
    """부모 창(px,py,pw,ph) 중앙에 놓는 tk geometry 위치부('+X+Y'). (순수 함수)

    bounds=(left, top, right, bottom): 부모가 있는 **모니터 작업 영역**. 주면 팝업이 그 안에
    다 들어오게 밀어 넣는다(음수 좌표 허용 — 왼쪽·위쪽 모니터). 없으면 옛 방식(음수는 0으로).
    실측 2026-09-09(사용자): 멀티모니터에서 음수를 0으로 밀어 팝업이 보이는 범위 밖에 떠
    모달 잠금으로 창 전체가 안 눌렸다.
    """
    x = px + (pw - win_w) // 2
    y = py + (ph - win_h) // 2
    if bounds is None:
        return f"+{max(x, 0)}+{max(y, 0)}"
    left, top, right, bottom = bounds
    x = max(min(x, right - win_w), left)
    y = max(min(y, bottom - win_h), top)
    return f"+{x}+{y}"


def monitor_work_area(widget: Any) -> tuple[int, int, int, int] | None:
    """위젯이 놓인 모니터의 작업 영역(left, top, right, bottom, 물리 픽셀 = tk 좌표) — Windows만.

    tk는 winfo_screenwidth()로 주 모니터 크기만 주므로, 다른 모니터에 있는 창의 팝업 위치는
    Win32 MonitorFromWindow로 그 모니터를 찾아 계산한다. 실패하면 None(호출자가 옛 방식)."""
    import sys

    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(widget.winfo_id(), 2)  # GA_ROOT — 최상위 창 핸들
        mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return None
        r = info.rcWork
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:  # noqa: BLE001 - 위치 보정은 편의 기능, 실패해도 팝업은 뜬다
        return None


def center_on_parent(win: Any, parent: Any) -> None:
    """Toplevel을 부모 창 중앙으로 옮긴다(크기 확정 후 호출). 부모가 안 보이면 모니터 중앙.
    부모가 있는 모니터 안에 다 들어오게 맞춘다(멀티모니터, 2026-09-09)."""
    win.update_idletasks()
    # 아직 화면에 안 그려진 창은 winfo_width()가 1이라 요청 크기(req)로 계산한다(실측 2026-09-04).
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    bounds = None
    if parent is not None and parent.winfo_viewable() and parent.winfo_width() > 1:
        px, py, pw, ph = (parent.winfo_rootx(), parent.winfo_rooty(),
                          parent.winfo_width(), parent.winfo_height())
        bounds = monitor_work_area(parent)
    else:
        px, py, pw, ph = 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(centered_geometry(w, h, px, py, pw, ph, bounds))


def _dialog(parent: Any, title: str, message: str,
            buttons: list[tuple[str, bool]]) -> bool:
    import tkinter as tk

    win = tk.Toplevel(parent)
    win.title(title)
    win.resizable(False, False)
    win.transient(parent)
    tk.Label(win, text=message, justify="left", padx=20, pady=16, wraplength=440).pack()
    result = {"v": False}

    def pick(value: bool) -> None:
        result["v"] = value
        win.destroy()

    row = tk.Frame(win)
    row.pack(pady=(0, 12))
    for i, (label, value) in enumerate(buttons):
        btn = tk.Button(row, text=label, width=10, command=partial(pick, value))
        btn.pack(side="left", padx=6)
        if i == 0:
            btn.focus_set()
    win.bind("<Return>", lambda _e: pick(buttons[0][1]))
    win.bind("<Escape>", lambda _e: pick(False))
    win.protocol("WM_DELETE_WINDOW", lambda: pick(False))
    # -topmost를 geometry 뒤에 걸면 Windows가 창을 (0,0)으로 되돌린다(실측 2026-09-04) → 먼저 건다.
    win.attributes("-topmost", True)  # 다른 창 뒤에 숨지 않게
    center_on_parent(win, parent)
    win.grab_set()
    win.focus_force()  # Enter/Esc가 팝업으로 가게(포커스 없으면 키가 다른 창으로 감)
    win.wait_window()
    return result["v"]


def ask_yes_no(parent: Any, title: str, message: str) -> bool:
    """예/아니오 — 예면 True. Enter=예, Esc/닫기=아니오."""
    return _dialog(parent, title, message, [("예", True), ("아니오", False)])


def show_message(parent: Any, title: str, message: str) -> None:
    """확인 버튼 하나짜리 알림."""
    _dialog(parent, title, message, [("확인", True)])


def hint_position(anchor_x: int, anchor_y: int, anchor_h: int, pop_w: int, pop_h: int,
                  bounds: tuple[int, int, int, int] | None) -> tuple[int, int]:
    """상태줄 위에 띄우는 전문 팝업의 위치(순수) — 기본은 상태줄 바로 위, 위에 자리가 없으면
    아래. 모니터 작업 영역(bounds=(x, y, w, h)) 안에 좌우도 맞춘다."""
    x, y = anchor_x, anchor_y - pop_h - 4
    if bounds is not None:
        bx, by, bw, bh = bounds
        if y < by:
            y = anchor_y + anchor_h + 4
        x = max(bx, min(x, bx + bw - pop_w))
        y = max(by, min(y, by + bh - pop_h))
    elif y < 0:
        y = anchor_y + anchor_h + 4
    return x, y


def attach_full_text_popup(label: Any, root: Any) -> None:
    """상태줄(한 줄로 잘리는 라벨)을 **더블클릭**하면 전문을 힌트 창으로 보여 준다(사용자
    2026-09-15: 상태줄이 좁아져 다 못 읽음). 창 폭에 맞춰 줄바꿈, 클릭·Esc·포커스 이탈로 닫힘.
    여러 번 눌러도 하나."""
    import tkinter as tk

    box: dict[str, Any] = {"win": None}

    def close(_e: object = None) -> None:
        win = box["win"]
        box["win"] = None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def show(_e: object = None) -> None:
        close()
        text = str(label.cget("text") or "")
        if not text.strip():
            return
        win = tk.Toplevel(root)
        win.overrideredirect(True)  # 제목줄 없는 힌트 창
        win.attributes("-topmost", True)
        wrap = max(240, root.winfo_width() - 40)
        tk.Label(win, text=text, justify="left", anchor="w", wraplength=wrap, bg="#ffffe0",
                 relief="solid", bd=1, padx=8, pady=6).pack()
        win.update_idletasks()
        x, y = hint_position(label.winfo_rootx(), label.winfo_rooty(), label.winfo_height(),
                             win.winfo_reqwidth(), win.winfo_reqheight(), monitor_work_area(label))
        win.geometry(f"+{x}+{y}")
        win.bind("<Button-1>", close)
        win.bind("<Escape>", close)
        win.bind("<FocusOut>", close)
        win.focus_force()
        box["win"] = win

    label.bind("<Double-Button-1>", show)
    label.bind("<Destroy>", close)
    label.show_full_text = show  # 프로그램·미리보기 캡처에서 직접 부를 수 있게(더블클릭 합성 불가)
