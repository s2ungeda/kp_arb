"""알람 사운드 재생 — winsound(윈도우 내장, wav, 의존성 0). DESIGN-settings §2.

설정창 미리듣기·메인창 알람이 공용으로 쓴다. 재생 실패가 앱을 죽이지 않게 사유만 반환.
"""
from __future__ import annotations


def play_wav(path: str) -> str | None:
    """wav 파일을 **비동기**로 재생. 성공이면 None, 실패면 사유 문자열.

    경로가 비었거나 winsound 없음(비윈도우)·파일 오류면 사유를 돌려준다(예외 안 냄).
    """
    if not path:
        return "경로 없음"
    try:
        import winsound
    except ImportError:
        return "winsound 없음(윈도우 전용)"
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as exc:  # noqa: BLE001 - 재생 실패는 사유만(앱 안 죽임)
        return str(exc)
    return None
