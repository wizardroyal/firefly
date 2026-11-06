"""Record Hyperliquid public feeds to JSONL for Phase 0 research. No orders, no keys.

    python tools/hl_record.py 3600 rec-rth-20261109.jsonl [--coins io:SNDK,xyz:SNDK,io:NBIS,xyz:NBIS]

io coins: l2Book (top 5 levels) + trades. Other coins (the xyz references):
bbo + trades. Each line: {"t": local epoch seconds, "ch": channel, "d": data}.
Record >= 60 min in EACH session (US RTH, extended, overnight, weekend) plus
transitions (oracle switch, Monday open) before tuning anything.
"""
import argparse
import asyncio
import json
import time

import websockets

URL = "wss://api.hyperliquid.xyz/ws"
DEPTH = 5


async def record(seconds: float, out: str, coins: list[str]) -> None:
    end = time.time() + seconds
    n = 0
    with open(out, "a", buffering=1) as f:
        while time.time() < end:
            try:
                async with websockets.connect(URL, ping_interval=None, max_size=16 * 2**20) as ws:
                    for c in coins:
                        book = "l2Book" if c.startswith("io:") else "bbo"
                        for typ in (book, "trades"):
                            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": typ, "coin": c}}))
                    last_ping = time.time()
                    while time.time() < end:
                        if time.time() - last_ping > 30:
                            await ws.send(json.dumps({"method": "ping"}))
                            last_ping = time.time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        m = json.loads(raw)
                        ch, d = m.get("channel"), m.get("data")
                        if ch not in ("l2Book", "bbo", "trades") or d is None:
                            continue
                        if ch == "l2Book":
                            d = {"coin": d["coin"], "time": d["time"], "levels": [lv[:DEPTH] for lv in d["levels"]]}
                        f.write(json.dumps({"t": round(time.time(), 3), "ch": ch, "d": d}) + "\n")
                        n += 1
            except (websockets.ConnectionClosed, OSError) as e:
                print("reconnecting:", e)
                await asyncio.sleep(2)
    print(f"wrote {n} messages to {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("seconds", type=float)
    ap.add_argument("out")
    ap.add_argument("--coins", default="io:SNDK,xyz:SNDK,io:NBIS,xyz:NBIS")
    a = ap.parse_args()
    asyncio.run(record(a.seconds, a.out, a.coins.split(",")))
