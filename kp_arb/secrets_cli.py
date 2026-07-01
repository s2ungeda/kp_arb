"""kp-arb 비밀을 Windows 자격증명관리자(DPAPI)에 등록/조회/삭제.

    python -m kp_arb.secrets_cli set LS_STOCK_APPKEY     # 값은 화면에 안 보이게 입력
    python -m kp_arb.secrets_cli has LS_STOCK_APPKEY     # 등록 여부
    python -m kp_arb.secrets_cli del LS_STOCK_APPKEY

값은 명령 인자로 받지 않는다(히스토리 평문 노출 방지) — set은 getpass로 입력받는다.
"""
from __future__ import annotations

import getpass
import sys
from collections.abc import Callable

from .config import KEYRING_SERVICE


def main(argv: list[str], *, prompt: Callable[[str], str] = getpass.getpass) -> int:
    import keyring

    if len(argv) < 3:
        print("usage: python -m kp_arb.secrets_cli {set|has|del} NAME")
        return 2
    cmd, name = argv[1], argv[2]
    if cmd == "set":
        keyring.set_password(KEYRING_SERVICE, name, prompt(f"{name}: "))
        print(f"stored {name} in {KEYRING_SERVICE}")
    elif cmd == "has":
        print("yes" if keyring.get_password(KEYRING_SERVICE, name) is not None else "no")
    elif cmd == "del":
        keyring.delete_password(KEYRING_SERVICE, name)
        print(f"deleted {name}")
    else:
        print(f"unknown command {cmd!r}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
