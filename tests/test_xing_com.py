"""xingAPI COM 세션(xing_com) — 가짜 COM으로 스레드 경계·직렬화·시간 초과·이벤트 (라이브 없음)."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from kp_arb.gateways.xing_com import (
    LANE_ORDER,
    LANE_QUERY,
    QueryResult,
    XingError,
    XingSession,
    XingTimeout,
    is_ok_code,
)

T1102 = ("BEGIN_FUNCTION_MAP\n.Func,현재가,t1102,attr;\nBEGIN_DATA_MAP\n"
         "t1102InBlock,입력,input;\nbegin\n"
         "단축코드,shcode,shcode,char,6;\nend\nt1102OutBlock,출력,output;\nbegin\n"
         "한글명,hname,hname,char,20;\n현재가,price,price,long,8;\nend\nEND_DATA_MAP\nEND_FUNCTION_MAP\n")
T0441 = ("BEGIN_FUNCTION_MAP\n.Func,잔고,t0441,attr;\nBEGIN_DATA_MAP\n"
         "t0441InBlock,입력,input;\nbegin\n"
         "계좌번호,accno,accno,char,11;\n비밀번호,passwd,passwd,char,8;\nend\n"
         "t0441OutBlock1,출력,output,occurs;\nbegin\n종목번호,expcode,expcode,char,32;\n"
         "잔고수량,jqty,jqty,long,18;\nend\nEND_DATA_MAP\nEND_FUNCTION_MAP\n")
CFOAT = ("BEGIN_FUNCTION_MAP\n.Func,선물주문,CFOAT00100,attr;\nBEGIN_DATA_MAP\n"
         "CFOAT00100InBlock1,입력,input;\nbegin\n계좌,AcntNo,AcntNo,char,20;\n"
         "종목,FnoIsuNo,FnoIsuNo,char,12;\nend\nCFOAT00100OutBlock2,출력2,output;\nbegin\n"
         "주문번호,OrdNo,OrdNo,long,10;\nend\nEND_DATA_MAP\nEND_FUNCTION_MAP\n")
H1 = ("BEGIN_FUNCTION_MAP\n.Func,호가,H1_,attr;\nBEGIN_DATA_MAP\nH1_InBlock,입력,input;\nbegin\n"
      "단축코드,shcode,shcode,char,6;\nend\nH1_OutBlock,출력,output;\nbegin\n"
      "종목코드,shcode,shcode,char,6;\n매수1,bidho1,bidho1,long,8;\nend\nEND_DATA_MAP\nEND_FUNCTION_MAP\n")


class FakeWaker:
    def __init__(self) -> None:
        self._e = threading.Event()

    def set(self) -> None:
        self._e.set()

    def wait(self, timeout_ms: int) -> None:
        self._e.wait(timeout_ms / 1000)
        self._e.clear()


class FakeSession:
    sink: Any = None

    def __init__(self, fac: FakeFactory) -> None:
        self.fac = fac
        self.connected = False

    def ConnectServer(self, host: str, port: int) -> bool:  # noqa: N802
        self.fac.calls.append(("connect", host, port))
        self.connected = not self.fac.connect_fail
        return self.connected

    def Login(self, uid: str, pw: str, cert: str, stype: int, dlg: bool) -> bool:  # noqa: N802
        self.fac.calls.append(("login", uid, stype))  # 비번은 기록 안 함
        code, msg = self.fac.login_result
        self.sink("login", code, msg)  # xing은 이벤트로 결과를 준다(같은 스레드에서 흉내)
        return True

    def GetLastError(self) -> int:  # noqa: N802
        return -1

    def GetErrorMessage(self, code: int) -> str:  # noqa: N802
        return f"err{code}"

    def GetAccountListCount(self) -> int:  # noqa: N802
        return len(self.fac.accounts)

    def GetAccountList(self, i: int) -> str:  # noqa: N802
        return self.fac.accounts[i]


class FakeQuery:
    sink: Any = None
    ResFileName = ""
    IsNext = False

    def __init__(self, fac: FakeFactory) -> None:
        self.fac = fac
        self.fields: dict[tuple[str, str, int], str] = {}
        self.counts: dict[str, int] = {}
        self.out: dict[str, Any] = {}

    def SetBlockCount(self, block: str, n: int) -> None:  # noqa: N802
        self.counts[block] = n

    def SetFieldData(self, block: str, fld: str, i: int, v: str) -> None:  # noqa: N802
        self.fields[(block, fld, i)] = v

    def Request(self, nxt: bool) -> int:  # noqa: N802
        self.fac.requests.append((self.ResFileName, dict(self.fields), nxt))
        tr = Path(self.ResFileName).stem
        if tr in self.fac.request_fail:
            return -21
        behaviour = self.fac.responses.get(tr)
        if behaviour is None:
            return 1  # 응답 없음(시간 초과 시험)
        code, msg, out = behaviour
        self.out = out
        # xing 순서: OnReceiveMessage → (성공이면) OnReceiveData
        self.sink("message", False, code, msg)
        if is_ok_code(code):
            self.sink("data", tr)
        return 1

    def GetErrorMessage(self, rc: int) -> str:  # noqa: N802
        return f"reqerr{rc}"

    def GetBlockCount(self, block: str) -> int:  # noqa: N802
        rows = self.out.get(block, [])
        return len(rows) if isinstance(rows, list) else 1

    def GetFieldData(self, block: str, fld: str, i: int) -> str:  # noqa: N802
        rows = self.out.get(block, {})
        row = rows[i] if isinstance(rows, list) else rows
        return str(row.get(fld, ""))

    def GetTRCountPerSec(self, tr: str) -> int:  # noqa: N802
        return 10

    def GetTRCountBaseSec(self, tr: str) -> int:  # noqa: N802
        return 1

    def GetTRCountLimit(self, tr: str) -> int:  # noqa: N802
        return 200

    def GetTRCountRequest(self, tr: str) -> int:  # noqa: N802
        return 3


class FakeReal:
    sink: Any = None
    ResFileName = ""

    def __init__(self, fac: FakeFactory) -> None:
        self.fac = fac
        self.keys: list[tuple[str, str, str]] = []
        self.advised = 0
        self.fields: dict[str, str] = {}

    def SetFieldData(self, block: str, fld: str, v: str) -> None:  # noqa: N802
        self.keys.append((block, fld, v))

    def AdviseRealData(self) -> None:  # noqa: N802
        self.advised += 1

    def UnadviseRealData(self) -> None:  # noqa: N802
        self.advised = 0

    def GetFieldData(self, block: str, fld: str) -> str:  # noqa: N802
        return self.fields.get(fld, "")


class FakeFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.requests: list[tuple[str, dict[tuple[str, str, int], str], bool]] = []
        self.connect_fail = False
        self.login_result = ("0000", "로그인 성공")
        self.accounts = ["20142871002", "55501234567"]
        self.responses: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self.request_fail: set[str] = set()
        self.reals: dict[str, FakeReal] = {}
        self.queries: list[FakeQuery] = []
        self.thread_ids: list[int] = []

    def init_thread(self) -> None:
        self.thread_ids.append(threading.get_ident())

    def uninit_thread(self) -> None:
        pass

    def make_waker(self) -> FakeWaker:
        return FakeWaker()

    def pump(self) -> None:
        pass

    def create_session(self) -> FakeSession:
        self.session = FakeSession(self)
        return self.session

    def create_query(self) -> FakeQuery:
        q = FakeQuery(self)
        self.queries.append(q)
        return q

    def create_real(self) -> FakeReal:
        r = FakeReal(self)
        self._last_real = r
        return r


@pytest.fixture
def res_dir(tmp_path: Path) -> Path:
    for name, text in (("t1102", T1102), ("t0441", T0441), ("CFOAT00100", CFOAT), ("H1_", H1)):
        (tmp_path / f"{name}.res").write_bytes(text.encode("cp949"))
    return tmp_path


async def _session(res_dir: Path, fac: FakeFactory | None = None,
                   clock: Any = None) -> tuple[XingSession, FakeFactory]:
    fac = fac or FakeFactory()
    s = XingSession(fac, res_dir, clock=clock) if clock else XingSession(fac, res_dir)
    await s.start()
    return s, fac


async def test_login_runs_on_com_thread_and_returns_accounts(res_dir: Path) -> None:
    s, fac = await _session(res_dir)
    try:
        accts = await s.login("hts.example", 20001, "user", "pw", "certpw")
        assert accts == fac.accounts and s.logged_in
        assert fac.thread_ids and fac.thread_ids[0] != threading.get_ident()  # COM은 다른 스레드
        assert ("connect", "hts.example", 20001) in fac.calls
        assert ("login", "user", 0) in fac.calls
        # 로그인 거부·접속 실패는 XingError
        fac.login_result = ("2005", "비밀번호 오류")
        with pytest.raises(XingError, match="2005"):
            await s.login("h", 1, "u", "p", "c")
        fac.connect_fail = True
        with pytest.raises(XingError, match="접속 실패"):
            await s.login("h", 1, "u", "p", "c")
    finally:
        s.close()


async def test_query_fills_fields_and_reads_blocks_like_rest(res_dir: Path) -> None:
    s, fac = await _session(res_dir)
    try:
        fac.responses["t1102"] = ("00000", "정상",
                                  {"t1102OutBlock": {"hname": "삼성", "price": "70100"}})
        r = await s.query("t1102", {"t1102InBlock": {"shcode": "005930"}})
        assert isinstance(r, QueryResult) and r.rsp_cd == "00000"
        assert r.as_body() == {"t1102OutBlock": {"hname": "삼성", "price": "70100"},
                               "rsp_cd": "00000", "rsp_msg": "정상"}
        res_file, fields, nxt = fac.requests[0]
        assert res_file.endswith("t1102.res")
        assert fields == {("t1102InBlock", "shcode", 0): "005930"}
        assert nxt is False
        # occurs 블록은 list, 행 수는 GetBlockCount
        fac.responses["t0441"] = ("00000", "", {"t0441OutBlock1": [
            {"expcode": "A1167000", "jqty": "3"}, {"expcode": "A5067000", "jqty": "1"}]})
        r2 = await s.query("t0441", {"t0441InBlock": {"accno": "2014", "passwd": "x"}})
        assert r2.blocks["t0441OutBlock1"][1] == {"expcode": "A5067000", "jqty": "1"}
        # 거부: OnReceiveMessage만 오고 데이터 없음 → 본문에 코드·문구만
        fac.responses["t1102"] = ("01427", "가격범위", {})
        r3 = await s.query("t1102", {"t1102InBlock": {"shcode": "005930"}})
        assert r3.rsp_cd == "01427" and r3.blocks == {}
        # Request() 자체 실패
        fac.request_fail.add("t1102")
        with pytest.raises(XingError, match="요청 실패"):
            await s.query("t1102", {"t1102InBlock": {"shcode": "005930"}})
        # Res 없음
        with pytest.raises(XingError, match="Res 파일 없음"):
            await s.query("t9999", {})
    finally:
        s.close()


async def test_order_lane_is_separate_and_timeout_is_result_unknown(res_dir: Path) -> None:
    fake_now = [100.0]
    s, fac = await _session(res_dir, clock=lambda: fake_now[0])
    try:
        # 조회 차선은 응답이 안 오는 상태(t1102 응답 미정의) — 주문 차선은 따로 돌아야 한다
        pending = asyncio.ensure_future(
            s.query("t1102", {"t1102InBlock": {"shcode": "005930"}}, timeout_s=5.0))
        await asyncio.sleep(0.05)
        fac.responses["CFOAT00100"] = ("00039", "매도 주문이 완료되었습니다.",
                                      {"CFOAT00100OutBlock2": {"OrdNo": "605"}})
        r = await asyncio.wait_for(
            s.query("CFOAT00100",
                    {"CFOAT00100InBlock1": {"AcntNo": "2014", "FnoIsuNo": "A1167000"}},
                    lane=LANE_ORDER), 1.0)
        assert r.rsp_cd == "00039" and r.blocks["CFOAT00100OutBlock2"]["OrdNo"] == "605"
        assert len(fac.queries) == 2  # 차선마다 XAQuery 하나
        # 조회는 아직 대기 중 → 시계를 넘기면 시간 초과(XingTimeout)
        assert not pending.done()
        fake_now[0] = 106.0
        with pytest.raises(XingTimeout):
            await asyncio.wait_for(pending, 1.0)
        # 같은 차선의 다음 요청은 정상
        fac.responses["t1102"] = ("00000", "", {"t1102OutBlock": {"hname": "h", "price": "1"}})
        assert (await s.query("t1102", {"t1102InBlock": {"shcode": "1"}})).rsp_cd == "00000"
        assert await s.limits("t1102") == (10, 1, 200, 3)
        assert LANE_QUERY in s._queries
    finally:
        s.close()


async def test_realtime_advise_and_event_to_asyncio(res_dir: Path) -> None:
    s, fac = await _session(res_dir)
    got: list[tuple[str, str, dict[str, str]]] = []
    s.on_real.append(lambda tr, key, f: got.append((tr, key, f)))
    states: list[tuple[str, str, str]] = []
    s.on_session.append(lambda k, c, m: states.append((k, c, m)))
    try:
        await s.advise("H1_", "005930")
        await s.advise("H1_", "000660")
        await s.advise("H1_", "005930")  # 중복은 한 번만
        await s.advise("H1_", "U005930   ")  # Res 길이(6)보다 길면 잘라서 넣는다(CUR 8자리 키 대비)
        r = fac._last_real
        assert r.keys == [("H1_InBlock", "shcode", "005930"), ("H1_InBlock", "shcode", "000660"),
                          ("H1_InBlock", "shcode", "U00593")]
        assert r.advised == 3 and r.ResFileName.endswith("H1_.res")
        # COM 스레드에서 이벤트 → asyncio 콜백(필드 dict)
        r.fields = {"shcode": "005930", "bidho1": "70000"}
        s._call(lambda: r.sink("real", "H1_"))
        for _ in range(50):
            if got:
                break
            await asyncio.sleep(0.01)
        assert got == [("H1_", "", {"shcode": "005930", "bidho1": "70000"})]
        # 세션 끊김 이벤트
        s._call(lambda: fac.session.sink("disconnect"))
        for _ in range(50):
            if states:
                break
            await asyncio.sleep(0.01)
        assert states == [("disconnect", "", "")] and not s.logged_in
        await s.unadvise_all()
        assert r.advised == 0
    finally:
        s.close()
