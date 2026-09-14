"""파일 로그 설정 — 도구별로 logs/<이름>_YYYYMMDD.log 에 남긴다.

코어의 파일 로그는 **큐 핸들러**(``QueuedHandler``)를 거친다: 호출 스레드(asyncio 이벤트 루프)는
레코드를 큐에 넣고 바로 돌아오고, 파일 쓰기는 별도 스레드가 한다. 동기 파일 쓰기는 윈도우에서
한 줄에 0.2~1ms라 체결→후주문 경로(줄 4~6개)에 그대로 얹혔다(운영 실측 2026-09-14: 선체결 수신→
HL 발주요청 5~22ms). 시각(asctime)은 레코드 생성 시각이라 큐를 거쳐도 그대로다.
"""
from __future__ import annotations

import atexit
import copy
import logging
import queue
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path

_LISTENERS: list[QueueListener] = []


class QueuedHandler(QueueHandler):
    """``target`` 핸들러의 파일 쓰기를 별도 스레드로 미룬다(같은 프로세스 큐).

    표준 QueueHandler.prepare는 큐에 넣기 전에 레코드를 **미리 포맷해 msg를 바꾼다**(다른 프로세스로
    pickle하려는 설계). 그러면 target의 포맷터가 한 번 더 붙어 시각·레벨 접두가 두 번 찍힌다. 여기는
    같은 프로세스라 pickle이 필요 없으니 메시지만 합치고(args 참조 해제) 포맷·예외 출력은 target에
    맡긴다. 포맷터는 **target에** 단다(이 핸들러에 단 포맷터는 무시).
    """

    def __init__(self, target: logging.Handler) -> None:
        self._q: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
        super().__init__(self._q)
        self.target = target
        self._listener = QueueListener(self._q, target, respect_handler_level=True)
        self._listener.start()
        _LISTENERS.append(self._listener)

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        prepared = copy.copy(record)
        prepared.msg = record.getMessage()
        prepared.args = None
        return prepared

    def flush(self) -> None:
        """큐가 비고 target이 다 쓸 때까지(최대 1초) 기다린다 — 테스트·종료 직전 확인용."""
        import time as _t

        deadline = _t.monotonic() + 1.0
        while not self._q.empty() and _t.monotonic() < deadline:
            _t.sleep(0.005)
        _t.sleep(0.01)  # 방금 꺼낸 레코드가 target.emit을 지나는 시간
        self.target.flush()

    def close(self) -> None:
        _stop_listener(self._listener)
        self.target.close()
        super().close()


def _stop_listener(listener: QueueListener) -> None:
    if listener._thread is not None:  # stop()을 두 번 부르면 join(None)으로 터진다
        listener.stop()
    if listener in _LISTENERS:
        _LISTENERS.remove(listener)


def stop_queued_logging() -> None:
    """큐에 남은 레코드를 전부 파일에 쓰고 쓰기 스레드를 멈춘다 — 종료 직전·atexit."""
    for listener in list(_LISTENERS):
        _stop_listener(listener)


atexit.register(stop_queued_logging)


def has_file_handler(logger: logging.Logger, kind: type[logging.Handler]) -> bool:
    """로거에 ``kind`` 핸들러가(큐 뒤에 있든 직접이든) 이미 붙어 있는가 — 중복 부착 방지용."""
    for h in logger.handlers:
        if isinstance(h, kind) or (isinstance(h, QueuedHandler) and isinstance(h.target, kind)):
            return True
    return False


class DailyFileHandler(logging.FileHandler):
    """자정에 파일을 바꾸는 로그 핸들러 — 항상 ``logs/<prefix>_<오늘>.log`` 에 쓴다.

    표준 TimedRotatingFileHandler는 활성 파일이 날짜 없는 이름이고 회전분에만 날짜가 붙어
    '파일 이름=당일 날짜' 요구와 반대다. 그래서 기록할 때 날짜가 바뀌면 스스로 오늘 날짜
    파일로 갈아탄다 — 24시간 무중단이라 시작 시각 날짜에 고정되면 안 됨(Phase 8).
    """

    def __init__(self, log_dir: Path, prefix: str = "core") -> None:
        self._dir = log_dir
        self._prefix = prefix
        self._day = self._today()
        super().__init__(self._path(self._day), encoding="utf-8")

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y%m%d")

    def _path(self, day: str) -> str:
        return str((self._dir / f"{self._prefix}_{day}.log").resolve())

    def emit(self, record: logging.LogRecord) -> None:
        day = self._today()
        if day != self._day:  # 자정 넘김 → 오늘 파일로 갈아탄다
            self._day = day
            self.baseFilename = self._path(day)
            if self.stream is not None:
                self.stream.close()
            self.stream = self._open()
        super().emit(record)


def attach_daily_file(
    logger_name: str, prefix: str, log_dir: Path | None,
    fmt: str = "%(asctime)s %(levelname)s %(message)s",
) -> logging.Logger:
    """이름의 로거에 날짜별 파일(<log_dir>/<prefix>_YYYYMMDD.log)을 1회만 붙여 돌려준다.

    root로 전파하지 않아 코어 로그·콘솔과 섞이지 않는다. log_dir이 None이면(테스트 등)
    파일 없이 조용한 로거. 자동M 종목별 로그(autom_<종목>_날짜.log) 등에 쓴다.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if has_file_handler(logger, DailyFileHandler) or has_file_handler(logger, logging.NullHandler):
        return logger
    if log_dir is None:
        logger.addHandler(logging.NullHandler())
        return logger
    try:
        log_dir.mkdir(exist_ok=True)
        handler = DailyFileHandler(log_dir, prefix=prefix)
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(QueuedHandler(handler))  # 파일 쓰기는 별도 스레드(모듈 머리말)
    except OSError:
        logger.addHandler(logging.NullHandler())
    return logger


def setup_logging(name: str, *, level: int = logging.INFO) -> logging.Logger:
    """콘솔 + 날짜별 파일(logs/)에 남기는 로거를 만든다. 재호출해도 핸들러가 중복되지 않는다."""
    logger = logging.getLogger(f"kp_arb.{name}")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    file_handler = logging.FileHandler(log_dir / f"{name}_{stamp}.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger
