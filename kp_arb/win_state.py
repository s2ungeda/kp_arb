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


def saved_fields(name: str) -> dict[str, Any]:
    """저장된 화면 폼 필드값(dict). 없으면 빈 dict. (종목·체크박스 등)"""
    val = _read(_slotted(name)).get("fields")
    return {str(k): v for k, v in val.items()} if isinstance(val, dict) else {}


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
