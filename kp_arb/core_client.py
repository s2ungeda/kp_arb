"""코어 API 클라이언트 공용 — 화면(메인/전략/모니터)들이 공유 (DESIGN §12).

코어는 localhost에서만 듣는다. 실패(미접속 등)는 None — 화면은 표시만 한다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

CORE_URL = "http://127.0.0.1:8787"


def core_request(path: str, payload: dict[str, Any] | None = None,
                 timeout: float = 1.0) -> dict[str, Any] | None:
    """GET(payload 없음)/POST(payload=JSON) 요청. 실패는 None."""
    url = f"{CORE_URL}{path}"
    try:
        if payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
