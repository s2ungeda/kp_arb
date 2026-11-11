"""HL latency probe: HTTP vs WebSocket post, same signed actions, same session.

Per round, for each path (http, ws) in turn:
  maker (ALO buy 10% below mid) -> cancel -> taker (IOC buy) -> close (IOC reduce-only sell)
Fills DO happen (taker + close); the account is flat after each path. rounds=0 sends no orders.
After the rounds: optional idle wait (idle_s, WS kept alive with pings), then one cancel of a
non-existent order id per path -> "cold HTTP" vs "warm WS" cost. Always runs (no orders).
Finally matches requests with on-chain block times via the public explorer (may lag by region).

Needs:  Python 3.11+ 64-bit, pip install hyperliquid-python-sdk (brings websocket-client).
Keys:   env HL_AGENT_KEY (throw-away agent key), HL_ACCOUNT_ADDRESS (0x...). NTP-synced clock.
Usage:  python hl_ws_order_probe.py [rounds=3] [sz=1] [tag=here] [idle_s=0]
        python hl_ws_order_probe.py 3 1 tokyo 420     # 3 rounds, then 7 min idle, then cold test
        python hl_ws_order_probe.py 0 1 tokyo 420     # no orders: idle + cold cancel test only
ASCII-only on purpose (remote Windows consoles may lack Korean fonts).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, cast

INFO_URL = "https://api.hyperliquid.xyz/info"
EXPLORER_URL = "https://rpc.hyperliquid.xyz/explorer"
WS_URL = "wss://api.hyperliquid.xyz/ws"
COIN, DEX = "xyz:SMSN", "xyz"
FAKE_OID = 123456789
MAKER_OFFSET = -0.10
TAKER_SLIPPAGE = 0.03
PING_EVERY_S = 20.0
KST = timezone(timedelta(hours=9))
KINDS = ("maker", "cancel", "taker", "close", "fakecancel")
PATHS = ("http", "ws")


def post(url: str, payload: dict[str, Any]) -> Any:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def kst(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%H:%M:%S.%f")[:-3]


def med(xs: list[float]) -> str:
    return f"{statistics.median(xs):.0f}" if xs else "-"


class Rec:
    def __init__(self, path: str, kind: str, t_send: float, t_resp: float, note: str) -> None:
        self.path, self.kind, self.t_send, self.t_resp, self.note = path, kind, t_send, t_resp, note
        self.t_block: float | None = None

    @property
    def rtt(self) -> float:
        return self.t_resp - self.t_send


class WsPoster:
    """One WebSocket; sends {"method":"post",...,"request":{"type":"action"}} and waits by id."""

    def __init__(self, timeout_s: float = 10.0) -> None:
        self.timeout_s = timeout_s
        self.ws: Any = None
        self.next_id = 0
        self.last_ping = 0.0
        self.connects = 0

    def connect(self) -> None:
        import websocket  # websocket-client (dependency of the HL SDK)

        self.ws = websocket.create_connection(WS_URL, timeout=self.timeout_s)
        self.connects += 1
        self.last_ping = time.monotonic()
        print(f"  [ws] connected (#{self.connects})")

    def ensure(self) -> None:
        if self.ws is None:
            self.connect()

    def ping(self) -> None:
        self.ensure()
        self.ws.send(json.dumps({"method": "ping"}))
        self.last_ping = time.monotonic()

    def keep_alive_sleep(self, seconds: float) -> None:
        """Idle while keeping the WS alive: ping every PING_EVERY_S AND keep reading.

        Reading matters: websocket-client answers the server's ping frames (pong) and sees
        the close frame only inside recv(). Send-only idling got closed by the server
        ("socket is already closed." = TLS EOF on send; seen from Tokyo 2026-11-11).
        """
        import websocket

        end = time.monotonic() + seconds
        next_report = time.monotonic() + 60.0
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            if time.monotonic() >= next_report:  # silent otherwise - show it is alive
                print(f"  [idle] {left:.0f}s left, ws connects so far {self.connects}")
                next_report += 60.0
            try:
                self.ensure()
                self.ws.settimeout(min(left, 1.0))
                self.ws.recv()  # pong / server ping (auto-answered) / close frame
                if not self.ws.connected:
                    raise ConnectionError("server sent close")
                if time.monotonic() - self.last_ping >= PING_EVERY_S:
                    self.ping()
            except websocket.WebSocketTimeoutException:
                if time.monotonic() - self.last_ping >= PING_EVERY_S:
                    try:
                        self.ping()
                    except Exception as exc:  # noqa: BLE001
                        print(f"  [ws] ping failed ({exc}) - reconnecting")
                        self.ws = None
                        self.connect()
            except Exception as exc:  # noqa: BLE001
                print(f"  [ws] idle read failed ({exc}) - reconnecting")
                self.ws = None
                time.sleep(1.0)
                self.connect()
            finally:
                if self.ws is not None:
                    self.ws.settimeout(self.timeout_s)

    def post_action(self, payload: dict[str, Any]) -> Any:
        self.ensure()
        self.next_id += 1
        rid = self.next_id
        frame = json.dumps({"method": "post", "id": rid,
                            "request": {"type": "action", "payload": payload}})
        try:
            self.ws.send(frame)
        except Exception as exc:  # noqa: BLE001
            self.ws = None
            raise ConnectionError(f"ws send failed: {exc}") from exc
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            try:
                raw = self.ws.recv()
            except Exception as exc:  # noqa: BLE001
                self.ws = None
                raise ConnectionError(f"ws recv failed: {exc}") from exc
            msg = json.loads(raw)
            if msg.get("channel") != "post":
                continue  # pong / other
            data = msg.get("data") or {}
            if data.get("id") != rid:
                continue
            resp = data.get("response") or {}
            if resp.get("type") == "error":
                raise RuntimeError(f"ws error: {resp.get('payload')}")
            return resp.get("payload")
        raise TimeoutError("ws post timed out")


def status_note(resp: Any) -> tuple[str, int | None, float]:
    try:
        st = resp["response"]["data"]["statuses"][0]
    except (KeyError, TypeError, IndexError):
        return json.dumps(resp)[:80], None, 0.0
    if "resting" in st:
        return f"resting oid={st['resting']['oid']}", int(st["resting"]["oid"]), 0.0
    if "filled" in st:
        f = st["filled"]
        return (f"filled sz={f['totalSz']} avgPx={f['avgPx']} oid={f['oid']}", None,
                float(f["totalSz"]))
    if "error" in st:
        return f"error: {st['error'][:70]}", None, 0.0
    if "success" in st:
        return "success", None, 0.0
    return json.dumps(st)[:80], None, 0.0


def timed(path: str, kind: str, fn: Any, recs: list[Rec],
          pause: float = 1.5) -> tuple[int | None, float]:
    t_send = time.time() * 1000
    t0 = time.perf_counter()
    try:
        resp = fn()
    except Exception as exc:  # noqa: BLE001
        resp = {"status": "exc", "error": f"{type(exc).__name__}: {exc}"}
    ms = (time.perf_counter() - t0) * 1000
    note, oid, filled = status_note(resp)
    recs.append(Rec(path, kind, t_send, t_send + ms, note))
    print(f"  {path:<4} {kind:<10} {kst(t_send)} KST rtt {ms:4.0f}ms  {note}")
    time.sleep(pause)
    return oid, filled


def position_size(addr: str) -> float:
    st = post(INFO_URL, {"type": "clearinghouseState", "user": addr, "dex": DEX})
    for p in st.get("assetPositions", []):
        if p["position"]["coin"] == COIN:
            return float(p["position"]["szi"])
    return 0.0


class Router:
    """Swaps the SDK's HTTP sender (Exchange._post_action) for the WS poster per request."""

    def __init__(self, ex: Any, poster: WsPoster) -> None:
        self.ex, self.poster = ex, poster
        self.http = ex._post_action  # noqa: SLF001
        self.path = "http"
        ex._post_action = self._post_action  # noqa: SLF001

    def _post_action(self, action: Any, signature: Any, nonce: int) -> Any:
        if self.path == "http":
            return self.http(action, signature, nonce)
        payload = {
            "action": action, "nonce": nonce, "signature": signature,
            "vaultAddress": (self.ex.vault_address
                             if action["type"] not in ["usdClassTransfer", "sendAsset"] else None),
            "expiresAfter": self.ex.expires_after,
        }
        return self.poster.post_action(payload)


