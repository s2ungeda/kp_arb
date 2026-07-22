"""메인 화면(관리) — 코어 생명주기·상태 감시 + 화면 실행 메뉴 (DESIGN §6.2, §12).

    main.bat   (일상 운영 진입점 — 이것 하나만 실행)

- 코어 ▸ 코어 시작(자식 프로세스) / 코어 안전종료(shutdown 명령 — 강제 킬 없음)
- 화면 ▸ 전략 화면 / 시세 모니터 (별도 프로세스 — 메인을 닫아도 계속 돈다)
- 본문: 코어 상태 2초 갱신. 전략 화면은 코어 생명주기에 관여할 수 없다(사고 방지).
"""
from __future__ import annotations

import subprocess
import sys

from .core_client import core_request


def core_alive() -> bool:
    """코어 생존 확인 — /state 응답 여부."""
    return core_request("/state") is not None


def launch_module(module: str) -> None:
    """파이썬 모듈을 별도 프로세스로 실행 (윈도우는 새 콘솔 — 로그 확인용)."""
    flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    subprocess.Popen([sys.executable, "-m", module], creationflags=flags)


def main() -> None:
    """메인 창 실행."""
    import tkinter as tk

    root = tk.Tk()
    root.title("kp-arb 메인")
    root.resizable(False, False)
    root.option_add("*Font", ("Malgun Gothic", 9))

    lbl_core = tk.Label(root, text="코어: 확인 중 ...", anchor="w", width=42)
    lbl_core.pack(fill="x", padx=8, pady=(8, 2))
    status = tk.Label(root, text="-", anchor="w", relief="groove")
    status.pack(fill="x", padx=8, pady=(2, 8))

    def start_core() -> None:
        if core_alive():
            status.config(text="코어가 이미 떠 있음")
            return
        launch_module("kp_arb.core_server")
        status.config(text="코어 시작 중 ...")

    def stop_core() -> None:
        result = core_request("/command", {"cmd": "shutdown"})
        if result is None:
            status.config(text="코어 미접속 — 종료할 대상 없음")
        elif result.get("ok"):
            status.config(text="안전종료 요청됨 — 자동 정지 후 종료")
        else:
            status.config(text="종료 거부 — " + "; ".join(result.get("errors", [])))

    menubar = tk.Menu(root)
    m_screen = tk.Menu(menubar, tearoff=0)
    m_screen.add_command(label="전략 화면",
                         command=lambda: launch_module("kp_arb.strategy_panel"))
    m_screen.add_command(label="시세 모니터",
                         command=lambda: launch_module("kp_arb.monitor"))
    menubar.add_cascade(label="화면", menu=m_screen)
    m_core = tk.Menu(menubar, tearoff=0)
    m_core.add_command(label="코어 시작", command=start_core)
    m_core.add_command(label="코어 안전종료", command=stop_core)
    menubar.add_cascade(label="코어", menu=m_core)
    root.config(menu=menubar)

    def refresh() -> None:
        try:
            if core_alive():
                lbl_core.config(text="코어: 연결됨 (127.0.0.1:8787)", fg="dark green")
            else:
                lbl_core.config(text="코어: 미접속 — 메뉴 ▸ 코어 ▸ 코어 시작",
                                fg="#8b0000")
        finally:
            try:
                root.after(2000, refresh)
            except tk.TclError:
                pass  # 창 닫힘

    refresh()
    # 콘솔로 Ctrl-C 신호가 흘러들어도 화면을 죽이지 않는다 (monitor와 동일)
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
