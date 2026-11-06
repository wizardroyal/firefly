"""Telegram alerts (port of arcus-mm alerts.py).

Off unless both env vars are set (secrets never go in config):
    ENTROPY_TELEGRAM_TOKEN    bot token from @BotFather
    ENTROPY_TELEGRAM_CHAT_ID  your chat id
Fire-and-forget: a slow or failing Telegram never delays quoting, and the
token (part of the URL) is never logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time

import aiohttp

log = logging.getLogger("emm.alerts")

COOLDOWN_S = 600.0
API = "https://api.telegram.org"


class Notifier:
    def __init__(self, prefix: str, ipv4_only: bool = False, token: str | None = None, chat_id: str | None = None):
        self.token = token if token is not None else os.environ.get("ENTROPY_TELEGRAM_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("ENTROPY_TELEGRAM_CHAT_ID", "")
        self.prefix = prefix
        self.ipv4_only = ipv4_only
        self._last: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, key: str, text: str, force: bool = False) -> bool:
        if not self.enabled:
            return False
        now = time.monotonic()
        if not force and now - self._last.get(key, -1e9) < COOLDOWN_S:
            return False
        self._last[key] = now
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        t = loop.create_task(self._post(f"[{self.prefix}] {text}"))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return True

    async def _post(self, text: str) -> None:
        try:
            if self._session is None:
                conn = aiohttp.TCPConnector(family=socket.AF_INET) if self.ipv4_only else None
                self._session = aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=10))
            async with self._session.post(f"{API}/bot{self.token}/sendMessage",
                                          json={"chat_id": self.chat_id, "text": text[:3500]}) as r:
                if r.status != 200:
                    log.warning("telegram alert not sent: HTTP %s", r.status)
        except Exception as e:
            log.warning("telegram alert not sent: %s", type(e).__name__)

    async def close(self, timeout: float = 5.0) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)
        if self._session is not None:
            await self._session.close()
            self._session = None
