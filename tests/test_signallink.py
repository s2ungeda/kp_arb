"""SignalLink 순수 로직 테스트 — JSON 포맷·HELLO 파싱·피어 테이블·시퀀스 (소켓 없음)."""
from kp_arb.domain.enums import Instrument, Side, Underlying, Venue
from kp_arb.domain.models import Position
from kp_arb.fx import hl_coin_notional
from kp_arb.fx_reporter import Signal
from kp_arb.signallink import (
    PeerTable,
    _SeqGen,
    parse_bye,
    parse_hello,
    signal_wire_json,
)

SAMSUNG = Underlying.SAMSUNG


def _hl(qty: float, avg: float, side: Side = Side.SELL) -> Position:
    return Position(venue=Venue.HYPERLIQUID, instrument=Instrument.HL_PERP,
                    underlying=SAMSUNG, side=side, qty=qty, avg_price=avg)


def test_hl_coin_notional() -> None:
    # Σ(평균단가 × 수량) — HL만, 방향 무관(절대 수량)
    positions = [_hl(2, 52.0), _hl(3, 100.0, side=Side.SELL),
                 Position(venue=Venue.LS, instrument=Instrument.KR_STOCK,
                          underlying=SAMSUNG, side=Side.BUY, qty=100, avg_price=70_000)]
    assert hl_coin_notional(positions) == 2 * 52.0 + 3 * 100.0  # 국내 제외


def test_signal_wire_json_matches_delphi() -> None:
    # total_coin/total_domestic 정수, token/순서 고정 (ChatComm SendPayload와 동일 형태)
    sig = Signal(id="sig-20260724-001", fx=1385.2, total_domestic=0.0,
                 total_coin=104.6, token="Meme", datetime="2026-07-24T09:30:00")
    wire = signal_wire_json(sig)
    assert wire == ('{"id":"sig-20260724-001","fx":1385.2,"total_domestic":0,'
                    '"total_coin":105,"token":"Meme","datetime":"2026-07-24T09:30:00"}')


def test_parse_hello_and_bye() -> None:
    assert parse_hello("HELLO\tabc123\t감시\t5001") == ("abc123", "감시", 5001)
    assert parse_hello("HELLO\tabc\t이름\t0") is None    # 포트 0 제외
    assert parse_hello("BYE\tabc") is None
    assert parse_hello("HELLO\tabc") is None             # 필드 부족
    assert parse_bye("BYE\tabc123") == "abc123"
    assert parse_bye("HELLO\tabc\t이름\t5001") is None


def test_peer_table_discovery_and_timeout() -> None:
    table = PeerTable()
    assert table.on_hello("HELLO\tp1\t감시\t5001", ip="10.0.0.5", now=100.0)
    assert "p1" in table.peers and table.peers["p1"].tcp_port == 5001
    # 자기 자신은 등록 안 함
    assert not table.on_hello("HELLO\tme\t나\t5002", ip="10.0.0.9", now=100.0, self_id="me")
    # 15초 지나면 만료
    table.prune(now=100.0 + 16.0)
    assert "p1" not in table.peers
    # BYE로 즉시 제거
    table.on_hello("HELLO\tp2\t감시\t5003", ip="10.0.0.6", now=200.0)
    assert table.on_bye("BYE\tp2") and "p2" not in table.peers


def test_seq_gen_daily_reset() -> None:
    gen = _SeqGen()
    assert gen.next_id("20260724") == "sig-20260724-001"
    assert gen.next_id("20260724") == "sig-20260724-002"
    assert gen.next_id("20260725") == "sig-20260725-001"  # 날짜 바뀌면 리셋
