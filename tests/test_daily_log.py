"""코어 로그 자정 롤오버 핸들러 테스트 (Phase 8 — 파일명이 당일 날짜)."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from kp_arb.core_server import _DailyFileHandler


def test_daily_handler_writes_today_file(tmp_path: Path) -> None:
    handler = _DailyFileHandler(tmp_path)
    today = time.strftime("%Y%m%d")
    try:
        assert (tmp_path / f"core_{today}.log").exists()  # 시작 즉시 당일 파일
        logger = logging.getLogger("test_daily_write")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.info("hello-log")
        logger.removeHandler(handler)
    finally:
        handler.close()
    assert "hello-log" in (tmp_path / f"core_{today}.log").read_text(encoding="utf-8")


def test_daily_handler_rolls_on_date_change(tmp_path: Path) -> None:
    handler = _DailyFileHandler(tmp_path)
    today = time.strftime("%Y%m%d")
    handler._day = "19990101"  # 어제(가짜)로 되돌림 → 다음 기록에서 오늘로 갈아타야
    logger = logging.getLogger("test_daily_roll")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info("after-midnight")
        logger.removeHandler(handler)
    finally:
        handler.close()
    assert handler._day == today  # 오늘로 갱신됨
    assert "after-midnight" in (tmp_path / f"core_{today}.log").read_text(encoding="utf-8")
