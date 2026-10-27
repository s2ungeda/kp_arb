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
import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .xing_res import ResError, ResSpec, load_res

log = logging.getLogger("kp_arb.xing")

LOGIN_OK = "0000"
QUERY_TIMEOUT_S = 10.0   # 조회(REST와 같은 값)
ORDER_TIMEOUT_S = 30.0   # 주문(REST ORDER_REQUEST_TIMEOUT_S와 같은 값)
STUCK_GRACE_S = 5.0      # COM 스레드가 멈춰 _expire가 안 돌 때 asyncio 쪽 2차 시간 초과 여유
THREAD_STALL_S = 5.0     # COM 스레드 심장박동이 이만큼 끊기면 '정지'로 본다(실시간 상태 끊김 표시)
# COM 스레드 한 바퀴(펌프+명령) 진단(사용자 2026-10-23 "스레드가 왜 멈춘 거지") — 운영 10-23
# 09:08:52~09:09:02 심장박동이 10초 끊겼는데 그 사이 체결 통보는 처리됐다 = 죽은 게 아니라 한
# 바퀴 안에서 밀린 메시지를 처리하던 것으로 보임. 바퀴가 이만큼 넘게 걸리면 무엇을 처리했는지
# 한 줄 남긴다.
# 실시간 TR 중 **읽는 칸을 제한**하는 것(DESIGN-ls-xing §9, 2026-10-23) — 전략은 1호가만 쓰므로
# 호가 TR(주식 H1_/NH1 52~53칸, 주식선물 JH0 68칸)은 코드·시각·매도1·매수1·잔량 둘만 읽는다(COM 호출
# 52~68 → 6~7). 없는 TR은 전부 읽는다.
_LEVEL1 = frozenset({"shcode", "ex_shcode", "futcode", "hotime", "offerho1", "bidho1",
                     "offerrem1", "bidrem1"})
REAL_FIELD_LIMIT: dict[str, frozenset[str]] = {"H1_": _LEVEL1, "NH1": _LEVEL1, "JH0": _LEVEL1}
# 통합 호가 UH1(2026-10-27 오후, §9 "주식 호가는 UH1 하나"): 단계마다 가격 둘 + 잔량 여섯
# (KRX·NXT·통합 × 매도·매수)을 KRX·NXT 매도·매수 1호가가 다 나올 때까지만 읽는다(최대 10단계,
# 가격 0이면 끝).
UNIFIED_QUOTE_TR = "UH1"
UNIFIED_HEAD_FIELDS = ("ex_shcode", "shcode", "hotime")
UNIFIED_LEVEL_FIELDS = ("offerho{i}", "bidho{i}", "krx_offerrem{i}", "nxt_offerrem{i}",
                        "unt_offerrem{i}", "krx_bidrem{i}", "nxt_bidrem{i}", "unt_bidrem{i}")
UNIFIED_MAX_LEVELS = 10


def _positive(raw: str | None) -> bool:
    try:
        return float(raw or 0) > 0
    except (TypeError, ValueError):
        return False


def read_unified_quote(get: Callable[[str], str], have: Collection[str],
                       *, levels: int = UNIFIED_MAX_LEVELS) -> dict[str, str]:
    """UH1 칸을 필요한 만큼만 읽는다(순수 — get은 칸 하나 읽기, have는 Res의 칸 이름들).

    머리 칸(코드·시각) 뒤 단계별 8칸을 읽다가 KRX·NXT의 매도·매수 1호가(그 거래소 잔량 > 0인 첫
    단계)가 넷 다 나오면 멈춘다. 가격이 둘 다 0인 단계(빈 단계)면 끝. 보통 1~2단계 = 11~19칸."""
    out: dict[str, str] = {}
    for f in UNIFIED_HEAD_FIELDS:
        if f in have:
            out[f] = get(f)
    need = {"krx_offerrem", "nxt_offerrem", "krx_bidrem", "nxt_bidrem"}
    for i in range(1, levels + 1):
        names = [n.format(i=i) for n in UNIFIED_LEVEL_FIELDS]
        vals = {n: get(n) for n in names if n in have}
        out.update(vals)
        if not _positive(vals.get(f"offerho{i}")) and not _positive(vals.get(f"bidho{i}")):
            break
        for key in list(need):
            if _positive(vals.get(f"{key}{i}")):
                need.discard(key)
        if not need:
            break
    return out
