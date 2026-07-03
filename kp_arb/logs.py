"""파일 로그 설정 — 도구별로 logs/<이름>_YYYYMMDD.log 에 남긴다."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


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
