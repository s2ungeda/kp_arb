"""체결쏴(자동M)-주식선물(HL선) 화면 — order_autom의 화면 골격을 HL선 사양(SF_HL_FIRST_SPEC)으로.

exec §7D 시험(사용자 2026-09-22): 선주문 HL ALO(메이커) → 후주문 LS SF 테이커. 화면은 주식선물
골격 그대로이고 코어 상품 "sf_hl_first"(종목 상태 키 "종목|sf_hl_first")로 명령을 보낸다.
"""
from .order_autom import SF_HL_FIRST_SPEC
from .order_autom import main as _main


def main() -> None:
    """HL선 체결쏴 화면 실행 (python -m kp_arb.order_autom_hl_first [--preview])."""
    _main(SF_HL_FIRST_SPEC)


if __name__ == "__main__":
    main()
