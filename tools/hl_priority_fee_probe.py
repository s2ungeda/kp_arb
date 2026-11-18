"""HL priority-fee probe (DESIGN-hl-priority-fee section 5) -- MAINNET, REAL ORDER. Run by the user only.

Sends ONE IOC limit buy far below the market (default 3% under best bid) with the priority-fee
grouping {"p": P} through the same SDK call the core uses (Exchange.bulk_orders(..., grouping=...)).
An IOC that cannot cross is cancelled by the exchange at once, so normally nothing fills and no
position is left; IOC priority fee is charged on FILLED notional only, so a no-fill probe costs 0.
What we learn: (1) does the server accept our signature with a dict grouping, (2) the response
shape, (3) any error text (e.g. no undelegated staked HYPE).

Keys:   env HL_AGENT_KEY (agent private key), HL_ACCOUNT_ADDRESS (0x...). Same as the core.
Usage:  python tools/hl_priority_fee_probe.py [p=1] [sz=1] [offset=0.03] [coin=xyz:SMSN]
        python tools/hl_priority_fee_probe.py 1 1          # p=1 (~0.0001bp), size 1
        python tools/hl_priority_fee_probe.py 10000 1      # p=10000 (=1bp)
ASCII-only on purpose.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

INFO_URL = "https://api.hyperliquid.xyz/info"


def main() -> int:
    key = os.environ.get("HL_AGENT_KEY", "")
    addr = os.environ.get("HL_ACCOUNT_ADDRESS", "")
    if not key or not addr:
        print("set HL_AGENT_KEY and HL_ACCOUNT_ADDRESS first")
        return 2
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    sz = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    offset = float(sys.argv[3]) if len(sys.argv) > 3 else 0.03
    coin = sys.argv[4] if len(sys.argv) > 4 else "xyz:SMSN"

    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils.constants import MAINNET_API_URL

    info = Info(MAINNET_API_URL, skip_ws=True, perp_dexs=["xyz"])
    ex = Exchange(Account.from_key(key), MAINNET_API_URL, account_address=addr, perp_dexs=["xyz"])
    book = info.l2_snapshot(coin)
    bid = float(book["levels"][0][0]["px"])
    raw = bid * (1.0 - offset)
    # price grid: 5 significant figures / 2 decimals for this coin family -- round to 2 decimals
    px = round(raw, 2)
    grouping: Any = {"p": p}
    req = {"coin": coin, "is_buy": True, "sz": sz, "limit_px": px,
           "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": False}
    print(f"best bid {bid}  -> IOC buy {sz} @ {px} (offset {offset:.1%}), grouping {grouping}")
    print("this is a REAL mainnet order; it should cancel at once without filling. Ctrl+C to abort")
    time.sleep(3.0)
    t0 = time.time()
    try:
        resp = ex.bulk_orders([req], grouping=grouping)
    except Exception as exc:  # noqa: BLE001 - show everything
        print(f"EXCEPTION after {time.time() - t0:.3f}s: {type(exc).__name__}: {exc}")
        return 1
    took = time.time() - t0
    print(f"response after {took:.3f}s:")
    print(json.dumps(resp, ensure_ascii=False, indent=1))
    try:
        st = resp["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        st = None
    if isinstance(st, dict) and "filled" in st:
        print("WARNING: it FILLED -- you now hold a position; close it by hand (general order window)")
    elif isinstance(st, dict) and "error" in st:
        print(f"order error: {st['error']}")
    else:
        print(f"status: {st}  (expected: resting then immediate cancel, or an error text)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
