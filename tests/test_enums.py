"""도메인 열거형 순수 로직 테스트."""
from kp_arb.domain.enums import Underlying


def test_from_krx_code_roundtrip() -> None:
    for underlying in Underlying:
        assert Underlying.from_krx_code(underlying.krx_code) is underlying


def test_from_krx_code_known() -> None:
    assert Underlying.from_krx_code("005930") is Underlying.SAMSUNG
    assert Underlying.from_krx_code("000660") is Underlying.SK_HYNIX
    assert Underlying.from_krx_code("005380") is Underlying.HYUNDAI


def test_from_krx_code_unknown_is_none() -> None:
    assert Underlying.from_krx_code("999999") is None
    assert Underlying.from_krx_code("") is None
