"""화면 창 위치·설정 저장·복원 (각 화면 프로세스가 마지막 상태를 기억).

- 창별 이름(main/autoT/order_hl 등) + 인스턴스 슬롯(``KP_WIN_SLOT``)으로 키를 만들고,
  **키마다 별도 파일**(``.win_state/<키>.json``)에 위치·필드를 담는다.
  → 메인·여러 화면(각각 별도 프로세스)이 한 파일을 동시에 읽고-고쳐-쓰다 서로
    덮어쓰는 경합이 사라진다(예전 단일 파일의 '저장이 될 때도 안 될 때도' 문제 해결).
- 위치(+X+Y)만 복원한다 — 크기는 각 화면이 정한 고정값을 그대로 쓴다(내용 잘림 방지).
- 주기적으로 저장하므로, 창을 X로 닫든 부모(메인) 종료로 죽든 마지막 상태가 남는다.
- 배포판(exe)은 실행파일 옆, 개발은 프로젝트 루트에 저장(main_window와 동일 규칙).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import tkinter as tk

_BASE_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
             else Path(__file__).resolve().parent.parent)
_STATE_DIR = _BASE_DIR / ".win_state"  # 키마다 파일 하나 — 프로세스 간 경합 방지


def position_only(geometry: str) -> str | None:
    """tkinter geometry(``'WxH+X+Y'``)에서 위치부(``'+X+Y'``)만 뽑는다.

    크기만 있거나 형식이 이상하면 ``None``. 멀티모니터 음수 좌표(``-``)도 처리. (순수 함수)
    """
    body = geometry.split("x", 1)[-1] if "x" in geometry else geometry
    for i, ch in enumerate(body):
        if ch in "+-":
            return body[i:]
    return None


def storage_key(name: str, slot: str | None) -> str:
    """저장 키 — 슬롯 있으면 인스턴스별(``name#slot``), 없으면 ``name``. (순수 함수)

    같은 종류 창을 여러 개 띄우면 각 프로세스가 다른 슬롯을 받아 위치·설정이 안 섞인다.
    """
    return f"{name}#{slot}" if slot else name


def _slotted(name: str) -> str:
    """현재 프로세스 슬롯(``KP_WIN_SLOT``)을 붙인 저장 키."""
    return storage_key(name, os.environ.get("KP_WIN_SLOT"))


def _key_path(key: str) -> Path:
    """키 → 파일 경로(파일명에 안전한 문자만)."""
    safe = "".join(c if (c.isalnum() or c in "_#.-") else "_" for c in key)
    return _STATE_DIR / f"{safe}.json"


def _read(key: str) -> dict[str, Any]:
    try:
        raw = json.loads(_key_path(key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(key: str, data: dict[str, Any]) -> None:
    """바뀔 때만·원자적·세대 백업 3개(.win_state/backup/) — state_backup 공통."""
    from .state_backup import write_if_changed

    write_if_changed(_key_path(key), json.dumps(data), generations=3)


def saved_position(name: str) -> str | None:
    """저장된 화면 위치(``'+X+Y'``) 또는 없으면 None."""
    pos = _read(_slotted(name)).get("pos")
    return pos if isinstance(pos, str) else None


def save(name: str, geometry: str, *, keep_size: bool = False) -> None:
    """현재 창 위치를 저장(같은 키의 필드값은 보존). 이 키 파일만 건드려 경합 없음.

    keep_size=True면 크기까지(``'WxH+X+Y'`` 통째로) 저장 — 시세 화면처럼 사용자가 늘려 쓰는 창용.
    """
    pos = position_only(geometry)
    if pos is None:
        return
    key = _slotted(name)
    data = _read(key)
    data["pos"] = pos
    if keep_size and is_full_geometry(geometry):
        data["geom"] = geometry
    _write(key, data)


def is_full_geometry(geometry: str) -> bool:
    """``'WxH+X+Y'`` 모양인가(크기+위치 둘 다). 크기 저장 대상 판정. (순수 함수)"""
    return re.fullmatch(r"\d+x\d+[+-]\d+[+-]\d+", geometry) is not None


def saved_geometry(name: str) -> str | None:
    """저장된 크기+위치(``'WxH+X+Y'``) 또는 없으면 None — keep_size 창용."""
    geom = _read(_slotted(name)).get("geom")
    return geom if isinstance(geom, str) and is_full_geometry(geom) else None


def _newest_sibling_fields(name: str) -> dict[str, Any] | None:
    """같은 창 이름의 다른 슬롯 파일(``name.json``·``name#n.json``) 중 **가장 최근 저장** 필드값.

    슬롯은 "살아 있는 같은 창이 안 쓰는 가장 작은 번호"라, 창을 하나 더 띄웠다 닫는 사이에 쓰던
    창이 #1에 저장되면 다음에 혼자 띄운 창(#0)은 옛 #0 파일을 복원해 값이 사라진 것처럼 보인다
    (실측 2026-09-17 체결쏴 주식: 종목·거래소가 기본값으로 돌아옴). 새로 뜬 창의 슬롯에 저장된
    필드가 없으면 최근 파일을 물려받는다.
    """
    safe = _key_path(name).stem
    best: tuple[float, dict[str, Any]] | None = None
    try:
        candidates = list(_STATE_DIR.glob(f"{safe}.json")) + list(_STATE_DIR.glob(f"{safe}#*.json"))
    except OSError:
        return None
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            continue
        fields = raw.get("fields") if isinstance(raw, dict) else None
        if isinstance(fields, dict) and fields and (best is None or mtime > best[0]):
            best = (mtime, {str(k): v for k, v in fields.items()})
    return best[1] if best else None


def saved_fields(name: str) -> dict[str, Any]:
    """저장된 화면 폼 필드값(dict). 없으면 빈 dict. (종목·체크박스 등)

    이 슬롯에 저장된 필드가 없으면 같은 창 이름의 가장 최근 파일 것(_newest_sibling_fields)."""
    val = _read(_slotted(name)).get("fields")
    if isinstance(val, dict) and val:
        return {str(k): v for k, v in val.items()}
    return _newest_sibling_fields(name) or {}


def save_fields(name: str, fields: dict[str, Any]) -> None:
    """화면 폼 필드값 저장(같은 키의 위치는 보존). 이 키 파일만 건드려 경합 없음."""
    key = _slotted(name)
    data = _read(key)
    data["fields"] = fields
    _write(key, data)


def attach(root: tk.Tk, name: str, *, interval_ms: int = 2000,
           keep_size: bool = False) -> None:
    """창의 마지막 위치를 복원하고, 주기적으로 저장한다. tkinter 창에 붙인다.

    같은 종류 창을 여러 개 띄우면 프로세스별 슬롯(``KP_WIN_SLOT``)이 키에 붙어
    각 창이 자기 위치를 따로, 서로 안 덮어쓰고 기억한다.
    keep_size=True면 크기도 함께 복원·저장한다(기본은 위치만 — 고정 크기 창의 내용 잘림 방지).
    """
    import tkinter as tk

    set_taskbar_group()
    target = (saved_geometry(name) if keep_size else None) or saved_position(name)
    if target:
        try:
            root.geometry(target)  # 위치(+크기) 복원
        except tk.TclError:
            pass

    def _tick() -> None:
        try:
            save(name, root.winfo_geometry(), keep_size=keep_size)
            root.after(interval_ms, _tick)
        except tk.TclError:
            pass  # 창 닫힘

    root.after(interval_ms, _tick)
    if name != "main":
        follow_main_minimize(root)


TASKBAR_APP_ID = "kp-arb.meme"  # 메인·화면·(개발 실행의 python/pythonw) 전부 한 작업표시줄 묶음


def set_taskbar_group(app_id: str = TASKBAR_APP_ID) -> None:
    """이 프로세스의 창을 작업표시줄에서 다른 우리 창들과 **한 단추로** 묶는다(Windows 전용).

    작업표시줄은 기본적으로 실행 파일 단위로 묶는다 — 개발 실행은 메인이 pythonw.exe, 화면이
    python.exe라 단추가 둘로 갈려 헷갈렸다(사용자 2026-09-17). 창을 만들기 전에 같은 앱 식별자를
    주면 하나로 묶인다. 배포판(meme.exe)은 원래 하나지만 같이 준다(무해)."""
    import sys

    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        pass


def mirror_action(main_iconic: bool, my_state: str, auto_iconified: bool) -> str | None:
    """메인 창 최소화를 따라가는 판단(순수 함수) — "iconify" / "deiconify" / "restore_main" / None.

    - 메인이 최소화됐는데 내가 아니면 최소화. 단 내가 **메인 때문에** 최소화됐다가 (작업표시줄에서)
      사용자가 나만 복원한 경우면 메인을 복원("restore_main") — 그러면 다른 창들도 따라 올라온다
      (사용자 2026-09-17: 작업표시줄 아이콘을 누르면 모든 화면이 활성화돼야 함).
    - 메인이 돌아왔는데 내가 메인 때문에 최소화된 상태면 복원(사용자가 따로 최소화해 둔 창은
      건드리지 않음).
    """
    if main_iconic and my_state != "iconic":
        return "restore_main" if auto_iconified else "iconify"
    if not main_iconic and auto_iconified and my_state == "iconic":
        return "deiconify"
    return None


def follow_main_minimize(root: tk.Tk, interval_ms: int = 300) -> None:
    """메인 창(``KP_MAIN_HWND``)이 최소화되면 이 창도 최소화, 메인이 돌아오면 같이 복원
    (사용자 2026-09-17: 다른 프로그램처럼 메인을 최소화하면 전부 최소화). 화면은 메인의 자식 창이
    아니라 **별도 프로세스**라 윈도우가 알아서 묶어 주지 않는다 — 각 창이 메인 창 상태를
    0.3초마다 보고 따라간다(작업표시줄 단추는 창마다 그대로). Windows 전용, 환경변수 없으면 무시."""
    import sys
    import tkinter as tk

    raw = os.environ.get("KP_MAIN_HWND")
    if sys.platform != "win32" or not raw:
        return
    try:
        import ctypes

        hwnd = int(raw)
        user32 = ctypes.windll.user32
    except (ValueError, AttributeError, OSError):
        return
    box = {"auto": False}

    def _tick() -> None:
        try:
            if not user32.IsWindow(hwnd):
                return  # 메인이 사라짐(감시는 watch_parent_exit가 맡음)
            act = mirror_action(bool(user32.IsIconic(hwnd)), str(root.state()), box["auto"])
            if act == "iconify":
                root.iconify()
                box["auto"] = True
            elif act == "deiconify":
                root.deiconify()
                box["auto"] = False
            root.after(interval_ms, _tick)
        except tk.TclError:
            pass  # 창 닫힘

    root.after(interval_ms, _tick)