def one_cycle(ex: Any, router: Router, path: str, sz: float, recs: list[Rec]) -> None:
    router.path = path
    px_maker = ex._slippage_price(COIN, True, MAKER_OFFSET)  # noqa: SLF001
    oid, _ = timed(path, "maker", partial(ex.order, COIN, True, sz, px_maker,
                                          {"limit": {"tif": "Alo"}}), recs)
    if oid is not None:
        timed(path, "cancel", partial(ex.cancel, COIN, oid), recs)
    else:
        print("  cancel skipped (maker did not rest)")
    px_taker = ex._slippage_price(COIN, True, TAKER_SLIPPAGE)  # noqa: SLF001
    _, filled = timed(path, "taker", partial(ex.order, COIN, True, sz, px_taker,
                                             {"limit": {"tif": "Ioc"}}), recs)
    if filled > 0:
        px_close = ex._slippage_price(COIN, False, TAKER_SLIPPAGE)  # noqa: SLF001
        timed(path, "close", partial(ex.order, COIN, False, filled, px_close,
                                     {"limit": {"tif": "Ioc"}}, reduce_only=True), recs)
    else:
        print("  close skipped (taker did not fill)")


def match_chain(recs: list[Rec], addr: str) -> None:
    rows: list[dict[str, Any]] = []
    for attempt in range(6):
        time.sleep(3.0 * (attempt + 1))
        try:
            d = post(EXPLORER_URL, {"type": "userDetails", "user": addr})
            rows = [t for t in d.get("txs", [])
                    if t.get("time", 0) >= recs[0].t_send - 500
                    and t.get("action", {}).get("type") in ("order", "cancel")]
        except Exception as exc:  # noqa: BLE001
            print(f"  explorer query failed ({exc}) - retrying")
            continue
        if len(rows) >= len(recs):
            break
        print(f"  explorer shows {len(rows)}/{len(recs)} tx - waiting for index")
    rows.sort(key=lambda t: t["time"])
    if len(rows) != len(recs):
        print(f"  chain has {len(rows)} tx != {len(recs)} sent - block matching skipped"
              " (explorer index lags from some regions; paste this output and match later)")
        return
    for rec, t in zip(recs, rows, strict=True):
        want = "order" if rec.kind in ("maker", "taker", "close") else "cancel"
        if t["action"]["type"] != want:
            print("  tx order mismatch - block matching skipped")
            return
        rec.t_block = float(t["time"])
        print(f"  {rec.path:<4} {rec.kind:<10} block {t.get('block')} {kst(rec.t_block)}"
              f"  request->block {rec.t_block - rec.t_send:4.0f}ms"
              f"  block->response {rec.t_resp - rec.t_block:4.0f}ms")


