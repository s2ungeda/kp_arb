"""HL 호가단위(틱) 옵션 계산 — 순수 로직 (DESIGN §5.10).

가격 자릿수 기준으로 HL 유효숫자(nSigFigs) 뭉치기 단계를 만든다. 코어가 계산해
manual_snapshot에 실어주면, 화면은 '적' 전에도 콤보를 바로 채운다(라이브 가격 의존 제거).
가격 크기(자릿수)는 종목별로 안정적이라 사실상 고정 — 크게 변해도 자동 반영된다.
"""
from __future__ import annotations

import math

# (기준틱 배수, nSigFigs, mantissa) — HL은 유효숫자 기준으로 뭉치므로, 종목별 실제 틱 =
# 기준틱×배수로 표시한다(원시/2배 라벨 대신). nSigFigs=5일 때만 mantissa(1·2·5) 유효.
_MERGE_LEVELS: list[tuple[int, int | None, int | None]] = [
    (1, None, None), (2, 5, 2), (5, 5, 5), (10, 4, None), (100, 3, None), (1000, 2, None)]


def _fmt_tick(v: float) -> str:
    """틱 표시 — 정수면 정수, 소수면 끝 0 제거(4자리 이내). 화면 콤보 라벨과 동일 형식."""
    if v == int(v):
        return f"{int(v):,d}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def merge_tick_options(price: float) -> list[tuple[str, int | None, int | None]]:
    """활성 종목 가격 기준 호가단위 옵션 — [(틱표시, nSigFigs, mantissa), ...].

    기준틱 = 10^(floor(log10 가격) − 4)(유효숫자 5자리), 그 배수로 단계 구성.
    가격 ≤ 0(미수신)이면 빈 목록.
    """
    if price <= 0:
        return []
    base = 10.0 ** (math.floor(math.log10(abs(price))) - 4)
    return [(_fmt_tick(base * mult), nsf, mant) for mult, nsf, mant in _MERGE_LEVELS]
