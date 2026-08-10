"""화면 창 위치 저장·복원 (각 화면 프로세스가 마지막 위치를 기억).

- 화면별 이름(autoT/autoM/monitor/fx_monitor)으로 창 위치를 로컬 JSON에 보관.
- 위치(+X+Y)만 복원한다 — 크기는 각 화면이 정한 고정값을 그대로 쓴다(내용 잘림 방지).
- 주기적으로 저장하므로, 창을 X로 닫든 부모(메인) 종료로 죽든 마지막 위치가 남는다.
- 배포판(exe)은 실행파일 옆, 개발은 프로젝트 루트에 저장(main_window와 동일 규칙).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import tkinter as tk

_BASE_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
             else Path(__file__).resolve().parent.parent)
STATE_PATH = _BASE_DIR / "win_state.json"
FIELDS_PATH = _BASE_DIR / "win_fields.json"  # 화면별 폼 필드값(종목·체크박스 등) 저장


def position_only(geometry: str) -> str | None:
    """tkinter geometry(``'WxH+X+Y'``)에서 위치부(``'+X+Y'``)만 뽑는다.

    크기만 있거나 형식이 이상하면 ``None``. 멀티모니터 음수 좌표(``-``)도 처리. (순수 함수)
    """
    body = geometry.split("x", 1)[-1] if "x" in geometry else geometry
    for i, ch in enumerate(body):
        if ch in "+-":
            return body[i:]
    return None


def merge_geometry(store: dict[str, str], name: str, geometry: str) -> dict[str, str]:
    """``store``에 ``name`` → 위치를 얹은 새 dict. 위치를 못 뽑으면 원본 그대로. (순수 함수)"""
    pos = position_only(geometry)
    if pos is None:
        return dict(store)
    return {**store, name: pos}


def _load() -> dict[str, str]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, dict) else {}


def saved_position(name: str) -> str | None:
    """저장된 화면 위치(``'+X+Y'``) 또는 없으면 None."""
    return _load().get(name)


def save(name: str, geometry: str) -> None:
    """현재 창 위치를 저장(다른 화면 항목은 보존)."""
    data = merge_geometry(_load(), name, geometry)
    try:
        STATE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def saved_fields(name: str) -> dict[str, Any]:
    """저장된 화면 폼 필드값(dict). 없으면 빈 dict. (종목·체크박스 등)"""
    try:
        raw = json.loads(FIELDS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    val = raw.get(name) if isinstance(raw, dict) else None
    return {str(k): v for k, v in val.items()} if isinstance(val, dict) else {}


def save_fields(name: str, fields: dict[str, Any]) -> None:
    """화면 폼 필드값 저장(다른 화면 항목은 보존). 위치(win_state.json)와 별도 파일."""
    try:
        raw = json.loads(FIELDS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw[name] = fields
    try:
        FIELDS_PATH.write_text(json.dumps(raw), encoding="utf-8")
    except OSError:
        pass


def attach(root: tk.Tk, name: str, *, interval_ms: int = 2000) -> None:
    """창의 마지막 위치를 복원하고, 주기적으로 저장한다. tkinter 창에 붙인다."""
    import tkinter as tk

    pos = saved_position(name)
    if pos:
        try:
            root.geometry(pos)  # 위치만 이동(크기는 그대로)
        except tk.TclError:
            pass

    def _tick() -> None:
        try:
            save(name, root.winfo_geometry())
            root.after(interval_ms, _tick)
        except tk.TclError:
            pass  # 창 닫힘

    root.after(interval_ms, _tick)
