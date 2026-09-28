"""개발 PC용 껍데기 — 자동M 판별 정산 Sprd + 인자 CSV(kp_arb.report_sprd).

    python tools/round_sprd.py 20260928         # real_log/ 의 그 날짜 → real_log/sprd_20260928.csv
    python tools/round_sprd.py 20260928 logs    # 다른 폴더(개발 PC 로그)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kp_arb.report_sprd import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
