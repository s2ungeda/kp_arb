"""상태줄 콤보 — 읽기 전용 콤보박스로 최신 글을 보여 주고, 펼치면 지난 글(최대 50건).

NXT 개장 화면에서 먼저 썼고(사용자 2026-10-23 "상태바를 콤보박스로 — 지난 결과도 볼 수 있게,
50건까지만") 체결쏴 화면도 같은 모양으로(사용자 2026-10-23 "체결쏴 주식선물 상태바도 콤보박스로").
순수 부분은 push_history·style_for, 위젯은 StatusCombo.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

HISTORY_MAX = 50  # 상태줄 콤보 이력(사용자 2026-10-23 "100건은 많고 50건까지만")

# 글자색 → ttk 스타일 이름(체결쏴 LS 처리 멈춤 알림: 빨강 / 재개: 초록). 모르는 색은 기본.
STATUS_STYLES: dict[str, str] = {"black": "TCombobox", "red": "Stall.TCombobox",
                                 "dark green": "Resume.TCombobox"}


_STAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2} ")


def strip_stamp(line: str) -> str:
    """이력 한 줄에서 앞의 시각("HH:MM:SS ")을 뗀 글. 순수."""
    return _STAMP_RE.sub("", line, count=1)


def push_history(history: list[str], text: str, limit: int = HISTORY_MAX,
                 stamp: str = "") -> bool:
    """상태줄 이력에 text를 맨 앞에 넣는다 — 맨 앞 글(시각 뺀)과 같으면 안 넣고 False. stamp가
    있으면 "HH:MM:SS 글"로 남긴다(사용자 2026-10-23 "상태바에 남기는 로그에 시간 추가"). limit
    넘으면 뒤를 버린다. 순수."""
    if history and strip_stamp(history[0]) == text:
        return False
    history.insert(0, f"{stamp} {text}" if stamp else text)
    del history[limit:]
    return True


# LS 처리 지연 띠(사용자 2026-10-26 "상태바는 못 보고 지나간다 — 빨간 띠, 글은 'LS 처리 지연'"):
# 창 맨 위에 지연 동안만 보이는 빨간 바탕 흰 글 한 줄. 체결쏴·NXT 개장·동시호가 주문·메인 창 공통.
STALL_BANNER_TEXT = "LS 처리 지연 — 자동 주문 안 나감(풀리면 사라짐)"
STALL_BANNER_BG = "#c00000"


def ls_stalled(rows: Sequence[Mapping[str, Any]]) -> bool:
    """/state 채널 현황에서 LS 채널이 '처리 지연'(접속은 살아 있는데 COM 스레드가 안 돎)인가.
    끊김(connected False)은 다른 길(메인 끊김 알림)이 다룬다. 순수."""
    return any(str(r.get("venue")) == "LS" and bool(r.get("connected")) and bool(r.get("stalled"))
               for r in rows)


class StallBanner:
    """지연 띠 위젯 — ``set(True)``면 ``show(widget)``(화면이 정한 grid/pack)로 붙이고,
    ``set(False)``면 뗀다. 같은 상태면 아무것도 안 해 깜빡이지 않는다."""

    def __init__(self, parent: Any, font: Any, show: Callable[[Any], None]) -> None:
        import tkinter as tk

        self.widget = tk.Label(parent, text=STALL_BANNER_TEXT, bg=STALL_BANNER_BG, fg="white",
                               font=font, anchor="w", padx=8, pady=3)
        self._show = show
        self.on = False

    def set(self, stalled: bool) -> None:
        if stalled == self.on:
            return
        self.on = stalled
        if stalled:
            self._show(self.widget)
        elif self.widget.winfo_manager() == "grid":
            self.widget.grid_remove()
        else:
            self.widget.pack_forget()


def log_tk_errors(root: Any, log: Any) -> None:
    """tk 콜백(버튼·after) 안의 예외를 화면 로그에 남긴다 — 메인이 띄운 화면은 콘솔이 없어 tk가
    stderr로 내는 추적이 사라지고, 버튼이 '안 눌리는' 것처럼만 보인다(2026-10-26 동시호가 주문
    "예약 버튼 상태가 안 변함" 조사). 원인 파악용 — 예외를 삼키지 않고 기록만 한다."""
    import traceback

    def report(exc_type: Any, exc: Any, tb: Any) -> None:
        log.error("[화면 오류] %s", "".join(traceback.format_exception(exc_type, exc, tb)))

    root.report_callback_exception = report


def style_for(color: str) -> str:
    """글자색 이름 → 콤보 스타일 이름. 순수."""
    return STATUS_STYLES.get(color, STATUS_STYLES["black"])


class StatusCombo:
    """상태줄 콤보 하나 + 이력. ``show(text)``로 최신 글(이력 맨 앞과 같으면 안 쌓음),
    ``color(fg)``로 글자색. ``widget``을 grid/pack 한다. width=1 + 가로 채움 — 긴 글이 창을 넓히지
    않게; 펼친 목록은 글 길이에 맞춰 넓힌다(postcommand)."""

    def __init__(self, parent: Any, font: Any) -> None:
        import tkinter as tk
        from tkinter import ttk

        self._tk = tk
        self.history: list[str] = []
        self._color = "black"
        style = ttk.Style()
        for color, name in STATUS_STYLES.items():
            if name != "TCombobox":
                style.configure(name, foreground=color)
                style.map(name, foreground=[("readonly", color)],
                          fieldforeground=[("readonly", color)])
        self.widget = ttk.Combobox(parent, values=[], state="readonly", width=1, font=font)
        self.widget.config(postcommand=self._widen)

    def _widen(self) -> None:
        try:  # 펼침 목록(listbox) 폭을 글 길이에 맞춰 — ttk 내부 경로라 실패해도 무해
            popdown = self.widget.tk.call("ttk::combobox::PopdownWindow", self.widget)
            self.widget.tk.call(f"{popdown}.f.l", "configure", "-width",
                                min(160, max((len(t) for t in self.history), default=20) + 4))
        except self._tk.TclError:
            pass

    def show(self, text: str) -> None:
        """최신 글로(앞에 지금 시각) — 이력 맨 앞과 같은 글이면 그대로(매 갱신 쌓이지 않게)."""
        if push_history(self.history, text, stamp=time.strftime("%H:%M:%S")):
            self.widget.config(values=list(self.history))
        if self.widget.get() != self.history[0]:
            self.widget.set(self.history[0])

    def text(self) -> str:
        """지금 보이는 글(시각 뺀)."""
        return strip_stamp(str(self.widget.get()))

    def color(self, fg: str) -> None:
        """글자색(black·red·dark green) — 같은 색이면 아무것도 안 한다."""
        if fg != self._color:
            self._color = fg
            self.widget.config(style=style_for(fg))
