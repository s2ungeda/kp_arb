"""파일 로그 큐 전환(logs.QueuedHandler) — 포맷 1회, 예외 출력 유지, 종료 시 전부 기록."""
from __future__ import annotations

import logging
from pathlib import Path

from kp_arb.logs import DailyFileHandler, QueuedHandler, attach_daily_file, has_file_handler


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_queued_handler_formats_once_and_flushes_on_close(tmp_path: Path) -> None:
    # 표준 QueueHandler는 큐에 넣기 전 미리 포맷해 시각·레벨 접두가 두 번 붙는다 — 여기선 한 번.
    target = logging.FileHandler(tmp_path / "q.log", encoding="utf-8")
    target.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    qh = QueuedHandler(target)
    qh.setFormatter(logging.Formatter("IGNORED %(message)s"))  # 큐 핸들러의 포맷터는 무시
    logger = logging.getLogger("test.logs.queue.once")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(qh)
    logger.info("체결 %s %d", "선주문", 3)
    qh.close()  # 쓰기 스레드가 남은 레코드를 다 쓰고 멈춘다
    logger.removeHandler(qh)
    text = _read(tmp_path / "q.log")
    assert text.count("INFO test.logs.queue.once 체결 선주문 3") == 1
    assert "IGNORED" not in text and text.count("INFO") == 1  # 접두 1회


def test_queued_handler_keeps_exception_traceback(tmp_path: Path) -> None:
    target = logging.FileHandler(tmp_path / "e.log", encoding="utf-8")
    target.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    qh = QueuedHandler(target)
    logger = logging.getLogger("test.logs.queue.exc")
    logger.propagate = False
    logger.addHandler(qh)
    try:
        raise ValueError("boom-42")
    except ValueError:
        logger.warning("조회 실패", exc_info=True)
    qh.close()
    logger.removeHandler(qh)
    text = _read(tmp_path / "e.log")
    assert "WARNING 조회 실패" in text and "ValueError: boom-42" in text and "Traceback" in text


def test_attach_daily_file_uses_queue_and_does_not_duplicate(tmp_path: Path) -> None:
    logger = attach_daily_file("test.logs.queue.daily", "autom_test", tmp_path)
    again = attach_daily_file("test.logs.queue.daily", "autom_test", tmp_path)
    assert again is logger and len(logger.handlers) == 1
    (handler,) = logger.handlers
    assert isinstance(handler, QueuedHandler) and isinstance(handler.target, DailyFileHandler)
    assert has_file_handler(logger, DailyFileHandler)
    for i in range(50):
        logger.info("줄 %d", i)
    handler.close()
    logger.removeHandler(handler)
    files = list(tmp_path.glob("autom_test_*.log"))
    assert len(files) == 1
    lines = _read(files[0]).splitlines()
    assert len(lines) == 50 and lines[-1].endswith("INFO 줄 49")
