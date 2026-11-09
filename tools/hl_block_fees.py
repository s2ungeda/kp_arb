"""HL 블록 우선순위 수수료 조회 — 개발용 래퍼. 본체는 kp_arb/hl_block_fees_tool.py(배포판은
``meme-core.exe blockfees`` / block_fees.bat).

사용: python tools\\hl_block_fees.py <YYYY-MM-DD> <HH:MM:SS> [초=2]
      [--me 0x주소] [--ours | --coin a,b] [--who]
  예: python tools\\hl_block_fees.py 2026-11-04 08:00:00 2 --ours
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kp_arb.hl_block_fees_tool import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
