"""메인 화면(관리) — 코어 생명주기·상태 감시 + 화면 실행 메뉴 (DESIGN §6.2, §12).

    main.bat   (일상 운영 진입점 — 이것 하나만 실행)

- 코어 ▸ 코어 시작(자식 프로세스) / 코어 안전종료(shutdown 명령 — 강제 킬 없음)
- 화면 ▸ 전략 화면 / 시세 모니터 (별도 프로세스 — 메인을 닫아도 계속 돈다)
- 본문: 코어 상태 2초 갱신. 전략 화면은 코어 생명주기에 관여할 수 없다(사고 방지).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from . import sound, win_state
from .core_client import core_request

# 메인 화면 마지막 상태(코어 실행 여부·띄운 화면 목록) — gitignore
# 배포판(exe)은 실행파일 옆, 개발은 프로젝트 루트 (frozen에서 _internal 안 방지)
_BASE_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
             else Path(__file__).resolve().parent.parent)
UI_STATE_PATH = _BASE_DIR / "ui_state.json"

_MUTEX_HANDLES: list[int] = []  # 단일 인스턴스 뮤텍스 핸들 유지(프로세스 수명 동안)


def _ensure_single_instance() -> bool:
    """메인이 이미 떠 있으면 False(두 번째 실행 차단). Windows 네임드 뮤텍스 — 프로세스가
    끝나면 OS가 자동 해제하므로 스테일 락 걱정이 없다. win32 외엔 항상 True(막지 않음)."""
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "meme-main-window")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        if handle:
            kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLES.append(handle)  # 닫지 않고 유지 → 종료 시 OS가 해제
    return True


def _warn_already_running() -> None:
    """이미 실행 중임을 알린다(win32 메시지박스)."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0, "Meme 메인이 이미 실행 중입니다.", "Meme", 0x40)


def core_alive() -> bool:
    """코어 생존 확인 — /state 응답 여부."""
    return core_request("/state") is not None


def _auto_running() -> bool:
    """실행 중(running) 세트가 하나라도 있는가 — 종료 확인창 판단용."""
    state = core_request("/state")
    if not isinstance(state, dict):
        return False
    for screen in (state.get("screens") or {}).values():
        if not isinstance(screen, dict):
            continue
        for block in ("entry_sets", "exit_sets"):
            for spread_set in screen.get(block) or []:
                if isinstance(spread_set, dict) and spread_set.get("running"):
                    return True
    return False


def _restart_step(
    st: dict[str, Any], alive: bool, *, after: int, cooldown: int, max_restarts: int
) -> str:
    """코어 감시 상태를 한 스텝 갱신하고 취할 행동을 돌려준다(순수 — I/O 없음, Phase 8-5).

    반환: ``"none"``(대기) · ``"restart"``(재기동) · ``"give_up"``(반복 실패로 중단).
    st 키(제자리 갱신): intentional(안전종료 여부)·down(연속 미접속)·cooldown(부팅 유예)·
    fails(연속 재기동 횟수)·gave_up(중단 여부). 살아있으면 카운터를 초기화한다.
    """
    if alive:
        st["down"] = 0
        st["fails"] = 0
        st["gave_up"] = False
        return "none"
    if st["cooldown"] > 0:  # 시작/재기동 직후 부팅 유예 — 미접속을 세지 않음
        st["cooldown"] -= 1
        return "none"
    if st["intentional"] or st["gave_up"]:  # 안전종료했거나 이미 포기 — 되살리지 않음
        return "none"
    st["down"] += 1
    if st["down"] < after:
        return "none"
    st["down"] = 0
    st["cooldown"] = cooldown
    st["fails"] += 1
    if st["fails"] > max_restarts:
        st["gave_up"] = True
        return "give_up"
    return "restart"


def launch_command(module: str, args: tuple[str, ...]) -> list[str]:
    """실행 명령 구성 — 개발(파이썬)과 배포판(exe, app.py 분기)을 모두 지원."""
    if not getattr(sys, "frozen", False):
        # 자식은 콘솔 있는 python.exe로 띄우고 창은 CREATE_NO_WINDOW로 숨긴다. 메인이
        # pythonw로 뜨면 sys.executable=pythonw라 자식 stdout이 None이 되어 불안정 →
        # python.exe로 교체(콘솔은 여전히 안 보임, stdout은 유효).
        exe = sys.executable.replace("pythonw.exe", "python.exe")
        return [exe, "-m", module, *args]
    exe_dir = Path(sys.executable).parent
    if module == "kp_arb.core_server":
        return [str(exe_dir / "meme-core.exe"), "core"]
    if module == "kp_arb.monitor":
        return [str(exe_dir / "meme.exe"), "monitor"]
    if module == "kp_arb.fx_monitor":
        return [str(exe_dir / "meme.exe"), "fx_monitor"]
    if module == "kp_arb.order_hl":
        return [str(exe_dir / "meme.exe"), "order_hl"]
    if module == "kp_arb.order_list":
        return [str(exe_dir / "meme.exe"), "order_list"]
    if module == "kp_arb.fx_auction_order":
        return [str(exe_dir / "meme.exe"), "fx_auction_order"]
    if module == "kp_arb.settings_window":
        return [str(exe_dir / "meme.exe"), "settings"]
    if module == "kp_arb.order_autot":
        return [str(exe_dir / "meme.exe"), "autoT"]
    return [str(exe_dir / "meme.exe")]


