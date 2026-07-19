"""원달러 외환현물 조회 — 네이버 금융(하나은행 고시 매매기준율).

엑셀 RTD(exrate_rtd_server.py의 USDKRWSPOT)와 **같은 소스** — 값 대조가 정확해진다.
LS API에는 현물환율 TR이 없어 외부 공개 API를 쓴다. 주간(fx_spot_window)
HL 환산용으로 30초 폴링(_fx_loop) — 네이버 과다호출 스로틀(5초)보다 충분히 느리다.
"""
from __future__ import annotations

from typing import Any

NAVER_SPOT_URL = (
    "https://m.stock.naver.com/front-api/marketIndex/"
    "productDetail?category=exchange&reutersCode=FX_USDKRW"
)
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}


def parse_spot(obj: dict[str, Any]) -> float | None:
    """응답 JSON → 현물환율. calcPrice(숫자) 우선, closePrice(콤마 문자열) 폴백."""
    result = obj.get("result") or {}
    raw: Any = result.get("calcPrice")
    if raw in (None, ""):
        raw = str(result.get("closePrice") or "").replace(",", "")
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


async def fetch_usdkrw_spot(timeout_s: float = 10.0) -> float | None:
    """네이버에서 현물환율 1건 조회. 실패는 None — 호출측이 환율이론가로 대체."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                NAVER_SPOT_URL,
                headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status != 200:
                    return None
                obj = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 - 네트워크 실패는 폴백(이론가)으로 흡수
        return None
    return parse_spot(obj) if isinstance(obj, dict) else None
