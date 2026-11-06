"""Hyperliquid WebSocket streams with auto-reconnect.

Subscriptions (formats verified live 2026-11-06 for l2Book/bbo/trades):
  l2Book     io coins we trade   {"coin","time","levels":[[bids],[asks]]}, level {"px","sz","n"}
  bbo        xyz reference coins {"coin","time","bbo":[bid|null, ask|null]}
  trades     io coins            [{"coin","side":"B"|"A" (taker),"px","sz","time","tid","users"}]
  orderUpdates(user)             [{"order":{...,"oid"},"status","statusTimestamp"}]
  userFills(user)                {"isSnapshot"?, "user", "fills":[...]}
The server drops connections that send nothing for 60 s, so we ping every
30 s. Messages CAN be lost without a visible disconnect: REST is the record
(the bot reconciles every 20 s and re-syncs fills after every reconnect).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
from typing import Awaitable, Callable

import websockets

log = logging.getLogger("emm.ws")


class Streams:
    def __init__(self, url: str, book_coins: list[str], bbo_coins: list[str], user: str | None,
                 on_message: Callable[[dict], None], on_reconnect: Callable[[], Awaitable[None]] | None = None,
                 dump: Callable[..., None] | None = None, ipv4_only: bool = False):
        self.url = url
        self.book_coins, self.bbo_coins = book_coins, bbo_coins
        self.user = user.lower() if user else None
        self.on_message = on_message
        self.on_reconnect = on_reconnect
        self.dump = dump
        self.ipv4_only = ipv4_only
        self.connected = asyncio.Event()
        self._stop = False

    def subs(self) -> list[dict]:
        out = []
        for c in self.book_coins:
            out.append({"type": "l2Book", "coin": c})
            out.append({"type": "trades", "coin": c})
        for c in self.bbo_coins:
            out.append({"type": "bbo", "coin": c})
        if self.user:
            out.append({"type": "orderUpdates", "user": self.user})
            out.append({"type": "userFills", "user": self.user})
        return out

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(30)
            await ws.send(json.dumps({"method": "ping"}))

    async def run(self) -> None:
        backoff, first = 1.0, True
        while not self._stop:
            ping = None
            try:
                extra = {"family": socket.AF_INET} if self.ipv4_only else {}
                async with websockets.connect(self.url, ping_interval=None, max_size=16 * 2**20, **extra) as ws:
                    for s in self.subs():
                        await ws.send(json.dumps({"method": "subscribe", "subscription": s}))
                    ping = asyncio.create_task(self._pinger(ws))
                    self.connected.set()
                    backoff = 1.0
                    if not first and self.on_reconnect:
                        await self.on_reconnect()
                    first = False
                    async for raw in ws:
                        msg = json.loads(raw)
                        if self.dump:
                            self.dump("ws", msg=msg)
                        ch = msg.get("channel")
                        if ch in ("pong", "subscriptionResponse"):
                            continue
                        if ch == "error":
                            log.warning("ws error frame: %s", msg)
                            continue
                        self.on_message(msg)
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                log.warning("ws disconnected: %s", e)
            except Exception:
                log.exception("ws handler error")
            finally:
                if ping:
                    ping.cancel()
            self.connected.clear()
            if self._stop:
                break
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 30)

    def stop(self) -> None:
        self._stop = True
