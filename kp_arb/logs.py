"""파일 로그 설정 — 도구별로 logs/<이름>_YYYYMMDD.log 에 남긴다."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


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
    if any(isinstance(h, DailyFileHandler | logging.NullHandler) for h in logger.handlers):
        return logger
    if log_dir is None:
        logger.addHandler(logging.NullHandler())
        return logger
    try:
        log_dir.mkdir(exist_ok=True)
        handler = DailyFileHandler(log_dir, prefix=prefix)
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
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
