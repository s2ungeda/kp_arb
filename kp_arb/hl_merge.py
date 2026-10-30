"""HL 호가단위(틱) 옵션 계산 — 순수 로직 (DESIGN §5.10).

가격 자릿수 기준으로 HL 유효숫자(nSigFigs) 뭉치기 단계를 만든다. 코어가 계산해
manual_snapshot에 실어주면, 화면은 '적' 전에도 콤보를 바로 채운다(라이브 가격 의존 제거).

**고른 값의 단일 진실은 틱(USD 숫자)**(사용자 2026-10-27 "원래 가격대별로 호가단위가 있어야 함"):
HL 서버의 머지 인자(nSigFigs, mantissa)는 유효숫자 기준이라 가격이 자릿수를 넘으면 같은 인자가
10배 틱이 된다 — 운영 실측: 하이닉스가 1,000달러를 넘자 저장돼 있던 nSigFigs 4가 0.1달러에서
1달러 격자로 변했는데 화면은 옛 라벨 "0.1"을 보여 줌. 그래서 코어는 고른 틱을 저장하고, 가격대가
바뀌면 그 틱에 맞는 인자를 다시 고른다(merge_choice_for_tick).
"""
from __future__ import annotations

import math
from typing import Any

# (기준틱 배수, nSigFigs, mantissa) — HL은 유효숫자 기준으로 뭉치므로, 종목별 실제 틱 =
# 기준틱×배수로 표시한다(원시/2배 라벨 대신). nSigFigs=5일 때만 mantissa(1·2·5) 유효.
_MERGE_LEVELS: list[tuple[int, int | None, int | None]] = [
    (1, None, None), (2, 5, 2), (5, 5, 5), (10, 4, None), (100, 3, None), (1000, 2, None)]


def fmt_tick(v: float) -> str:
    """틱 표시 — 정수면 정수, 소수면 끝 0 제거(4자리 이내). 화면 콤보 라벨과 동일 형식."""
    if v == int(v):
        return f"{int(v):,d}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


_fmt_tick = fmt_tick  # 옛 이름


def merge_band(price: float) -> int | None:
    """가격대(자릿수) — floor(log10 가격). 같은 가격대 안에선 틱 표가 같다. 가격 ≤ 0이면 None."""
    if price <= 0:
        return None
    return math.floor(math.log10(abs(price)))


def _base_tick(price: float) -> float:
    """기준틱 = 10^(floor(log10 가격) − 4) — 유효숫자 5자리 한 칸."""
    return 10.0 ** (math.floor(math.log10(abs(price))) - 4)


def merge_tick_values(price: float) -> list[tuple[float, int | None, int | None]]:
    """활성 종목 가격 기준 호가단위 옵션(숫자) — [(틱, nSigFigs, mantissa), ...]. 가격 ≤ 0이면
    빈 목록."""
    if price <= 0:
        return []
    base = _base_tick(price)
    return [(base * mult, nsf, mant) for mult, nsf, mant in _MERGE_LEVELS]


def merge_tick_options(price: float) -> list[tuple[str, int | None, int | None]]:
    """활성 종목 가격 기준 호가단위 옵션 — [(틱표시, nSigFigs, mantissa), ...].

    기준틱 = 10^(floor(log10 가격) − 4)(유효숫자 5자리), 그 배수로 단계 구성.
    가격 ≤ 0(미수신)이면 빈 목록.
    """
    return [(fmt_tick(v), nsf, mant) for v, nsf, mant in merge_tick_values(price)]


def merge_tick_size(price: float, n_sig_figs: int | None, mantissa: int | None) -> float:
    """화면 호가단위 콤보에서 고른 (nSigFigs, mantissa)의 실제 틱 크기(USD) — HL선 선주문의
    **주문단위**(exec §7D, 사용자 2026-09-22: 취소·재발주가 잦아 격자 대신 콤보 단위로 맞춤).
    모르는 조합·가격 없음이면 기준틱(유효숫자 5자리 한 칸)."""
    if price <= 0:
        return 0.0
    mult = next((m for m, nsf, mant in _MERGE_LEVELS if (nsf, mant) == (n_sig_figs, mantissa)),
                1)
    return _base_tick(price) * mult


def merge_active_dict(active: tuple[int | None, int | None] | None,
                      ref: float | None) -> dict[str, Any] | None:
    """지금 적용 중인 머지를 화면용 dict로 — n_sig_figs·mantissa + 그 가격대의 틱 글(ref 없으면
    None). 일반주문창·체결쏴·NXT 개장·동시호가 스냅샷이 전부 이것을 쓴다(한 규칙 — 2026-10-30 체결쏴
    호가단위 글자가 "-"로 남은 실증: 틱 글 없는 스냅샷을 화면이 못 읽음). 순수."""
    if active is None:
        return None
    tick = fmt_tick(merge_tick_size(float(ref), active[0], active[1])) if ref else None
    return {"n_sig_figs": active[0], "mantissa": active[1], "tick": tick}


def merge_choice_for_tick(price: float,
                          tick: float) -> tuple[float, int | None, int | None] | None:
    """고른 틱(USD)에 맞는 (적용 틱, nSigFigs, mantissa) — 지금 가격대 옵션 중 같은 틱, 없으면
    로그 거리가 가장 가까운 것(같은 거리면 작은 쪽). 예: 0.1을 골랐으면 999달러대에선 nSigFigs 4,
    1,009달러대에선 원시(None). 0.01을 골랐는데 1,000달러를 넘으면 0.1(가장 가까운 것).
    가격·틱 ≤ 0이면 None."""
    if price <= 0 or tick <= 0:
        return None
    vals = merge_tick_values(price)
    for v in vals:
        if math.isclose(v[0], tick, rel_tol=1e-9):
            return v
    return min(vals, key=lambda v: (abs(math.log10(v[0]) - math.log10(tick)), v[0]))
