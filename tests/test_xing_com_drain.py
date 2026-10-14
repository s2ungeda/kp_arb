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
        # 뒤 요청은 앞이 끝났을 때 이미 자기 마감이 지나 **보내지 않는다**(검토 10-13 ★7 (a) —
        # 옛 코드는 여기서 옛 가격으로 전송했다). 사유도 "앞 요청에 막혀 마감 지남"
        assert len(fac.requests) == 1
        assert any("마감 지남" in str(r) for r in results)
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


async def test_late_response_of_expired_request_is_not_attributed_to_next(res_dir: Path) -> None:
    # 검토 2026-10-13 ★7 (b): 시간 초과로 정리한 앞 요청(A)의 늦은 응답이 같은 차선의 다음 요청(B)
    # 결과가 되던 것 — B가 A의 주문번호로 등록되거나 A의 거부로 B를 재발주하는 길. 요청마다
    # XAQuery를 새로 만들고 이벤트를 순번으로 귀속시켜 A의 응답은 버리고 B는 제 응답만 받는다.
    fake_now = [100.0]
    fac = FakeFactory()
    s = XingSession(fac, res_dir, clock=lambda: fake_now[0])
    await s.start()
    try:
        body = {"CFOAT00100InBlock1": {"AcntNo": "1", "FnoIsuNo": "A116A000"}}
        a = asyncio.create_task(s.query("CFOAT00100", body, lane=LANE_ORDER, timeout_s=5.0))
        await asyncio.sleep(0.1)
        assert len(fac.queries) == 1 and not a.done()
        q_a = fac.queries[0]
        fake_now[0] = 106.0                                  # A 시간 초과(COM 쪽 _expire)
        with pytest.raises(XingTimeout):
            await asyncio.wait_for(a, 2.0)
        b = asyncio.create_task(s.query("CFOAT00100", body, lane=LANE_ORDER, timeout_s=5.0))
        await asyncio.sleep(0.1)
        assert len(fac.queries) == 2 and not b.done()
        # A의 늦은 응답이 옛 XAQuery로 도착 — B의 결과가 되면 안 된다
        q_a.out = {"CFOAT00100OutBlock2": {"OrdNo": "111"}}
        q_a.sink("message", False, "00039", "늦은 응답")
        q_a.sink("data", "CFOAT00100")
        await asyncio.sleep(0.1)
        assert not b.done()                                  # 무시됨
        q_b = fac.queries[1]                                 # B 제 응답
        q_b.out = {"CFOAT00100OutBlock2": {"OrdNo": "222"}}
        q_b.sink("message", False, "00039", "ok")
        q_b.sink("data", "CFOAT00100")
        r = await asyncio.wait_for(b, 2.0)
        assert r.blocks["CFOAT00100OutBlock2"]["OrdNo"] == "222"
    finally:
        s.close()


async def test_asyncio_timeout_leaves_pending_cleanup_to_com_thread(res_dir: Path) -> None:
    # 검토 2026-10-13 ★7 (c): asyncio 쪽 2차 시간 초과가 COM 전용 _pending을 직접 pop하지 않고
    # COM 스레드에 맡긴다 — 그 요청만(순번 대조) 뺀다. 스레드가 돌고 있으면 바로 빠져 다음 요청이
    # 같은 차선을 쓴다.
    fac = FakeFactory()
    s = XingSession(fac, res_dir)
    await s.start()
    try:
        s._expire = lambda: None  # type: ignore[method-assign]
        with pytest.raises(XingTimeout, match="무응답"):
            await s.query("t1102", {"t1102InBlock": {"shcode": "005930"}}, timeout_s=0.1)
        await asyncio.sleep(0.15)                            # COM 스레드가 _abandon을 돌 시간
        assert "query" not in s._pending
        fac.responses["t1102"] = ("00000", "", {"t1102OutBlock": {"hname": "h", "price": "1"}})
        r = await asyncio.wait_for(
            s.query("t1102", {"t1102InBlock": {"shcode": "005930"}}, timeout_s=1.0), 2.0)
        assert r.rsp_cd == "00000"
    finally:
        s.close()
