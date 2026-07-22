"""메인 화면(관리) — 코어 생명주기·상태 감시 + 화면 실행 메뉴 (DESIGN §6.2, §12).

    main.bat   (일상 운영 진입점 — 이것 하나만 실행)

- 코어 ▸ 코어 시작(자식 프로세스) / 코어 안전종료(shutdown 명령 — 강제 킬 없음)
- 화면 ▸ 전략 화면 / 시세 모니터 (별도 프로세스 — 메인을 닫아도 계속 돈다)
- 본문: 코어 상태 2초 갱신. 전략 화면은 코어 생명주기에 관여할 수 없다(사고 방지).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .core_client import core_request

# 메인 화면 마지막 상태(코어 실행 여부·띄운 화면 목록) — gitignore
UI_STATE_PATH = Path(__file__).resolve().parent.parent / "ui_state.json"


def core_alive() -> bool:
    """코어 생존 확인 — /state 응답 여부."""
    return core_request("/state") is not None


def launch_module(module: str, *, console: bool = False) -> subprocess.Popen[bytes]:
    """파이썬 모듈을 별도 프로세스로 실행.

    화면은 콘솔 숨김(CREATE_NO_WINDOW — cmd 창 안 뜸), 코어만 새 콘솔(로그 확인용).
    """
    flags = 0
    if sys.platform == "win32":
        flags = (subprocess.CREATE_NEW_CONSOLE if console
                 else subprocess.CREATE_NO_WINDOW)
    return subprocess.Popen([sys.executable, "-m", module], creationflags=flags)


def main() -> None:
    """메인 창 실행."""
    import threading
    import time
    import tkinter as tk

    # 코어 생존 확인은 HTTP 왕복(최대 1초)이라 화면 스레드에서 하면 창 끌기·
    # 메뉴가 그 순간 얼어붙는다 → 뒷단 스레드가 확인하고 화면은 결과만 읽는다.
    alive_box = {"alive": False}
    launched: list[tuple[str, subprocess.Popen[bytes]]] = []

    def save_ui_state() -> None:
        """마지막 상태 저장 — 다음 실행 때 그대로 복원."""
        data = {"core": alive_box["alive"],
                "screens": [m for m, p in launched if p.poll() is None]}
        try:
            UI_STATE_PATH.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def poll_core() -> None:
        while True:
            alive_box["alive"] = core_alive()
            save_ui_state()
            time.sleep(2.0)

    threading.Thread(target=poll_core, daemon=True).start()

    def open_screen(module: str) -> None:
        launched.append((module, launch_module(module)))

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
        launch_module("kp_arb.core_server", console=True)
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
                         command=lambda: open_screen("kp_arb.strategy_panel"))
    m_screen.add_command(label="시세 모니터",
                         command=lambda: open_screen("kp_arb.monitor"))
    menubar.add_cascade(label="화면", menu=m_screen)
    m_core = tk.Menu(menubar, tearoff=0)
    m_core.add_command(label="코어 시작", command=start_core)
    m_core.add_command(label="코어 안전종료", command=stop_core)
    menubar.add_cascade(label="코어", menu=m_core)
    root.config(menu=menubar)

    def refresh() -> None:
        try:  # 네트워크 호출 없음 — 뒷단 스레드 결과만 표시 (버벅임 방지)
            if alive_box["alive"]:
                lbl_core.config(text="코어: 연결됨 (127.0.0.1:8787)", fg="dark green")
            else:
                lbl_core.config(text="코어: 미접속 — 메뉴 ▸ 코어 ▸ 코어 시작",
                                fg="#8b0000")
        finally:
            try:
                root.after(500, refresh)
            except tk.TclError:
                pass  # 창 닫힘

    # --- 마지막 상태 복원: 코어가 떠 있었으면 재시동, 화면들도 다시 열기 ---
    try:
        saved_raw = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved_raw = {}
    saved = saved_raw if isinstance(saved_raw, dict) else {}
    if saved.get("core") and not core_alive():
        launch_module("kp_arb.core_server", console=True)
        status.config(text="마지막 상태 복원 — 코어 시작 중 ...")
    screens = [m for m in saved.get("screens", [])
               if isinstance(m, str) and m.startswith("kp_arb.")]
    if screens:
        def reopen() -> None:
            for module in screens:
                open_screen(module)
        root.after(1500, reopen)  # 코어가 뜰 시간을 살짝 준 뒤

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
