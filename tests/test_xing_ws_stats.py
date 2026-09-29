"""xing 실시간 수신 통계 로그 — TR별 1분 건수·장중 0건 경고(운영 PC 카운터 정지 의심 2026-09-29)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from kp_arb.domain.enums import Underlying
from kp_arb.gateways.xing_ws import XingRealClient, format_real_stats, in_market_hours


class FakeSession:
    def __init__(self) -> None:
        self.on_real: list[Any] = []
        self.on_session: list[Any] = []

    async def advise(self, tr: str, key: str) -> None:
        return None

    def push(self, tr: str, fields: dict[str, str]) -> None:
        for cb in self.on_real:
            cb(tr, "", fields)


def test_format_real_stats_sorts_and_totals() -> None:
    assert format_real_stats({"H1_": 120, "JH0": 450, "SC1": 0}) == "총 570건 — JH0 450, H1_ 120"
    assert format_real_stats({}) == "총 0건 — 없음"
    many = {str(i): i for i in range(1, 20)}
    assert format_real_stats(many, top=3) == "총 190건 — 19 19, 18 18, 17 17"


def test_in_market_hours_window() -> None:
    assert in_market_hours(time.struct_time((2026, 9, 29, 8, 30, 0, 1, 272, 0)))
    assert in_market_hours(time.struct_time((2026, 9, 29, 15, 49, 59, 1, 272, 0)))
    assert not in_market_hours(time.struct_time((2026, 9, 29, 8, 29, 59, 1, 272, 0)))
    assert not in_market_hours(time.struct_time((2026, 9, 29, 15, 50, 0, 1, 272, 0)))


async def test_stats_loop_logs_per_tr_counts(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeSession()

    async def ensure_login() -> None:
        return None

    client = XingRealClient(fake, ensure_login=ensure_login,  # type: ignore[arg-type]
                            reconnect_backoff_s=0.0, stats_every_s=0.05)
    client.subscribe_quotes(Underlying.SAMSUNG)
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.02)
    fake.push("H1_", {"shcode": "005930", "bidho1": "1", "offerho1": "2", "hotime": "090000"})
    fake.push("H1_", {"shcode": "005930", "bidho1": "1", "offerho1": "2", "hotime": "090001"})
    with caplog.at_level(logging.INFO, logger="kp_arb.xing"):
        await asyncio.sleep(0.12)
    client.stop()
    await asyncio.wait_for(task, 1.0)
    lines = [r.getMessage() for r in caplog.records
             if "실시간" in r.getMessage() and "수신" in r.getMessage()]
    assert lines and "H1_ 2" in lines[0]            # 첫 구간: 두 건
    assert client._real_counts == {"H1_": 2}
