"""배포판(exe) 진입점 — 인자로 어떤 화면/프로세스를 띄울지 정한다.

PyInstaller로 얼리면 `python -m kp_arb.모듈` 방식을 쓸 수 없어서,
exe 하나가 인자를 보고 분기한다 (main_window.launch_module이 인자를 맞춰 실행).

    meme.exe            # 메인 화면 (기본)
    meme.exe monitor    # 시세 모니터
    meme.exe autoT      # 자동T 주문 화면
    meme.exe autoM      # 자동M 주문 화면
    meme-core.exe core  # 코어 (콘솔 exe — 로그 표시)
"""
from __future__ import annotations

import sys


def main() -> None:
    """인자 1개로 분기. 알 수 없는 인자는 메인 화면."""
    # 윈도우 콘솔(CP949)이 못 그리는 문자로 print가 죽지 않게 — 대체 문자로 출력
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass
    arg = sys.argv[1] if len(sys.argv) > 1 else "main"
    if arg == "core":
        from kp_arb.core_server import main as run
        run()
    elif arg == "monitor":
        from kp_arb.monitor import main as run
        run()
    elif arg == "autoT":
        from kp_arb.order_autot import main as run
        run()
    elif arg == "fx_monitor":
        from kp_arb.fx_monitor import main as run
        run()
    elif arg == "order_hl":
        from kp_arb.order_hl import main as run
        run()
    elif arg == "order_list":
        from kp_arb.order_list import main as run
        run()
    elif arg == "fx_auction_order":
        from kp_arb.fx_auction_order import main as run
        run()
    elif arg == "settings":
        from kp_arb.settings_window import main as run
        run()
    elif arg == "keys":
        from kp_arb.key_setup import main as run
        run()
    else:
        from kp_arb.main_window import main as run
        run()


if __name__ == "__main__":
    main()