def launch_module(module: str, *args: str, console: bool = False,
                  watch_parent: bool | None = None,
                  slot: int | None = None) -> subprocess.Popen[bytes]:
    """모듈(또는 배포판 exe)을 별도 프로세스로 실행.

    콘솔 숨김(CREATE_NO_WINDOW — cmd 창 안 뜸)이 기본. 코어도 콘솔 없이 띄우고 로그는
    파일(logs/core_날짜.log)로만 남긴다. 자식 화면엔 메인 PID를 넘겨(KP_PARENT_PID)
    메인이 죽으면 스스로 닫히게 한다 — 단 코어는 독립 유지(watch_parent=False).
    """
    flags = 0
    if sys.platform == "win32":
        flags = (subprocess.CREATE_NEW_CONSOLE if console
                 else subprocess.CREATE_NO_WINDOW)
    if watch_parent is None:
        watch_parent = not console  # 콘솔 없는 화면은 메인 생사 감시(고아 방지)
    env = ({**os.environ, "KP_PARENT_PID": str(os.getpid())}
           if watch_parent else None)
    if slot is not None:  # 같은 종류 창 인스턴스 구분 — win_state 키에 붙는다
        env = {**(env if env is not None else os.environ), "KP_WIN_SLOT": str(slot)}
    return subprocess.Popen(launch_command(module, args), creationflags=flags, env=env)


