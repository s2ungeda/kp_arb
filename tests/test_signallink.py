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


def test_peer_list_includes_name() -> None:
    from kp_arb.signallink import SignalLinkSink

    sink = SignalLinkSink(system_name="kp-arb")
    sink._peers["p1"] = __import__("kp_arb.signallink", fromlist=["_Peer"])._Peer(
        ip="10.0.0.5", tcp_port=5001, last_seen=1.0, name="감시창")
    peers = sink.peer_list()
    assert peers == [{"name": "감시창", "ip": "10.0.0.5", "port": 5001}]


def test_parse_incoming_and_token_filter() -> None:
    from kp_arb.signallink import ACCEPT_TOKEN, parse_incoming

    line = ('inst1\t감시\t{"id":"s1","fx":1385.2,"total_domestic":0,'
            '"total_coin":104,"token":"Meme","datetime":"t"}')
    parsed = parse_incoming(line)
    assert parsed is not None
    name, payload = parsed
    assert name == "감시" and payload["token"] == ACCEPT_TOKEN
    assert payload["total_coin"] == 104
    # 형식/JSON 오류는 None
    assert parse_incoming("inst1\t감시") is None          # 필드 부족
    assert parse_incoming("inst1\t감시\t{broken") is None  # JSON 오류


def test_incoming_message_token_filter() -> None:
    # sink가 token=Meme만 on_message 호출, 다른 토큰은 스킵 — 순수하게 필터만 검증
    from kp_arb.signallink import parse_incoming

    meme = ('i\tn\t{"token":"Meme","total_coin":1,"fx":1,"id":"a",'
            '"total_domestic":0,"datetime":"t"}')
    dalin = meme.replace("Meme", "Dalin")
    assert parse_incoming(meme)[1]["token"] == "Meme"      # 처리 대상
    assert parse_incoming(dalin)[1]["token"] == "Dalin"    # 스킵 대상(호출측이 거름)
