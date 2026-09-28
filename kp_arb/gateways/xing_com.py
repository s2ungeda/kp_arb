"""xingAPI COM 세션 — 코어 안 STA 스레드 하나가 XASession/XAQuery/XAReal을 소유한다
(DESIGN-ls-xing.md §2.1).

- asyncio(메인 스레드) ↔ COM 스레드는 **명령 큐 + Future**로만 만난다. COM 객체는 COM 스레드 밖에서
  절대 만지지 않는다. COM 이벤트(OnReceiveData/OnReceiveRealData/OnLogin…)는 COM 스레드에서 필드를
  dict로 뽑아 ``loop.call_soon_threadsafe``로 넘긴다. **COM 스레드에서 네트워크·파일 I/O 금지.**
- 펌프: ``Waker``(실제 = win32event 이벤트 + MsgWaitForMultipleObjects)로 **명령이 들어오거나 윈도
  메시지가 오면 즉시** 깬다 — 예제식 "10ms 잠자기" 없음.
- COM 객체 생성·펌프·깨우기는 ``ComFactory``로 주입 — 테스트는 가짜(COM 없음), 라이브는
  ``Win32ComFactory``(pywin32, 32비트에서만 import).
- 조회 TR은 **차선(lane)**마다 XAQuery 하나로 한 번에 하나(직렬). 주문 TR은 "order" 차선을 따로 두어
  조회 뒤에 줄 서지 않는다.
- 실시간(XAReal)은 TR마다 객체 하나. 키 필드명은 Res InBlock 첫 필드.

라이브 검증 전 [OPEN]: 서버 호스트·포트, Res 경로, 실시간 TR의 OutBlock에 키(종목코드)가 있는지.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .xing_res import ResError, ResSpec, load_res

log = logging.getLogger("kp_arb.xing")

LOGIN_OK = "0000"
QUERY_TIMEOUT_S = 10.0   # 조회(REST와 같은 값)
ORDER_TIMEOUT_S = 30.0   # 주문(REST ORDER_REQUEST_TIMEOUT_S와 같은 값)
LANE_QUERY = "query"
LANE_ORDER = "order"


class XingError(RuntimeError):
    """xingAPI 호출 실패(로그인·요청 거부·시간 초과 등)."""


class XingTimeout(XingError):
    """응답 없음 — 주문이면 '결과 모름'(재전송 금지)."""


# --------------------------------------------------------------- 주입 계약 ---

class Waker(Protocol):
    """COM 스레드를 깨우는 손잡이 — set()은 아무 스레드, wait()는 COM 스레드에서."""

    def set(self) -> None: ...
    def wait(self, timeout_ms: int) -> None: ...


class ComFactory(Protocol):
    """COM 객체 생성·펌프. 이벤트는 만든 객체의 ``sink`` 속성(callable)으로 온다."""

    def init_thread(self) -> None: ...
    def uninit_thread(self) -> None: ...
    def make_waker(self) -> Waker: ...
    def pump(self) -> None: ...
    def create_session(self) -> Any: ...
    def create_query(self) -> Any: ...
    def create_real(self) -> Any: ...


# ---------------------------------------------------------- 결과 자료형 ---

@dataclass
class QueryResult:
    """TR 응답 — REST 응답 본문과 같은 모양으로 blocks를 채운다(occurs = list, 아니면 dict)."""

    tr: str
    rsp_cd: str = ""
    rsp_msg: str = ""
    blocks: dict[str, Any] = field(default_factory=dict)
    is_next: bool = False

    def as_body(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.blocks)
        out["rsp_cd"] = self.rsp_cd
        out["rsp_msg"] = self.rsp_msg
        return out


@dataclass
class _Pending:
    tr: str
    spec: ResSpec
    future: asyncio.Future[QueryResult]
    deadline: float
    rsp_cd: str = ""
    rsp_msg: str = ""


def is_ok_code(code: str) -> bool:
    """xing 메시지 코드 성공 판정 — REST rsp_cd와 같은 체계("00"으로 시작)."""
    return str(code).startswith("00")


# ------------------------------------------------------------------ 세션 ---

class XingSession:
    """COM 스레드 소유자. 공개 메서드는 전부 asyncio 스레드에서 부른다."""

    def __init__(
        self, factory: ComFactory, res_dir: Path | str, *,
        loop: asyncio.AbstractEventLoop | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._res_dir = Path(res_dir)
        self._loop = loop
        self._clock = clock
        self._cmds: queue.Queue[Callable[[], None]] = queue.Queue()
        self._waker: Waker | None = None
        self._thread: threading.Thread | None = None
        self._stop = False
        self._ready = threading.Event()
        # COM 스레드 전용 상태
        self._session: Any = None
        self._queries: dict[str, Any] = {}          # lane → XAQuery
        self._pending: dict[str, _Pending] = {}     # lane → 진행 중 요청
        self._reals: dict[str, Any] = {}            # tr → XAReal
        self._real_keys: dict[str, set[str]] = {}   # tr → advise된 키
        self._specs: dict[str, ResSpec] = {}
        self._login_future: asyncio.Future[tuple[str, str]] | None = None
        # asyncio 쪽 콜백
        self.on_real: list[Callable[[str, str, dict[str, str]], None]] = []  # (tr, key, fields)
        self.on_session: list[Callable[[str, str, str], None]] = []          # (state, code, msg)
        self.logged_in = False
        self.accounts: list[str] = []

    # --- 스레드 수명 ---

    async def start(self) -> None:
        self._loop = self._loop or asyncio.get_running_loop()
        self._waker = self._factory.make_waker()
        self._thread = threading.Thread(target=self._run, name="xing-com", daemon=True)
        self._thread.start()
        await asyncio.get_running_loop().run_in_executor(None, self._ready.wait)

    def close(self) -> None:
        self._stop = True
        if self._waker is not None:
            self._waker.set()

    def _run(self) -> None:
        self._factory.init_thread()
        try:
            self._session = self._factory.create_session()
            self._session.sink = self._on_session_event
            self._ready.set()
            assert self._waker is not None
            while not self._stop:
                self._waker.wait(50)
                self._factory.pump()
                self._drain()
                self._expire()
        finally:
            self._factory.uninit_thread()

    def _drain(self) -> None:
        while True:
            try:
                fn = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:  # noqa: BLE001 - 명령 하나의 오류로 COM 스레드를 죽이지 않음
                log.exception("xing COM 명령 실패")

    def _call(self, fn: Callable[[], None]) -> None:
        """COM 스레드에서 fn을 돈다(아무 스레드에서 호출)."""
        self._cmds.put(fn)
        assert self._waker is not None
        self._waker.set()

    def _resolve(self, fut: asyncio.Future[Any], value: Any = None,
                 exc: BaseException | None = None) -> None:
        """COM 스레드 → asyncio Future 완료."""
        assert self._loop is not None

        def _set() -> None:
            if fut.done():
                return
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(value)

        self._loop.call_soon_threadsafe(_set)

    def _emit(self, fn: Callable[..., None], *args: Any) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(fn, *args)

    # --- 로그인 ---

    async def login(self, host: str, port: int, user_id: str, password: str,
                    cert_password: str, server_type: int = 0,
                    timeout_s: float = 30.0) -> list[str]:
        """접속 + 로그인. OnLogin('0000')이면 계좌 목록을 돌려준다. 자격은 로그에 남기지 않는다."""
        assert self._loop is not None
        fut: asyncio.Future[tuple[str, str]] = self._loop.create_future()
        self._login_future = fut

        def _do() -> None:
            s = self._session
            if not s.ConnectServer(host, port):
                err = s.GetErrorMessage(s.GetLastError())
                self._resolve(fut, exc=XingError(f"xing 접속 실패 {host}:{port} — {err}"))
                return
            if not s.Login(user_id, password, cert_password, server_type, False):
                err = s.GetErrorMessage(s.GetLastError())
                self._resolve(fut, exc=XingError(f"xing 로그인 요청 실패 — {err}"))

        self._call(_do)
        try:
            code, msg = await asyncio.wait_for(fut, timeout_s)
        except TimeoutError as exc:
            raise XingTimeout("xing 로그인 응답 없음") from exc
        if code != LOGIN_OK:
            raise XingError(f"xing 로그인 거부 {code}: {msg}")
        self.logged_in = True
        self.accounts = await self._accounts()
        log.info("xing 로그인 성공 %s:%d 계좌 %d개", host, port, len(self.accounts))
        return list(self.accounts)

    async def _accounts(self) -> list[str]:
        assert self._loop is not None
        fut: asyncio.Future[list[str]] = self._loop.create_future()

        def _do() -> None:
            s = self._session
            n = int(s.GetAccountListCount())
            self._resolve(fut, [str(s.GetAccountList(i)) for i in range(n)])

        self._call(_do)
        return await fut

    def _on_session_event(self, kind: str, code: str = "", msg: str = "") -> None:
        """XASession 이벤트(COM 스레드): login/logout/disconnect."""
        if kind == "login" and self._login_future is not None:
            self._resolve(self._login_future, (code, msg))
            self._login_future = None
        if kind in ("logout", "disconnect"):
            self.logged_in = False
        for cb in self.on_session:
            self._emit(cb, kind, code, msg)

    # --- 조회·주문 TR ---

    def _spec(self, tr: str) -> ResSpec:
        spec = self._specs.get(tr)
        if spec is None:
            spec = load_res(self._res_dir, tr)  # 첫 사용 때 한 번(COM 스레드, 파일 읽기 1회)
            self._specs[tr] = spec
        return spec

    async def query(self, tr: str, blocks: dict[str, Any], *, lane: str = LANE_QUERY,
                    next_: bool = False, timeout_s: float | None = None) -> QueryResult:
        """TR 요청. blocks = {"<tr>InBlock": {필드: 값} | [{…}, …]} (REST 본문과 같은 모양).

        같은 차선의 앞 요청이 끝날 때까지 기다린다(직렬). 시간 초과는 XingTimeout — 주문 TR이면
        호출자가 '결과 모름'으로 다룬다(재전송 금지).
        """
        assert self._loop is not None
        if timeout_s is None:
            timeout_s = ORDER_TIMEOUT_S if lane == LANE_ORDER else QUERY_TIMEOUT_S
        fut: asyncio.Future[QueryResult] = self._loop.create_future()
        deadline = self._clock() + timeout_s

        def _do() -> None:
            if lane in self._pending:
                # 앞 요청 진행 중 — 뒤로 다시 줄 세운다(펌프 한 바퀴 뒤)
                self._cmds.put(_do)
                return
            try:
                spec = self._spec(tr)
            except ResError as exc:
                self._resolve(fut, exc=XingError(str(exc)))
                return
            q = self._queries.get(lane)
            if q is None:
                q = self._factory.create_query()
                q.sink = lambda kind, *a, ln=lane: self._on_query_event(ln, kind, *a)
                self._queries[lane] = q
            q.ResFileName = str(self._res_dir / f"{tr}.res")
            for name, rows in blocks.items():
                rows_list = rows if isinstance(rows, list) else [rows]
                block = spec.blocks.get(name)
                if block is not None and block.occurs:
                    q.SetBlockCount(name, len(rows_list))
                for i, row in enumerate(rows_list):
                    for fname, value in row.items():
                        q.SetFieldData(name, fname, i, "" if value is None else str(value))
            self._pending[lane] = _Pending(tr, spec, fut, deadline)
            rc = int(q.Request(next_))
            if rc < 0:
                del self._pending[lane]
                self._resolve(fut, exc=XingError(f"{tr} 요청 실패 {rc}: {q.GetErrorMessage(rc)}"))

        self._call(_do)
        return await fut

    def _on_query_event(self, lane: str, kind: str, *args: Any) -> None:
        """XAQuery 이벤트(COM 스레드): ('message', sys_err, code, msg) / ('data', tr)."""
        p = self._pending.get(lane)
        if p is None:
            return
        if kind == "message":
            sys_err, code, msg = bool(args[0]), str(args[1]), str(args[2])
            p.rsp_cd, p.rsp_msg = code, msg
            if sys_err or not is_ok_code(code):
                # 거부 — 데이터는 안 온다. 본문에 코드·문구만 실어 호출자가 거부로 다룬다
                del self._pending[lane]
                self._resolve(p.future, QueryResult(p.tr, code, msg))
            return
        if kind == "data":
            q = self._queries[lane]
            result = QueryResult(p.tr, p.rsp_cd or "00000", p.rsp_msg,
                                 self._read_blocks(q, p.spec), bool(getattr(q, "IsNext", False)))
            del self._pending[lane]
            self._resolve(p.future, result)

    @staticmethod
    def _read_blocks(q: Any, spec: ResSpec) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for block in spec.out_blocks:
            if block.occurs:
                n = int(q.GetBlockCount(block.name))
                out[block.name] = [
                    {f: str(q.GetFieldData(block.name, f, i)) for f in block.fields}
                    for i in range(n)]
            else:
                out[block.name] = {f: str(q.GetFieldData(block.name, f, 0)) for f in block.fields}
        return out

    def _expire(self) -> None:
        now = self._clock()
        for lane, p in list(self._pending.items()):
            if now >= p.deadline:
                del self._pending[lane]
                self._resolve(p.future, exc=XingTimeout(f"{p.tr} 응답 없음(시간 초과)"))

    async def limits(self, tr: str) -> tuple[int, int, int, int]:
        """xing이 알려 주는 TR 한도 — (초당 건수, 기준 초, 한도, 현재 요청 수)."""
        assert self._loop is not None
        fut: asyncio.Future[tuple[int, int, int, int]] = self._loop.create_future()

        def _do() -> None:
            q = self._queries.get(LANE_QUERY)
            if q is None:
                q = self._factory.create_query()
                q.sink = lambda kind, *a: self._on_query_event(LANE_QUERY, kind, *a)
                self._queries[LANE_QUERY] = q
            self._resolve(fut, (int(q.GetTRCountPerSec(tr)), int(q.GetTRCountBaseSec(tr)),
                                int(q.GetTRCountLimit(tr)), int(q.GetTRCountRequest(tr))))

        self._call(_do)
        return await fut

    # --- 실시간 ---

    async def advise(self, tr: str, key: str) -> None:
        """실시간 등록. key=""는 계좌 통보처럼 키 없는 TR."""
        assert self._loop is not None
        fut: asyncio.Future[None] = self._loop.create_future()

        def _do() -> None:
            try:
                spec = self._spec(tr)
            except ResError as exc:
                self._resolve(fut, exc=XingError(str(exc)))
                return
            r = self._reals.get(tr)
            if r is None:
                r = self._factory.create_real()
                r.ResFileName = str(self._res_dir / f"{tr}.res")
                r.sink = lambda kind, *a, t=tr: self._on_real_event(t, kind, *a)
                self._reals[tr] = r
                self._real_keys[tr] = set()
            if key in self._real_keys[tr]:
                self._resolve(fut, None)
                return
            key_field = spec.key_field()
            if key and key_field and spec.in_blocks:
                # 키를 Res 필드 길이에 맞춘다(실측 2026-09-28: CUR base_id 6자리 — WS 규격 8자리 키
                # "USD     "를 그대로 넣지 않게). 길이를 모르면 그대로
                width = spec.in_blocks[0].length_of(key_field)
                r.SetFieldData(spec.in_blocks[0].name, key_field, key[:width] if width else key)
            r.AdviseRealData()
            self._real_keys[tr].add(key)
            self._resolve(fut, None)

        self._call(_do)
        await fut

    async def unadvise_all(self) -> None:
        assert self._loop is not None
        fut: asyncio.Future[None] = self._loop.create_future()

        def _do() -> None:
            for r in self._reals.values():
                r.UnadviseRealData()
            for keys in self._real_keys.values():
                keys.clear()
            self._resolve(fut, None)

        self._call(_do)
        await fut

    def _on_real_event(self, tr: str, kind: str, *args: Any) -> None:
        """XAReal 이벤트(COM 스레드): ('real', tr) → OutBlock 필드 dict를 asyncio로."""
        if kind != "real":
            return
        r = self._reals.get(tr)
        spec = self._specs.get(tr)
        if r is None or spec is None:
            return
        fields: dict[str, str] = {}
        for block in spec.out_blocks:
            for f in block.fields:
                fields[f] = str(r.GetFieldData(block.name, f))
        for cb in self.on_real:
            self._emit(cb, tr, "", fields)


# ------------------------------------------------------- pywin32 구현 ---

def register_install_dir(install_dir: Path | str) -> None:
    """xingAPI 설치 폴더를 이 프로세스의 DLL 탐색 경로에 넣는다(SetDllDirectory + PATH 앞).

    실서버 로그인은 XA_Session.dll이 설치 폴더의 공동인증 모듈(inisafenet·inipki·XecureS 등)을
    **이름으로** LoadLibrary 하는데, 그 탐색 순서엔 실행 파일 폴더·시스템·현재 폴더·PATH만 있다.
    DevCenter는 설치 폴더에서 실행돼 찾지만 코어(dist\\meme 또는 .venv32)는 못 찾아 로그인 거부
    2006 "공동인증 모듈 초기화에 실패"(운영 PC 실측 2026-09-28 17:25). 모의 서버는 인증 모듈을
    안 써서 개발 PC에선 드러나지 않았다. 한 번만 부르면 되고 두 번 불러도 무해(PATH 중복 없음).
    """
    import os

    path = str(Path(install_dir))
    if os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(path)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if path not in parts:
        os.environ["PATH"] = os.pathsep.join([path, *parts]) if parts != [""] else path


class Win32ComFactory:
    """실제 xingAPI COM(pywin32). 32비트 파이썬에서만 import된다.

    install_dir(xingAPI 설치 폴더)을 주면 COM 객체를 만들기 전에 DLL 탐색 경로에 넣는다
    (`register_install_dir` — 실서버 공동인증 모듈 로드용)."""

    PROG_SESSION = "XA_Session.XASession"
    PROG_QUERY = "XA_DataSet.XAQuery"
    PROG_REAL = "XA_DataSet.XAReal"

    def __init__(self, install_dir: Path | str | None = None) -> None:
        if install_dir is not None:
            register_install_dir(install_dir)

    def init_thread(self) -> None:
        import pythoncom

        pythoncom.CoInitialize()

    def uninit_thread(self) -> None:
        import pythoncom

        pythoncom.CoUninitialize()

    def make_waker(self) -> Waker:
        return _Win32Waker()

    def pump(self) -> None:
        import pythoncom

        pythoncom.PumpWaitingMessages()

    def create_session(self) -> Any:
        import win32com.client

        return win32com.client.DispatchWithEvents(self.PROG_SESSION, _SessionEvents)

    def create_query(self) -> Any:
        import win32com.client

        return win32com.client.DispatchWithEvents(self.PROG_QUERY, _QueryEvents)

    def create_real(self) -> Any:
        import win32com.client

        return win32com.client.DispatchWithEvents(self.PROG_REAL, _RealEvents)


class _Win32Waker:
    def __init__(self) -> None:
        import win32event

        self._evt = win32event.CreateEvent(None, False, False, None)

    def set(self) -> None:
        import win32event

        win32event.SetEvent(self._evt)

    def wait(self, timeout_ms: int) -> None:
        import win32event

        # 명령 이벤트 또는 윈도 메시지(COM 콜백) 중 먼저 오는 것에 즉시 깬다
        win32event.MsgWaitForMultipleObjects([self._evt], False, timeout_ms,
                                             win32event.QS_ALLINPUT)


class _SessionEvents:
    sink: Any = None

    def OnLogin(self, code: str, msg: str) -> None:  # noqa: N802 - COM 이벤트 이름
        if self.sink:
            self.sink("login", str(code), str(msg))

    def OnLogout(self) -> None:  # noqa: N802
        if self.sink:
            self.sink("logout")

    def OnDisconnect(self) -> None:  # noqa: N802
        if self.sink:
            self.sink("disconnect")


class _QueryEvents:
    sink: Any = None

    def OnReceiveData(self, tr: str) -> None:  # noqa: N802
        if self.sink:
            self.sink("data", str(tr))

    def OnReceiveMessage(self, sys_err: bool, code: str, msg: str) -> None:  # noqa: N802
        if self.sink:
            self.sink("message", bool(sys_err), str(code), str(msg))


class _RealEvents:
    sink: Any = None

    def OnReceiveRealData(self, tr: str) -> None:  # noqa: N802
        if self.sink:
            self.sink("real", str(tr))
