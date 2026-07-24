"""FX 노출 감시 화면 — 코어의 SignalLink 상태 표시·제어 (델파이 FChatMonitor 대응).

    python -m kp_arb.fx_monitor   (운영은 main.bat 메뉴에서)

코어가 외부 #2로 보내는 노출(total_coin=HL 명목, fx)을 감시한다:
- 발견된 피어(수신자) 목록, 마지막 송신값, 송신 로그
- 자동 송신 일시정지/재개, 수동 송신, 주기 변경
전송 자체는 코어가 한다(화면은 명령·표시만). 폴링은 뒷단 스레드.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any

from .core_client import core_request, watch_parent_exit


def main() -> None:
    """감시 창 실행 — 코어 클라이언트."""
    import tkinter as tk

    watch_parent_exit()  # 메인이 죽으면 이 창도 종료 (고아 방지)
    root = tk.Tk()
    root.title("kp-arb FX 노출 감시 [v2]")  # [v2]=뒷단 스레드 수정본 (옛 창 구분용)
    root.geometry("560x460")
    root.option_add("*Font", ("Malgun Gothic", 9))

    # 명령 전송은 뒷단 스레드로 — 화면 스레드에서 네트워크 하면 창이 언다(CLAUDE.md)
    jobs: queue.Queue[tuple[dict[str, Any], str]] = queue.Queue()
    results: queue.Queue[tuple[str, dict[str, Any] | None]] = queue.Queue()

    def sender() -> None:
        while True:
            payload, label = jobs.get()
            results.put((label, core_request("/command", payload)))

    threading.Thread(target=sender, daemon=True).start()

    def send(payload: dict[str, Any], label: str) -> None:
        jobs.put((payload, label))  # 큐에만 넣고 즉시 반환

    def drain_results() -> None:
        try:
            while True:
                label, result = results.get_nowait()
                if result is None:
                    set_status(f"{label} 실패 — 코어 미접속")
                elif not result.get("ok"):
                    set_status(f"{label} 거부 — {'; '.join(result.get('errors', []))}")
                else:
                    set_status(f"{label} 완료")
        except queue.Empty:
            pass
        try:
            root.after(200, drain_results)
        except tk.TclError:
            pass

    # --- 상단: 상태 + 제어 ---
    top = tk.Frame(root)
    top.pack(fill="x", padx=6, pady=(6, 2))
    lbl_state = tk.Label(top, text="상태: 확인 중 ...", anchor="w")
    lbl_state.pack(side="left")

    ctrl = tk.Frame(root)
    ctrl.pack(fill="x", padx=6, pady=2)
    btn_pause = tk.Button(ctrl, text="일시정지",
                          command=lambda: send({"cmd": "fx_pause"}, "일시정지"))
    btn_pause.pack(side="left")
    tk.Button(ctrl, text="재개",
              command=lambda: send({"cmd": "fx_resume"}, "재개")).pack(
        side="left", padx=(4, 12))
    tk.Button(ctrl, text="지금 송신",
              command=lambda: send({"cmd": "fx_send_now"}, "수동 송신")).pack(side="left")
    tk.Label(ctrl, text="  주기(초)").pack(side="left")
    ent_interval = tk.Entry(ctrl, width=5, justify="right")
    ent_interval.pack(side="left")
    tk.Button(ctrl, text="적용",
              command=lambda: send(
                  {"cmd": "fx_interval", "seconds": _to_float(ent_interval.get(), 2.0)},
                  "주기 변경")).pack(side="left", padx=(2, 0))

    # --- 마지막 송신값 ---
    last = tk.Frame(root)
    last.pack(fill="x", padx=6, pady=2)
    lbl_last = tk.Label(last, text="마지막 송신: -", anchor="w", fg="dark green")
    lbl_last.pack(side="left")

    # --- 피어 목록 + 로그 ---
    mid = tk.Frame(root)
    mid.pack(fill="both", expand=True, padx=6, pady=2)
    left = tk.Frame(mid)
    left.pack(side="left", fill="y")
    tk.Label(left, text="수신자(피어)").pack(anchor="w")
    lst_peers = tk.Listbox(left, width=28, height=8)
    lst_peers.pack(fill="y", expand=True)
    right = tk.Frame(mid)
    right.pack(side="left", fill="both", expand=True, padx=(6, 0))
    log_head = tk.Frame(right)
    log_head.pack(fill="x")
    tk.Label(log_head, text="송신 로그").pack(side="left")
    tk.Button(log_head, text="로그 지우기",
              command=lambda: send({"cmd": "fx_clear_log"}, "로그 지우기")).pack(
        side="right")
    txt_log = tk.Text(right, height=12, width=40, state="disabled")
    txt_log.pack(fill="both", expand=True)

    status = tk.Label(root, text="코어 확인 중 ...", anchor="w", relief="groove")
    status.pack(fill="x", padx=6, pady=(2, 6))

    def set_status(text: str) -> None:
        status.config(text=text)

    # --- 코어 폴링 (뒷단 스레드) → 화면은 결과만 ---
    box: dict[str, Any] = {"data": None, "misses": 0}

    def poll() -> None:
        while True:  # 이 스레드는 어떤 예외로도 죽지 않는다 (죽으면 영영 미접속)
            try:
                result = core_request("/state", timeout=2.0)
                if result is not None:
                    box["data"] = result
                    box["misses"] = 0
                else:
                    box["misses"] += 1
            except Exception:  # noqa: BLE001 - 폴링 스레드 보호
                box["misses"] += 1
            time.sleep(1.0)

    threading.Thread(target=poll, daemon=True).start()

    shown: dict[str, Any] = {"log": None, "peers": None}

    def refresh() -> None:
        # 어떤 예외로도 갱신 루프가 끊기지 않게 전면 방어. 무거운 위젯 갱신은
        # 실제 내용이 바뀔 때만(리스트박스·텍스트를 매초 다시 그리지 않음).
        try:
            data = box["data"]
            fx = data.get("fx") if isinstance(data, dict) else None
            if not isinstance(fx, dict):
                # 데이터 없음 — 한두 번 실패는 표시 안 하고, 여러 번 실패해야 미접속
                if not isinstance(data, dict):
                    if box["misses"] >= 3:
                        lbl_state.config(text="상태: 코어 미접속 (메인에서 코어 시작)",
                                         fg="#8b0000")
                else:
                    lbl_state.config(text="상태: 코어 구버전 — FX 미지원 (재시작)",
                                     fg="#8b0000")
                return
            if fx.get("connected") is False:
                lbl_state.config(text="상태: 코어 시세 미접속 (키·모드 확인)", fg="#8b0000")
                return
            paused = bool(fx.get("paused"))
            interval = fx.get("interval_s") or 0
            lbl_state.config(
                text=f"상태: {'일시정지' if paused else '자동 송신'} · 주기 {float(interval):g}초",
                fg="#8b0000" if paused else "dark green")
            btn_pause.config(text="재개" if paused else "일시정지")
            info = fx.get("last")
            if isinstance(info, dict):
                lbl_last.config(
                    text=f"마지막 송신: total_coin={float(info.get('total_coin', 0)):,.0f} "
                         f"fx={float(info.get('fx', 0)):,.2f} "
                         f"ok={info.get('ok')} @ {info.get('datetime', '')}")
            peers = fx.get("peers") or []
            peer_key = [(p.get("name"), p.get("ip"), p.get("port")) for p in peers]
            if peer_key != shown["peers"]:  # 바뀔 때만 리스트박스 다시 그림
                shown["peers"] = peer_key
                lst_peers.delete(0, "end")
                for p in peers:
                    lst_peers.insert(
                        "end", f"{p.get('name', '')} ({p.get('ip')}:{p.get('port')})")
                if not peers:
                    lst_peers.insert("end", "(수신자 없음)")
            entries = fx.get("log") or []
            text = "\n".join(str(e) for e in entries)
            if text != shown["log"]:  # 바뀔 때만 텍스트 다시 그림
                shown["log"] = text
                txt_log.config(state="normal")
                txt_log.delete("1.0", "end")
                txt_log.insert("end", text)
                txt_log.see("end")
                txt_log.config(state="disabled")
            set_status("코어 연결됨")
        except Exception:  # noqa: BLE001 - 갱신 1회 실패로 화면을 죽이지 않음
            pass
        finally:
            try:
                root.after(1000, refresh)
            except tk.TclError:
                pass

    # 주기 입력칸 초기값
    def fill_interval() -> None:
        data = box["data"]
        fx = data.get("fx") if isinstance(data, dict) else None
        if isinstance(fx, dict) and fx.get("interval_s") is not None:
            ent_interval.insert(0, f"{fx['interval_s']:g}")
        else:
            try:
                root.after(300, fill_interval)
            except tk.TclError:
                pass

    fill_interval()
    refresh()
    drain_results()
    while True:
        try:
            root.mainloop()
            break
        except KeyboardInterrupt:
            try:
                root.winfo_exists()
            except tk.TclError:
                break


def _to_float(text: str, default: float) -> float:
    try:
        return float(text.strip())
    except ValueError:
        return default


if __name__ == "__main__":
    main()