SLOW_ITER_S = 1.0        # 한 바퀴가 이보다 길면 경고
SLOW_ITER_LOG_GAP_S = 5.0  # 경고는 이 간격으로만(느린 바퀴가 이어질 때 로그 폭주 방지)
# 시세 큐 카운트 진단(사용자 2026-10-27, DESIGN-ls-xing §9) — 바퀴당 처리 이벤트 수(윈도 메시지
# 큐에 밀린 양의 간접 지표)와 COM→asyncio 인계 대기 건수(넘김 − 처리)가 이만큼이면 같은 경고 줄.
# 운영 실측 10-27 장 시작 분당 7,500건 = 바퀴당 6건이라 넉넉히 둔 값 — 환경변수로 조정.
ITER_EVENTS_N = 100
BACKLOG_N = 200
ITER_EVENTS_ENV = "KP_XING_ITER_EVENTS_N"
BACKLOG_ENV = "KP_XING_BACKLOG_N"
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
    # 요청 순번·그 요청의 XAQuery(검토 2026-10-13 ★7 (b)): 요청마다 객체를 새로 만들고 이벤트를
    # 순번으로 귀속시켜, 시간 초과로 정리한 앞 요청의 늦은 응답이 같은 차선의 다음 요청 결과가
    # 되지 않게 한다
    seq: int = 0
    query: Any = None
    rsp_cd: str = ""
    rsp_msg: str = ""


def is_ok_code(code: str) -> bool:
    """xing 메시지 코드 성공 판정 — REST rsp_cd와 같은 체계("00"으로 시작)."""
    return str(code).startswith("00")


# ------------------------------------------------------------------ 세션 ---

def slow_iteration_text(total_s: float, pump_s: float, counts: dict[str, int],
                        drain_s: float, drain_n: int, pending: int, backlog: int = 0,
                        top: int = 6) -> str:
    """느린 바퀴 경고 글 — 전체·펌프·명령 처리 시간, 펌프에서 처리한 이벤트 수(종류별 많은 순
    top개), 이번 바퀴 명령 수, 남은 명령 수, COM→asyncio 인계 대기 시세 건수. 순수."""
    n = sum(counts.values())
    kinds = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:top])
    return (f"xing COM 스레드 한 바퀴 {total_s:.2f}s — 펌프 {pump_s:.2f}s 이벤트 {n}건"
            f"({kinds or '없음'}) 명령 {drain_s:.2f}s {drain_n}개, 남은 명령 {pending}개, "
            f"인계 대기 {backlog}건")


def iteration_needs_log(total_s: float, n_events: int, backlog: int, *,
                        slow_s: float = SLOW_ITER_S, events_n: int = ITER_EVENTS_N,
                        backlog_n: int = BACKLOG_N) -> bool:
    """한 바퀴를 로그에 남길 조건(DESIGN-ls-xing §9 시세 지연 진단) — 시간·바퀴당 이벤트·인계
    대기 중 하나라도 문턱 이상. 순수."""
    return total_s >= slow_s or n_events >= events_n or backlog >= backlog_n


