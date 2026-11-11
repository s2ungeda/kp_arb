"""HL latency probe with REAL orders (maker + taker) - run from the region under test.

Per round it sends four /exchange requests on COIN and times each one:
  1. maker  : ALO (post-only) buy, 10% below mid  -> rests, never fills
  2. cancel : cancel that resting order
  3. taker  : IOC buy at mid+slippage            -> fills (takes liquidity)
  4. close  : IOC reduce-only sell of the position -> fills, account is flat again
Then it matches every request with its on-chain tx (public explorer) to split
request->block and block->response, and prints medians per kind.

Fills DO happen (taker + close). Keep sz small. Needs margin for sz * mid.
Needs:  Python 3.11+ 64-bit, pip install hyperliquid-python-sdk, NTP-synced clock.
Keys:   env HL_AGENT_KEY (throw-away agent key), HL_ACCOUNT_ADDRESS (0x...).
Usage:  python hl_region_order_probe.py [rounds=3] [sz=1] [tag=here]
Windows PowerShell example:
        $env:HL_AGENT_KEY = "..." ; $env:HL_ACCOUNT_ADDRESS = "0x..."
        python C:\\hl\\hl_region_order_probe.py 3 1 tokyo
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
from typing import Any

INFO_URL = "https://api.hyperliquid.xyz/info"
EXPLORER_URL = "https://rpc.hyperliquid.xyz/explorer"
COIN, DEX = "xyz:SMSN", "xyz"
MAKER_OFFSET = -0.10   # ALO buy 10% below mid (never crosses)
TAKER_SLIPPAGE = 0.03  # IOC price 3% through the book (fills at best available)
KST = timezone(timedelta(hours=9))
KINDS = ("maker", "cancel", "taker", "close")


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
    def __init__(self, kind: str, t_send: float, t_resp: float, note: str) -> None:
        self.kind, self.t_send, self.t_resp, self.note = kind, t_send, t_resp, note
        self.t_block: float | None = None

    @property
    def rtt(self) -> float:
        return self.t_resp - self.t_send


def status_note(resp: Any) -> tuple[str, int | None, float]:
    """Summarize an /exchange response -> (note, oid if resting, filled size)."""
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


def timed(kind: str, fn: Any, recs: list[Rec]) -> tuple[int | None, float]:
    t_send = time.time() * 1000
    t0 = time.perf_counter()
    try:
        resp = fn()
    except Exception as exc:  # noqa: BLE001
        resp = {"status": "exc", "error": f"{type(exc).__name__}: {exc}"}
    ms = (time.perf_counter() - t0) * 1000
    note, oid, filled = status_note(resp)
    recs.append(Rec(kind, t_send, t_send + ms, note))
    print(f"  {kind:<6} {kst(t_send)} KST rtt {ms:4.0f}ms  {note}")
    time.sleep(1.5)
    return oid, filled


def position_size(addr: str) -> float:
    """Position on COIN via raw /info clearinghouseState (dex-scoped). 0.0 if none."""
    st = post(INFO_URL, {"type": "clearinghouseState", "user": addr, "dex": DEX})
    for p in st.get("assetPositions", []):
        if p["position"]["coin"] == COIN:
            return float(p["position"]["szi"])
    return 0.0


def run(rounds: int, sz: float, key: str, addr: str) -> list[Rec]:
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    ex = Exchange(Account.from_key(key), constants.MAINNET_API_URL,
                  account_address=addr, perp_dexs=[DEX], timeout=10)
    recs: list[Rec] = []
    for r in range(rounds):
        mid = float(ex.info.all_mids(DEX)[ex.info.name_to_coin[COIN]])
        print(f"-- round {r + 1}/{rounds}  mid {mid}")
        # 1. maker (ALO, 10% below mid)
        px_maker = ex._slippage_price(COIN, True, MAKER_OFFSET)  # noqa: SLF001
        oid, _ = timed("maker", partial(ex.order, COIN, True, sz, px_maker,
                                        {"limit": {"tif": "Alo"}}), recs)
        # 2. cancel it
        if oid is not None:
            timed("cancel", partial(ex.cancel, COIN, oid), recs)
        else:
            print("  cancel skipped (maker did not rest)")
        # 3. taker (IOC through the book)
        px_taker = ex._slippage_price(COIN, True, TAKER_SLIPPAGE)  # noqa: SLF001
        _, filled = timed("taker", partial(ex.order, COIN, True, sz, px_taker,
                                           {"limit": {"tif": "Ioc"}}), recs)
        # 4. close what the taker just bought (IOC reduce-only). Uses the fill size from
        #    the order response itself - an earlier version asked the SDK for the position
        #    and got 0, leaving 3 contracts open (Tokyo 2026-11-11 10:32).
        if filled > 0:
            px_close = ex._slippage_price(COIN, False, TAKER_SLIPPAGE)  # noqa: SLF001
            timed("close", partial(ex.order, COIN, False, filled, px_close,
                                   {"limit": {"tif": "Ioc"}}, reduce_only=True), recs)
        else:
            print("  close skipped (taker did not fill)")
    try:
        left = position_size(addr)
    except Exception as exc:  # noqa: BLE001
        print(f"  position check failed ({exc}) - verify on the HL app")
        return recs
    print(f"  position after run: {left} {COIN}")
    if left != 0:
        print(f"  WARNING: position left {left} {COIN} - close it manually")
    return recs


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
        want = "cancel" if rec.kind == "cancel" else "order"
        if t["action"]["type"] != want:
            print("  tx order mismatch - block matching skipped")
            return
        rec.t_block = float(t["time"])
        print(f"  {rec.kind:<6} block {t.get('block')} {kst(rec.t_block)}"
              f"  request->block {rec.t_block - rec.t_send:4.0f}ms"
              f"  block->response {rec.t_resp - rec.t_block:4.0f}ms")


def main(argv: list[str]) -> int:
    rounds = int(argv[0]) if argv else 3
    sz = float(argv[1]) if len(argv) > 1 else 1.0
    tag = argv[2] if len(argv) > 2 else "here"
    key, addr = os.environ.get("HL_AGENT_KEY"), os.environ.get("HL_ACCOUNT_ADDRESS")
    if not (key and addr):
        print("HL_AGENT_KEY / HL_ACCOUNT_ADDRESS not set - nothing sent")
        return 1
    print(f"== HL order probe [{tag}] {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST, "
          f"{rounds} rounds, sz {sz}, {COIN} ==")
    recs = run(rounds, sz, key, addr)
    if not recs:
        return 1
    match_chain(recs, addr)
    print("== medians (ms) ==")
    summary: dict[str, Any] = {"tag": tag}
    for kind in KINDS:
        rs = [r for r in recs if r.kind == kind]
        if not rs:
            continue
        tb = [r.t_block - r.t_send for r in rs if r.t_block is not None]
        tr = [r.t_resp - r.t_block for r in rs if r.t_block is not None]
        print(f"  {kind:<6} n={len(rs)} rtt {med([r.rtt for r in rs])}"
              f"  request->block {med(tb)}  block->response {med(tr)}")
        summary[kind] = {"n": len(rs), "rtt": med([r.rtt for r in rs]),
                         "to_block": med(tb), "to_resp": med(tr)}
    print("SUMMARY " + json.dumps(summary))  # paste this line back for comparison
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
