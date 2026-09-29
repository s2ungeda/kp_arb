"""xing COM 스레드 — 같은 차선 동시 요청이 명령 루프를 무한 회전시키지 않는지 + 심장박동 정지 판정.

운영 PC 사고 2026-09-29 14:55: HL선 두 세트의 LS 후주문이 같은 순간 주문 차선에 들어오자 뒤 요청이
"다음 바퀴에"라며 큐에 자기를 다시 넣고 옛 `_drain`(큐가 빌 때까지)이 그걸 즉시 다시 꺼내 영원히
돌았다 → 펌프·시간 초과가 안 돌아 LS 조회·주문·실시간 전부 정지, 세션은 '연결'로 보임."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_xing_com import CFOAT, T1102, FakeFactory  # 가짜 COM 공장·Res 본문 재사용

from kp_arb.gateways.xing_com import LANE_ORDER, XingSession, XingTimeout


@pytest.fixture
def res_dir(tmp_path: Path) -> Path:
    for name, text in (("t1102", T1102), ("CFOAT00100", CFOAT)):
        (tmp_path / f"{name}.res").write_bytes(text.encode("cp949"))
    return tmp_path


async def test_two_requests_on_same_lane_do_not_spin_the_com_thread(res_dir: Path) -> None:
    fac = FakeFactory()  # responses 비움 → 어떤 TR도 응답 없음(시간 초과 경로)
    s = XingSession(fac, res_dir)
    await s.start()
    try:
        body = {"CFOAT00100InBlock1": {"AcntNo": "1", "FnoIsuNo": "A116A000"}}
        # 두 주문 요청을 같은 순간 — 옛 코드는 여기서 COM 스레드가 무한 회전(테스트가 걸림)
        q1 = asyncio.create_task(s.query("CFOAT00100", body, lane=LANE_ORDER, timeout_s=0.3))
        q2 = asyncio.create_task(s.query("CFOAT00100", body, lane=LANE_ORDER, timeout_s=0.3))
        results = await asyncio.wait_for(asyncio.gather(q1, q2, return_exceptions=True), 5.0)
        assert all(isinstance(r, XingTimeout) for r in results)  # 둘 다 시간 초과(걸리지 않음)
        assert len(fac.requests) == 2                            # 뒤 요청도 앞이 끝난 뒤 실제 전송
        # 스레드는 계속 돈다 — 심장박동 갱신, 정지 아님
        beat = s.last_beat
        await asyncio.sleep(0.15)
        assert s.last_beat > beat and not s.thread_stalled()
    finally:
        s.close()


def test_thread_stalled_uses_last_beat(res_dir: Path) -> None:
    now = [1000.0]
    s = XingSession(FakeFactory(), res_dir, clock=lambda: now[0])
    assert not s.thread_stalled()          # 시작 전(0)은 정지 아님
    s.last_beat = 1000.0
    assert not s.thread_stalled()
    now[0] = 1004.0
    assert not s.thread_stalled(5.0)
    now[0] = 1006.0
    assert s.thread_stalled(5.0)           # 5초 넘게 심장박동 없음 → 정지


async def test_asyncio_side_timeout_when_com_thread_never_answers(res_dir: Path) -> None:
    # _expire(COM 스레드)가 못 돌아도 asyncio 쪽 2차 시간 초과가 XingTimeout으로 떨어뜨린다.
    fac = FakeFactory()
    s = XingSession(fac, res_dir)
    await s.start()
    try:
        s._expire = lambda: None  # type: ignore[method-assign] - COM 쪽 시간 초과가 죽은 상황 흉내
        with pytest.raises(XingTimeout, match="무응답"):
            await s.query("t1102", {"t1102InBlock": {"shcode": "005930"}}, timeout_s=0.1)
    finally:
        s.close()
