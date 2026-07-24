"""SignalLinkSink — Dalin broadcast(ChatComm.pas) 호환 노출 전송 채널.

프로토콜(델파이 원본 실측):
- **피어 발견**: UDP 8888 브로드캐스트. HELLO = "HELLO\\t인스턴스ID\\t이름\\tTCP포트",
  BYE = "BYE\\t인스턴스ID". 15초 무응답 피어 제거. 우리도 5초마다 HELLO 브로드캐스트.
- **전송**: 발견된 각 피어의 IP:TCP포트로 TCP 접속 → "인스턴스ID\\t이름\\t<JSON>\\n" 후 끊음.
- JSON = {"id","fx","total_domestic","total_coin","token","datetime"} — total_coin/
  total_domestic은 정수(Cardinal), fx는 소수, id="sig-YYYYMMDD-NNN"(일별 3자리 시퀀스).

순수 로직(JSON 포맷·HELLO 파싱·시퀀스)은 소켓과 분리해 테스트한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field

from .fx_reporter import Signal

UDP_PORT = 8888
BROADCAST_IP = "255.255.255.255"
HEARTBEAT_S = 5.0
PEER_TIMEOUT_S = 15.0
HELLO = "HELLO"
BYE = "BYE"
TAB = "\t"
ACCEPT_TOKEN = "Meme"  # 이 토큰만 수신 처리, 나머지는 스킵 (사용자 확정 2026-07-24)

log = logging.getLogger("kp_arb.signallink")


def parse_incoming(line: str) -> tuple[str, dict[str, object]] | None:
    """수신 TCP 라인 "인스턴스ID\\t이름\\t<JSON>" → (보낸이 이름, JSON dict).

    형식/JSON 오류는 None. token 필터는 호출측(수신 처리)에서.
    """
    parts = line.split(TAB, 2)
    if len(parts) < 3:
        return None
    try:
        payload = json.loads(parts[2])
    except (json.JSONDecodeError, ValueError):
        return None
    return (parts[1], payload) if isinstance(payload, dict) else None


def signal_wire_json(signal: Signal) -> str:
    """Signal → 델파이 JSON 문자열 (total_coin/total_domestic 정수, 순서 고정)."""
    return (
        f'{{"id":"{signal.id}","fx":{signal.fx:g},'
        f'"total_domestic":{int(round(signal.total_domestic))},'
        f'"total_coin":{int(round(signal.total_coin))},'
        f'"token":"{signal.token}","datetime":"{signal.datetime}"}}'
    )


def parse_hello(raw: str) -> tuple[str, str, int] | None:
    """HELLO 패킷 파싱 → (인스턴스ID, 이름, TCP포트). HELLO 아니거나 포트 0이면 None."""
    parts = raw.split(TAB)
    if len(parts) < 4 or parts[0] != HELLO:
        return None
    try:
        port = int(parts[3])
    except ValueError:
        return None
    return (parts[1], parts[2], port) if port > 0 else None


def parse_bye(raw: str) -> str | None:
    """BYE 패킷 파싱 → 인스턴스ID. 아니면 None."""
    parts = raw.split(TAB)
    return parts[1] if len(parts) >= 2 and parts[0] == BYE else None


@dataclass
class _Peer:
    ip: str
    tcp_port: int
    last_seen: float
    name: str = ""


@dataclass
class _SeqGen:
    """일별 3자리 시퀀스 id 생성 — "sig-YYYYMMDD-NNN"."""

    date: str = ""
    seq: int = 0

    def next_id(self, yyyymmdd: str) -> str:
        if yyyymmdd != self.date:
            self.date = yyyymmdd
            self.seq = 0
        self.seq += 1
        return f"sig-{yyyymmdd}-{self.seq:03d}"


class SignalLinkSink:
    """ExposureSink 구현 — UDP 발견 + TCP 전송. start()/stop()으로 백그라운드 관리.

    라이브 소켓이라 테스트에서 직접 띄우지 않는다(순수 헬퍼만 테스트).
    """

    def __init__(
        self,
        *,
        system_name: str = "kp-arb",
        udp_port: int = UDP_PORT,
        broadcast_ip: str = BROADCAST_IP,
        on_message: Callable[[str, dict[str, object]], None] | None = None,
        accept_token: str = ACCEPT_TOKEN,
    ) -> None:
        self._name = system_name
        self._udp_port = udp_port
        self._broadcast_ip = broadcast_ip
        self._instance_id = f"kparb-{socket.gethostname()[:8]}"
        self._peers: dict[str, _Peer] = {}
        self._seq = _SeqGen()
        self._udp: socket.socket | None = None
        self._tcp_server: asyncio.Server | None = None
        self._tcp_port = 0  # 실제 TCP 수신 포트 (HELLO에 알림)
        self._on_message = on_message
        self._accept_token = accept_token
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """UDP 발견 + TCP 수신 서버 + 하트비트 시작."""
        loop = asyncio.get_running_loop()
        # TCP 수신 서버 — 메시지를 받아 token 필터 (포트는 OS 할당)
        self._tcp_server = await asyncio.start_server(
            self._handle_conn, "0.0.0.0", 0)
        self._tcp_port = self._tcp_server.sockets[0].getsockname()[1]
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.bind(("0.0.0.0", self._udp_port))
        udp.setblocking(False)
        self._udp = udp
        self._tasks = [
            loop.create_task(self._recv_loop()),
            loop.create_task(self._heartbeat_loop()),
        ]
        log.info("SignalLink 시작 — UDP %d 발견 / TCP %d 수신 (이름 %s, 토큰 %s)",
                 self._udp_port, self._tcp_port, self._name, self._accept_token)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tcp_server is not None:
            self._tcp_server.close()
            self._tcp_server = None
        if self._udp is not None:
            self._broadcast(f"{BYE}{TAB}{self._instance_id}")
            self._udp.close()
            self._udp = None

    async def _handle_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """TCP 수신 — 한 줄 읽어 token=accept_token만 처리, 나머지 스킵."""
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            parsed = parse_incoming(line)
            if parsed is not None:
                name, payload = parsed
                if payload.get("token") == self._accept_token:
                    if self._on_message is not None:
                        self._on_message(name, payload)
                else:  # 다른 토큰 — 스킵
                    log.debug("수신 스킵 (토큰 %s≠%s)",
                              payload.get("token"), self._accept_token)
        except (TimeoutError, OSError):
            pass
        finally:
            writer.close()

    def _broadcast(self, packet: str) -> None:
        if self._udp is None:
            return
        try:
            self._udp.sendto(packet.encode("utf-8"),
                             (self._broadcast_ip, self._udp_port))
        except OSError:
            pass

    async def _heartbeat_loop(self) -> None:
        # HELLO에 실제 TCP 수신 포트를 알린다 — 피어가 우리에게 메시지를 보낼 수 있게.
        while True:
            self._broadcast(
                f"{HELLO}{TAB}{self._instance_id}{TAB}{self._name}{TAB}{self._tcp_port}")
            self._prune_peers()
            await asyncio.sleep(HEARTBEAT_S)

    async def _recv_loop(self) -> None:
        loop = asyncio.get_running_loop()
        assert self._udp is not None
        while True:
            try:
                data, addr = await loop.sock_recvfrom(self._udp, 4096)
            except (OSError, asyncio.CancelledError):
                return
            raw = data.decode("utf-8", errors="replace")
            hello = parse_hello(raw)
            if hello is not None:
                instance_id, name, port = hello
                if instance_id != self._instance_id:
                    self._peers[instance_id] = _Peer(addr[0], port, _now(), name)
                continue
            bye = parse_bye(raw)
            if bye is not None:
                self._peers.pop(bye, None)

    def _prune_peers(self) -> None:
        cutoff = _now() - PEER_TIMEOUT_S
        for pid in [p for p, v in self._peers.items() if v.last_seen < cutoff]:
            self._peers.pop(pid, None)

    async def send(self, signal: Signal) -> bool:
        """발견된 모든 피어로 TCP 전송. 하나라도 성공하면 True."""
        payload = signal_wire_json(signal)
        packet = f"{self._instance_id}{TAB}{self._name}{TAB}{payload}\n"
        peers = list(self._peers.values())
        if not peers:
            return False
        results = await asyncio.gather(
            *(self._send_tcp(p.ip, p.tcp_port, packet) for p in peers),
            return_exceptions=True,
        )
        return any(r is True for r in results)

    @staticmethod
    async def _send_tcp(ip: str, port: int, packet: str) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=2.0)
            writer.write(packet.encode("utf-8"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            del reader
            return True
        except (TimeoutError, OSError):
            return False

    def next_signal_id(self, yyyymmdd: str) -> str:
        """일별 시퀀스 id — 리포터가 id를 위임할 때 사용."""
        return self._seq.next_id(yyyymmdd)

    def peer_list(self) -> list[dict[str, object]]:
        """발견된 피어 목록 — 감시 화면 표시용 (이름·IP·포트)."""
        return [{"name": p.name, "ip": p.ip, "port": p.tcp_port}
                for p in self._peers.values()]


def _now() -> float:
    import time

    return time.monotonic()


@dataclass
class PeerTable:
    """테스트용 순수 피어 테이블 — 소켓 없이 발견/만료 로직 검증."""

    peers: dict[str, _Peer] = field(default_factory=dict)

    def on_hello(self, raw: str, ip: str, now: float, self_id: str = "") -> bool:
        hello = parse_hello(raw)
        if hello is None or hello[0] == self_id:
            return False
        self.peers[hello[0]] = _Peer(ip, hello[2], now)
        return True

    def on_bye(self, raw: str) -> bool:
        bye = parse_bye(raw)
        if bye is None:
            return False
        return self.peers.pop(bye, None) is not None

    def prune(self, now: float, timeout: float = PEER_TIMEOUT_S) -> None:
        cutoff = now - timeout
        for pid in [p for p, v in self.peers.items() if v.last_seen < cutoff]:
            self.peers.pop(pid, None)