def main(argv: list[str]) -> int:
    rounds = int(argv[0]) if argv else 3
    sz = float(argv[1]) if len(argv) > 1 else 1.0
    tag = argv[2] if len(argv) > 2 else "here"
    idle_s = float(argv[3]) if len(argv) > 3 else 0.0
    key, addr = os.environ.get("HL_AGENT_KEY"), os.environ.get("HL_ACCOUNT_ADDRESS")
    if not (key and addr):
        print("HL_AGENT_KEY / HL_ACCOUNT_ADDRESS not set - nothing sent")
        return 1
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    ex = Exchange(Account.from_key(key), constants.MAINNET_API_URL,
                  account_address=addr, perp_dexs=[DEX], timeout=10)
    poster = WsPoster()
    poster.connect()
    router = Router(ex, poster)
    print(f"== HL http-vs-ws probe [{tag}] {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST, "
          f"{rounds} rounds, sz {sz}, idle {idle_s:.0f}s, {COIN} ==")
    recs: list[Rec] = []
    for r in range(rounds):
        mid = float(ex.info.all_mids(DEX)[ex.info.name_to_coin[COIN]])
        print(f"-- round {r + 1}/{rounds}  mid {mid}")
        for path in PATHS:
            one_cycle(ex, router, path, sz, recs)
    if rounds:
        try:
            left = position_size(addr)
            print(f"  position after rounds: {left} {COIN}")
            if left != 0:
                print(f"  WARNING: position left {left} {COIN} - close it manually")
        except Exception as exc:  # noqa: BLE001
            print(f"  position check failed ({exc}) - verify on the HL app")
    # cold test: idle (WS pinged, HTTP untouched), then one fake cancel per path
    if idle_s > 0:
        print(f"-- idle {idle_s:.0f}s (ws pinged every {PING_EVERY_S:.0f}s, http idle)")
        poster.keep_alive_sleep(idle_s)
    print("-- fake-oid cancel per path (no order): cold http vs warm ws" if idle_s > 0
          else "-- fake-oid cancel per path (no order)")
    for path in PATHS:
        router.path = path
        timed(path, "fakecancel", partial(ex.cancel, COIN, FAKE_OID), recs, pause=1.0)
    for path in PATHS:  # second shot = both warm
        router.path = path
        timed(path, "fakecancel", partial(ex.cancel, COIN, FAKE_OID), recs, pause=1.0)
    match_chain(recs, addr)
    print("== medians (ms) ==")
    summary: dict[str, Any] = {"tag": tag, "idle_s": idle_s}
    for path in PATHS:
        for kind in KINDS:
            rs = [x for x in recs if x.path == path and x.kind == kind]
            if not rs:
                continue
            tb = [x.t_block - x.t_send for x in rs if x.t_block is not None]
            tr = [x.t_resp - x.t_block for x in rs if x.t_block is not None]
            rtts = [x.rtt for x in rs]
            print(f"  {path:<4} {kind:<10} n={len(rs)} rtt {med(rtts)} "
                  f"(first {rtts[0]:.0f})  request->block {med(tb)}  block->response {med(tr)}")
            summary[f"{path}.{kind}"] = {"n": len(rs), "rtt": med(rtts), "first": round(rtts[0]),
                                         "to_block": med(tb), "to_resp": med(tr)}
    print("SUMMARY " + json.dumps(summary))  # paste this line back for comparison
    try:
        poster.ws.close()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    cast(Any, sys.stdout).reconfigure(line_buffering=True)  # every line shows at once
    raise SystemExit(main(sys.argv[1:]))