def main() -> None:
    """메인 창 실행."""
    if not _ensure_single_instance():  # 중복 실행 차단 — 이미 떠 있으면 알림 후 종료
        _warn_already_running()
        return
    import threading
    import time
    import tkinter as tk

    # 마지막 상태는 **스레드 시작 전에** 읽는다 — 감시 스레드가 ui_state.json을
    # 빈 화면 목록으로 먼저 덮어써 복원이 안 되던 문제 방지.
    try:
        saved_raw = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved_raw = {}
    saved = saved_raw if isinstance(saved_raw, dict) else {}

    # 코어 생존 확인은 HTTP 왕복(최대 1초)이라 화면 스레드에서 하면 창 끌기·
    # 메뉴가 그 순간 얼어붙는다 → 뒷단 스레드가 확인하고 화면은 결과만 읽는다.
    alive_box: dict[str, Any] = {"alive": False, "ws": []}
    closing = {"flag": False}
    launched: list[tuple[str, int, subprocess.Popen[bytes]]] = []  # (token, slot, proc)

    # 코어 자동 재시작(Phase 8-5) — 연속 미접속이 임계 이상이면 재기동. cooldown은 시작
    # 직후 부팅 유예(초기값으로 시동 자체를 크래시로 오인하지 않게).
    RESTART_AFTER = 3       # 연속 미접속 3회(~6초)면 재기동
    RESTART_COOLDOWN = 6    # 시작/재기동 후 6회(~12초)는 부팅 유예
    MAX_RESTARTS = 5        # 이만큼 연속 실패하면 자동 재기동 중단(수동 점검)
    restart: dict[str, Any] = {"intentional": False, "down": 0,
                               "cooldown": RESTART_COOLDOWN, "fails": 0, "gave_up": False}

    def _alert(text: str, level: str = "warn") -> None:
        try:
            from . import alert
            alert.notify(text, level)
        except Exception:  # noqa: BLE001 - 알림 실패가 감시를 멈추지 않게
            pass

    def maybe_restart_core(alive: bool) -> None:
        action = _restart_step(restart, alive, after=RESTART_AFTER,
                               cooldown=RESTART_COOLDOWN, max_restarts=MAX_RESTARTS)
        if action == "restart":
            launch_module("kp_arb.core_server", console=False, watch_parent=False)
            _alert("코어 미접속 감지 — 자동 재기동", "error")
        elif action == "give_up":
            _alert("코어 자동 재기동 반복 실패 — 중단. 수동 점검 필요", "error")

    def save_ui_state() -> None:
        """마지막 상태 저장 — 다음 실행 때 그대로 복원."""
        data = {"core": alive_box["alive"],
                "screens": [tok for tok, _slot, p in launched if p.poll() is None]}
        try:
            UI_STATE_PATH.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    # 알람 사운드 — /state의 fill_seq·error_seq 증가 + WS 끊김 전이를 감지해 재생.
    # 시동 시점 값은 기준으로만 잡고 재생 안 함(첫 관측은 None→값).
    sound_box: dict[str, Any] = {"fill": None, "error": None, "ws": {}}

    def _play_alarm(settings: dict[str, Any], key: str) -> None:
        snd = settings.get(key) or {}
        if snd.get("enabled") and snd.get("path"):
            sound.play_wav(str(snd["path"]))

    def check_sounds(data: dict[str, Any] | None) -> None:
        if not data:
            return
        settings = data.get("settings") or {}
        for key, seq_field in (("sound_fill", "fill"), ("sound_error", "error")):
            seq = data.get(f"{seq_field}_seq")
            if isinstance(seq, int):
                prev = sound_box[seq_field]
                if prev is not None and seq > prev:
                    _play_alarm(settings, key)
                sound_box[seq_field] = seq
        for row in (data.get("ws") or []):  # WS 연결→끊김 전이마다 재생
            name = str(row.get("name"))
            conn = bool(row.get("connected"))
            if sound_box["ws"].get(name) is True and not conn:
                _play_alarm(settings, "sound_ws")
            sound_box["ws"][name] = conn

    def poll_core() -> None:
        while True:
            data = core_request("/state")  # 코어 생존 + WS 세션 현황 한 번에
            alive = data is not None
            alive_box["alive"] = alive
            alive_box["ws"] = (data or {}).get("ws") or []
            check_sounds(data)  # 알람(체결·에러·WS끊김)
            if not closing["flag"]:  # 종료 중엔 재기동·저장 안 함
                maybe_restart_core(alive)
                save_ui_state()
            time.sleep(2.0)

    threading.Thread(target=poll_core, daemon=True).start()

    def _next_slot(module: str) -> int:
        # 같은 module의 살아있는 인스턴스가 안 쓰는 가장 작은 슬롯 — 창별 위치 분리.
        used = {slot for tok, slot, p in launched
                if tok.split(" ", 1)[0] == module and p.poll() is None}
        slot = 0
        while slot in used:
            slot += 1
        return slot

    def open_screen(module: str, *args: str) -> None:
        token = " ".join([module, *args])  # ui_state 저장/복원용 식별자
        slot = _next_slot(module)  # 인스턴스별 슬롯 → win_state 키 분리(각 창 위치 따로)
        launched.append((token, slot, launch_module(module, *args, slot=slot)))

    root = tk.Tk()
    root.title("Meme")
    root.resizable(False, False)
    root.option_add("*Font", ("Malgun Gothic", 9))
    win_state.attach(root, "main")  # 메인창 위치 저장·복원(슬롯 없음 — 단일 인스턴스)

    lbl_core = tk.Label(root, text="코어: 확인 중 ...", anchor="w", width=42)
    lbl_core.pack(fill="x", padx=8, pady=(8, 2))
    status = tk.Label(root, text="-", anchor="w", relief="groove")
    status.pack(fill="x", padx=8, pady=(2, 8))

    # WS 세션 현황(Phase 8-3) — 코어 /state의 ws를 2초마다 읽어 표시.
    from tkinter import ttk

    ws_frame = tk.LabelFrame(root, text="WS 세션")
    ws_frame.pack(fill="x", padx=8, pady=(0, 8))
    ws_tree = ttk.Treeview(ws_frame, columns=("no", "venue", "name", "state", "rx"),
                           show="headings", height=3, selectmode="none")
    for col, title, wid, anc in (("no", "No", 32, "center"), ("venue", "거래소", 48, "center"),
                                 ("name", "이름", 96, "w"), ("state", "상태", 54, "center"),
                                 ("rx", "수신", 84, "e")):
        ws_tree.heading(col, text=title)
        ws_tree.column(col, width=wid, anchor=cast(Any, anc), stretch=False)
    ws_tree.tag_configure("up", foreground="dark green")
    ws_tree.tag_configure("down", foreground="#8b0000")
    ws_tree.pack(fill="x", padx=4, pady=4)
    ws_box: dict[str, Any] = {"sig": None}

    def render_ws() -> None:
        rows = alive_box.get("ws") or []
        sig = tuple((r.get("name"), r.get("connected"), r.get("rx_count"),
                     r.get("disconnects")) for r in rows)
        if sig == ws_box["sig"]:  # 변화 없으면 다시 그리지 않음(깜빡임 방지)
            return
        ws_box["sig"] = sig
        ws_tree.delete(*ws_tree.get_children())
        for i, r in enumerate(rows, 1):
            up = bool(r.get("connected"))
            ws_tree.insert("", "end", tags=("up" if up else "down",), values=(
                i, r.get("venue"), r.get("name"), "연결" if up else "끊김",
                f"{r.get('rx_count', 0):,}"))

    def start_core() -> None:
        if core_alive():
            status.config(text="코어가 이미 떠 있음")
            return
        restart["intentional"] = False  # 사용자가 다시 켬 — 자동 재기동 재개
        restart["gave_up"] = False
        restart["cooldown"] = RESTART_COOLDOWN  # 부팅 유예
        launch_module("kp_arb.core_server", console=False, watch_parent=False)
        status.config(text="코어 시작 중 ...")

    def stop_core() -> None:
        restart["intentional"] = True  # 안전종료 — 자동 재기동하지 않음
        result = core_request("/command", {"cmd": "shutdown"})
        if result is None:
            status.config(text="코어 미접속 — 종료할 대상 없음")
        elif result.get("ok"):
            status.config(text="안전종료 요청됨 — 자동 정지 후 종료")
        else:
            status.config(text="종료 거부 — " + "; ".join(result.get("errors", [])))

    menubar = tk.Menu(root)
    m_screen = tk.Menu(menubar, tearoff=0)
    m_screen.add_command(label="바로쏴 (자동T)",
                         command=lambda: open_screen("kp_arb.order_autot"))
    m_screen.add_command(label="시세 모니터",
                         command=lambda: open_screen("kp_arb.monitor"))
    m_screen.add_command(label="FX 노출 감시",
                         command=lambda: open_screen("kp_arb.fx_monitor"))
    m_screen.add_command(label="HL 일반주문 (수동)",
                         command=lambda: open_screen("kp_arb.order_hl"))
    m_screen.add_command(label="주문 리스트 (미체결·취소·정정)",
                         command=lambda: open_screen("kp_arb.order_list"))
    m_screen.add_command(label="원달러선물 동시호가 주문",
                         command=lambda: open_screen("kp_arb.fx_auction_order"))
    m_screen.add_separator()
    m_screen.add_command(label="공통설정",
                         command=lambda: open_screen("kp_arb.settings_window"))
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
            render_ws()
        finally:
            try:
                root.after(500, refresh)
            except tk.TclError:
                pass  # 창 닫힘

    # --- 코어는 메인과 함께 시작 (사용자 확정 2026-07-24) ---
    if not core_alive():
        launch_module("kp_arb.core_server", console=False, watch_parent=False)
        status.config(text="코어 시작 중 ...")
    screens = [m for m in saved.get("screens", [])
               if isinstance(m, str) and m.startswith("kp_arb.")]
    if screens:
        # 저장된 화면은 **코어 연결된 뒤** 연다 — 코어 미접속 상태에서 화면 조작을 막기 위함
        # (사용자 확정). 코어가 30초 내 안 뜨면 자동 복원 포기(메뉴로 수동 오픈 유도).
        def reopen_when_ready(waited_ms: int = 0) -> None:
            if alive_box["alive"]:
                for token in screens:
                    module, *args = token.split()
                    open_screen(module, *args)
                return
            if waited_ms >= 30_000:
                status.config(text="코어 미접속 — 저장된 화면 자동 복원 안 함(메뉴에서 여세요)")
                return
            root.after(500, lambda: reopen_when_ready(waited_ms + 500))

        root.after(500, lambda: reopen_when_ready(500))

    def on_close() -> None:
        """메인 종료 = 화면들 + 코어까지 함께 종료 (사용자 확정 2026-07-24).

        단, 자동 매매(실행 중 세트)가 있으면 확인창 — 실수로 매매를 끊지 않게.
        """
        from tkinter import messagebox

        if _auto_running() and not messagebox.askokcancel(
                "종료 확인", "자동 매매가 실행 중입니다.\n코어까지 종료하시겠습니까?"):
            return
        save_ui_state()  # 닫기 직전 화면 목록 저장 — 다음 실행 때 다시 열림
        closing["flag"] = True
        core_request("/command", {"cmd": "shutdown"})  # 코어 안전종료(정지·취소 후)
        for _tok, _slot, proc in launched:
            if proc.poll() is None:
                proc.terminate()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

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
