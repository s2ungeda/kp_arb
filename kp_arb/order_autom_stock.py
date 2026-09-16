"""체결쏴(자동M)-주식 화면 — order_autom의 화면 골격을 주식 사양(STOCK_SPEC)으로 띄운다.

사용자 2026-09-16: "레이아웃은 주식선물과 유사, 역방향 제외, 주식용 공통설정은 따로". 화면 먼저
`--preview`(코어 미접속)로 완성하고, 판정·수량 비율 같은 전략 결정은 뒤에 붙인다.
"""
from .order_autom import STOCK_SPEC
from .order_autom import main as _main


def main() -> None:
    """주식 버전 체결쏴 화면 실행 (python -m kp_arb.order_autom_stock [--preview])."""
    _main(STOCK_SPEC)


if __name__ == "__main__":
    main()