def _env_int(name: str, default: int) -> int:
    """환경변수 정수(없거나 못 읽으면 기본값)."""
    import os

    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


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
        self._queries: dict[str, Any] = {}          # lane → 마지막 XAQuery(한도 조회용)
        self._pending: dict[str, _Pending] = {}     # lane → 진행 중 요청
        self._seq = itertools.count(1)              # 요청 순번(★7 (b))
        self._reals: dict[str, Any] = {}            # tr → XAReal
        self.last_beat = 0.0  # COM 스레드 루프가 마지막으로 돈 시각(_clock) — 정지 감시용
        self._iter_counts: dict[str, int] = {}      # 이번 바퀴 펌프에서 처리한 이벤트 수(종류별)
        self._last_slow_log = float("-inf")         # 느린 바퀴 경고를 마지막으로 남긴 시각
        # 시세 인계 카운터(§9 진단) — _real_sent는 COM 스레드만, _real_done은 asyncio 루프만 올린다
        # (각자 한 쪽만 쓰니 잠금 없이 안전). 차 = 루프가 아직 못 본 시세 건수.
        self._real_sent = 0
        self._real_done = 0
        self._iter_events_n = _env_int(ITER_EVENTS_ENV, ITER_EVENTS_N)
        self._backlog_n = _env_int(BACKLOG_ENV, BACKLOG_N)
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
                t0 = self.last_beat = self._clock()  # 심장박동 — 멈추면 xing_ws 감시가 '끊김'으로
                self._iter_counts.clear()
                try:
                    self._factory.pump()
                    self._expire()
                except Exception:  # noqa: BLE001 - 펌프 한 번의 오류로 COM 스레드가 조용히 죽지 않게
                    log.exception("xing COM 펌프 오류 — 계속")
                t1 = self._clock()
                drain_n = self._cmds.qsize()
                self._drain()
                self._note_iteration(t0, t1, self._clock(), drain_n)
        finally:
            # 여기 오면 실시간·조회가 전부 멈춘다 — 조용히 끝나지 않게 반드시 남긴다
            log.warning("xing COM 스레드 종료(stop=%s)", self._stop)
            self._factory.uninit_thread()

    @property
    def real_backlog(self) -> int:
        """COM 스레드가 넘겼지만 asyncio 루프가 아직 처리하지 않은 시세 건수."""
        return self._real_sent - self._real_done

    def _note_iteration(self, t0: float, t1: float, t2: float, drain_n: int) -> None:
        """한 바퀴 진단 — 오래 걸렸거나(SLOW_ITER_S) 바퀴당 이벤트·인계 대기가 문턱 이상이면
        무엇을 처리했는지 경고 한 줄(간격 제한). DESIGN-ls-xing §9 시세 지연 진단."""
        total = t2 - t0
        backlog = self.real_backlog
        if not iteration_needs_log(total, sum(self._iter_counts.values()), backlog,
                                   events_n=self._iter_events_n, backlog_n=self._backlog_n):
            return
        if t2 - self._last_slow_log < SLOW_ITER_LOG_GAP_S:
            return
        self._last_slow_log = t2
        log.warning(slow_iteration_text(total, t1 - t0, dict(self._iter_counts), t2 - t1,
                                        drain_n, self._cmds.qsize(), backlog))

    def _drain(self) -> None:
        """이번 바퀴에 **지금 들어 있는 명령만** 처리한다(qsize 스냅샷).

        옛 `while True`는 큐가 빌 때까지 돌았는데, 같은 차선에 앞 요청이 진행 중이면 뒤 요청(_do)이
        "다음 바퀴에"라며 자기를 큐에 다시 넣고 → 이 루프가 그걸 즉시 다시 꺼내 → **무한 회전**.
        펌프·시간 초과가 영영 안 돌아 응답도 실시간도 멈추고 세션은 '연결'로 보였다(운영 PC 실측
        2026-09-29 14:55:26 — HL선 두 세트의 LS 후주문이 같은 순간 주문 차선에 들어옴 → LS 전부
        정지, HL만 살아 헤지 없는 HL 체결 누적). 모의 시험은 전부 순차라 걸리지 않았다.
        """
        for _ in range(self._cmds.qsize()):
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
            # 서버 쪽 실시간 등록은 세션과 함께 죽는다 — 등록 키를 비워 재로그인 뒤 _advise_all이
            # 실제로 AdviseRealData를 다시 보내게 한다(검토 2026-10-13 ★5: 전엔 키가 남아 전부 "이미
            # 등록"으로 건너뛰어 재로그인 뒤 시세·계좌 통보가 끊긴 채였다). XAReal 객체는 그대로
            # 쓴다
            n = sum(len(k) for k in self._real_keys.values())
            if n:
                log.warning("xing 세션 %s — 실시간 등록 %d건 비움(재로그인 뒤 다시 등록)", kind, n)
            for tr in self._real_keys:
                self._real_keys[tr] = set()
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
        seq = next(self._seq)

        def _do() -> None:
            # 줄 서는 동안 호출자가 포기했거나(asyncio 쪽 시간 초과) 마감이 지났으면 보내지 않는다
            # (검토 2026-10-13 ★7 (a): COM 정지 회복 때 "결과 모름"으로 끝난 주문들이 옛 가격으로
            # 한꺼번에 나가던 길)
            if fut.done():
                log.warning("xing %s 요청 — 줄 서는 동안 호출자가 포기함, 전송 안 함", tr)
                return
            if self._clock() >= deadline:
                self._resolve(fut, exc=XingTimeout(f"{tr} 앞 요청에 막혀 마감 지남 — 전송 안 함"))
                return
            if lane in self._pending:
                # 앞 요청 진행 중 — 뒤로 다시 줄 세운다(펌프 한 바퀴 뒤)
                self._cmds.put(_do)
                return
            try:
                spec = self._spec(tr)
            except ResError as exc:
                self._resolve(fut, exc=XingError(str(exc)))
                return
            # 요청마다 XAQuery를 새로 — 이벤트가 그 요청의 순번으로 온다(★7 (b))
            q = self._factory.create_query()
            q.sink = lambda kind, *a, ln=lane, sq=seq: self._on_query_event(ln, sq, kind, *a)
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
            self._pending[lane] = _Pending(tr, spec, fut, deadline, seq=seq, query=q)
            rc = int(q.Request(next_))
            if rc < 0:
                del self._pending[lane]
                self._resolve(fut, exc=XingError(f"{tr} 요청 실패 {rc}: {q.GetErrorMessage(rc)}"))

        self._call(_do)
        # 시간 초과는 COM 스레드의 _expire가 처리하지만, 그 스레드 자체가 멈추면(2026-09-29 운영
        # 실측) 영원히 기다리게 된다 → asyncio 쪽에서도 여유를 두고 한 번 더 건다(결과 모름).
        limit = timeout_s + STUCK_GRACE_S
        try:
            return await asyncio.wait_for(fut, limit)
        except TimeoutError as exc:
            # _pending은 COM 스레드 전용 — 여기(asyncio)서 직접 pop하지 않고 그 스레드에 맡긴다
            # (검토 ★7 (c)). 스레드가 멈춰 있으면 회복 뒤 _expire가 같은 요청을 정리한다
            self._call(lambda: self._abandon(lane, seq))
            raise XingTimeout(f"{tr} 응답 없음(COM 스레드 무응답 {limit:.0f}초)") from exc

    def _abandon(self, lane: str, seq: int) -> None:
        """호출자가 포기한 요청을 차선에서 뺀다(COM 스레드). 다른 요청이면 건드리지 않는다."""
        p = self._pending.get(lane)
        if p is not None and p.seq == seq:
            del self._pending[lane]

    def _on_query_event(self, lane: str, seq: int, kind: str, *args: Any) -> None:
        """XAQuery 이벤트(COM 스레드): ('message', sys_err, code, msg) / ('data', tr).
        seq = 이벤트를 보낸 XAQuery의 요청 순번 — 지금 진행 중인 요청과 다르면(시간 초과로 정리한 앞
        요청의 늦은 응답) 버린다(★7 (b)). 'data'는 TR 이름까지 대조."""
        key = f"{lane}응답"
        self._iter_counts[key] = self._iter_counts.get(key, 0) + 1
        p = self._pending.get(lane)
        if p is None or p.seq != seq:
            log.warning("xing 늦은 응답 무시 — 차선 %s %s 순번 %s(진행 중 %s) %s", lane, kind, seq,
                        p.seq if p is not None else "없음",
                        "".join(str(a) for a in args[:3]) if kind == "message" else "")
            return
        if kind == "data" and args and str(args[0]).strip() and str(args[0]).strip() != p.tr:
            log.warning("xing 응답 TR 불일치 — 차선 %s 기대 %s 받음 %s, 무시", lane, p.tr, args[0])
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
            q = p.query if p.query is not None else self._queries[lane]
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

    def thread_stalled(self, max_gap_s: float = THREAD_STALL_S) -> bool:
        """COM 스레드 루프가 max_gap_s 넘게 안 돌았나(아무 스레드에서 호출). 시작 전(0)은 False.
        운영 실측 2026-09-29: 스레드가 멈춰도 세션은 '연결'로 보여 감지 못 했다."""
        return self.last_beat > 0 and (self._clock() - self.last_beat) > max_gap_s

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
                q.sink = lambda kind, *a: self._on_query_event(LANE_QUERY, 0, kind, *a)
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

    async def unadvise(self, tr: str, key: str) -> None:
        """실시간 해제(키 하나) — 종목·월물 구독 계획이 바뀔 때(DESIGN-ls-xing §9). 등록된 적 없는
        키는 아무것도 안 한다. key=""(계좌 통보처럼 키 없는 TR)는 그 TR 전체 해제."""
        assert self._loop is not None
        fut: asyncio.Future[None] = self._loop.create_future()

        def _do() -> None:
            r = self._reals.get(tr)
            keys = self._real_keys.get(tr)
            if r is None or keys is None or key not in keys:
                self._resolve(fut, None)
                return
            try:
                if key:
                    spec = self._specs.get(tr)
                    width = (spec.in_blocks[0].length_of(spec.key_field() or "")
                             if spec is not None and spec.in_blocks and spec.key_field() else 0)
                    r.UnadviseRealDataWithKey(key[:width] if width else key)
                else:
                    r.UnadviseRealData()
            except Exception as exc:  # noqa: BLE001 - COM 오류 → 호출자에 전달
                self._resolve(fut, exc=XingError(f"{tr}/{key!r} 해제 실패: {exc}"))
                return
            keys.discard(key)
            self._resolve(fut, None)

        self._call(_do)
        await fut

    def _on_real_event(self, tr: str, kind: str, *args: Any) -> None:
        """XAReal 이벤트(COM 스레드): ('real', tr) → OutBlock 필드 dict를 asyncio로.

        REAL_FIELD_LIMIT에 있는 TR은 그 칸만 읽는다(GetFieldData가 COM 호출 하나씩이라 JH0 68칸을
        다 읽으면 바퀴가 늦다 — §9 "호가는 1호가만")."""
        if kind != "real":
            return
        self._iter_counts[tr] = self._iter_counts.get(tr, 0) + 1
        r = self._reals.get(tr)
        spec = self._specs.get(tr)
        if r is None or spec is None:
            return
        keep = REAL_FIELD_LIMIT.get(tr)
        fields: dict[str, str] = {}
        for block in spec.out_blocks:
            if tr == UNIFIED_QUOTE_TR:
                def get(f: str, _name: str = block.name) -> str:
                    return str(r.GetFieldData(_name, f))

                fields.update(read_unified_quote(get, block.fields))
                continue
            for f in block.fields:
                if keep is None or f in keep:
                    fields[f] = str(r.GetFieldData(block.name, f))
        self._real_sent += 1  # COM 스레드만 올린다
        self._emit(self._deliver_real, tr, fields)

    def _deliver_real(self, tr: str, fields: dict[str, str]) -> None:
        """asyncio 루프에서 시세 1건을 콜백들에 전달 — 처리 카운터는 루프만 올린다."""
        self._real_done += 1
        for cb in self.on_real:
            cb(tr, "", fields)


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
