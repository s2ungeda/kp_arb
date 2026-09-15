"""개발 PC용 껍데기 — real_log/ 복사본으로 자동M 지연·슬리피지 리포트(kp_arb.report_latency).

    python tools/round_latency.py 20260914            # real_log/ 의 그 날짜
    python tools/round_latency.py 20260914 logs       # 다른 폴더
운영 PC에서는 배포판 `meme-core.exe report [YYYYMMDD]`(report.bat) — 같은 코드.
"""
from __future__ import annotations

import sys
from pathlib import Path

from kp_arb.report_latency import main

if __name__ == "__main__":
    argv = list(sys.argv)
    if len(argv) < 3:  # 폴더 생략 → real_log/
        argv = argv[:2] + [str(Path(__file__).resolve().parent.parent / "real_log")]
    raise SystemExit(main(argv))
