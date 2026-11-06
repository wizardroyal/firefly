"""Hyperliquid REST client (async) + signer.

DECISION (handoff "decide first"): the official SDK is synchronous (requests)
with a threaded websocket. We use it ONLY for L1-action signing
(hyperliquid.utils.signing.sign_l1_action) and talk to the API with our own
aiohttp REST + websockets WS client, arcus-mm style. The bot is one asyncio
loop; no threads touch bot state.

Nonces: one source per signer. Each write takes max(now_ms, last + 1) and is
signed synchronously in the same step, so concurrent asyncio writes can never
reuse a nonce. Action dicts are built in the SDK's key order: the signature
is over msgpack(action), so key order and the exact number strings matter.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

import aiohttp

from .meta import AssetMeta, wire

log = logging.getLogger("emm.rest")
D = Decimal

RATE_LIMIT_HINTS = ("too many cumulative requests", "rate limit")


class Signer:
    def __init__(self, private_key: str, vault_address: str | None, is_mainnet: bool):
        from eth_account import Account
        self.wallet = Account.from_key(private_key)
        self.vault = vault_address.lower() if vault_address else None
        self.is_mainnet = is_mainnet
        self._last_nonce = 0

    @property
    def address(self) -> str:
        return self.wallet.address

    def next_nonce(self) -> int:
        n = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = n
        return n

    def payload(self, action: dict) -> dict:
        from hyperliquid.utils.signing import sign_l1_action
        nonce = self.next_nonce()
        sig = sign_l1_action(self.wallet, action, self.vault, nonce, None, self.is_mainnet)
        return {"action": action, "nonce": nonce, "signature": sig, "vaultAddress": self.vault, "expiresAfter": None}


def order_wire(asset: int, is_buy: bool, px: Decimal, sz: Decimal, reduce_only: bool, tif: str) -> dict:
    return {"a": asset, "b": is_buy, "p": wire(px), "s": wire(sz), "r": reduce_only, "t": {"limit": {"tif": tif}}}


@dataclass
class WriteResult:
    ok: bool                         # request accepted AND the per-order status is not an error
    oid: int | None = None
    error: str = ""
    filled_sz: Decimal = D(0)
    raw: dict = field(default_factory=dict)

    @property
    def rate_limited(self) -> bool:
        return any(h in self.error.lower() for h in RATE_LIMIT_HINTS)


def parse_status(body: dict) -> WriteResult:
    """{"status":"ok","response":{"type":..,"data":{"statuses":[s]}}} or {"status":"err","response":msg}.
    s: {"resting":{"oid"}} | {"filled":{"totalSz","avgPx","oid"}} | {"error": msg} | "success"."""
    if not isinstance(body, dict) or body.get("status") != "ok":
        msg = body.get("response") if isinstance(body, dict) else body
        return WriteResult(False, error=str(msg), raw=body if isinstance(body, dict) else {})
    resp = body.get("response") or {}
    statuses = (resp.get("data") or {}).get("statuses") if isinstance(resp, dict) else None
    if not statuses:
        return WriteResult(True, raw=body)
    s = statuses[0]
    if s == "success":
        return WriteResult(True, raw=body)
    if isinstance(s, dict):
        if "error" in s:
            return WriteResult(False, error=str(s["error"]), raw=body)
        if "resting" in s:
            return WriteResult(True, oid=int(s["resting"]["oid"]), raw=body)
        if "filled" in s:
            f = s["filled"]
            return WriteResult(True, oid=int(f.get("oid", 0)) or None, filled_sz=D(str(f.get("totalSz", 0))), raw=body)
    return WriteResult(True, raw=body)


class HlRest:
    def __init__(self, base_url: str, signer: Signer | None, account: str,
                 dump: Callable[..., None] | None = None, ipv4_only: bool = False,
                 on_write: Callable[[WriteResult], None] | None = None):
        self.base = base_url.rstrip("/")
        self.signer = signer
        self.account = account.lower() if account else ""
        self.dump = dump
        self.ipv4_only = ipv4_only
        self.on_write = on_write
        self._s: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        conn = aiohttp.TCPConnector(family=socket.AF_INET) if self.ipv4_only else None
        self._s = aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=10))
        return self

    async def __aexit__(self, *exc):
        if self._s:
            await self._s.close()
            self._s = None

    async def _post(self, path: str, body: dict):
        assert self._s is not None, "use `async with HlRest(...)`"
        async with self._s.post(self.base + path, json=body) as r:
            txt = await r.text()
            if r.status == 429:
                raise aiohttp.ClientResponseError(r.request_info, (), status=429, message="IP rate limited")
            try:
                import json
                data = json.loads(txt)
            except ValueError:
                data = {"status": "err", "response": f"HTTP {r.status}: {txt[:300]}"}
            if r.status >= 500:
                raise aiohttp.ClientResponseError(r.request_info, (), status=r.status, message=txt[:200])
            return data

    # ------------------------------------------------------------------ reads
    async def info(self, body: dict):
        return await self._post("/info", body)

    async def perp_dexs(self) -> list:
        return await self.info({"type": "perpDexs"})

    async def meta_ctxs(self, dex: str) -> tuple[dict, list]:
        m, c = await self.info({"type": "metaAndAssetCtxs", "dex": dex})
        return m, c

    async def l2(self, coin: str) -> dict:
        return await self.info({"type": "l2Book", "coin": coin})

    async def state(self, dex: str) -> dict:
        return await self.info({"type": "clearinghouseState", "user": self.account, "dex": dex})

    async def open_orders(self, dex: str) -> list:
        return await self.info({"type": "openOrders", "user": self.account, "dex": dex})

    async def fills_since(self, start_ms: int) -> list:
        return await self.info({"type": "userFillsByTime", "user": self.account, "startTime": int(start_ms)})

    async def funding_since(self, start_ms: int) -> list:
        return await self.info({"type": "userFunding", "user": self.account, "startTime": int(start_ms)})

    async def user_fees(self) -> dict:
        return await self.info({"type": "userFees", "user": self.account})

    async def rate_limit(self) -> dict:
        return await self.info({"type": "userRateLimit", "user": self.account})

    async def positions(self, dex: str) -> dict[str, Decimal]:
        st = await self.state(dex)
        return {p["position"]["coin"]: D(p["position"]["szi"]) for p in st.get("assetPositions", [])}

    # ------------------------------------------------------------------ writes
    async def _write(self, action: dict) -> WriteResult:
        if self.signer is None:
            raise RuntimeError("no signer (dry run?)")
        payload = self.signer.payload(action)
        body = await self._post("/exchange", payload)
        res = parse_status(body)
        if self.dump:
            self.dump("write", action=action, body=body)
        if self.on_write:
            self.on_write(res)
        return res

    async def place(self, meta: AssetMeta, is_buy: bool, px: Decimal, sz: Decimal,
                    reduce_only: bool = False, tif: str = "Alo") -> WriteResult:
        return await self._write({"type": "order", "orders": [order_wire(meta.asset, is_buy, px, sz, reduce_only, tif)],
                                  "grouping": "na"})

    async def modify(self, meta: AssetMeta, oid: int, is_buy: bool, px: Decimal, sz: Decimal,
                     reduce_only: bool = False, tif: str = "Alo") -> WriteResult:
        return await self._write({"type": "batchModify",
                                  "modifies": [{"oid": int(oid),
                                                "order": order_wire(meta.asset, is_buy, px, sz, reduce_only, tif)}]})

    async def cancel(self, meta: AssetMeta, oid: int) -> WriteResult:
        return await self._write({"type": "cancel", "cancels": [{"a": meta.asset, "o": int(oid)}]})

    async def cancel_many(self, pairs: list[tuple[int, int]]) -> WriteResult:
        """pairs: (asset, oid). Hyperliquid has no cancel-all; list then batch-cancel."""
        if not pairs:
            return WriteResult(True)
        return await self._write({"type": "cancel", "cancels": [{"a": a, "o": int(o)} for a, o in pairs]})

    async def schedule_cancel(self, at_ms: int | None) -> WriteResult:
        action: dict = {"type": "scheduleCancel"}
        if at_ms is not None:
            action["time"] = int(at_ms)
        return await self._write(action)

    async def update_leverage(self, meta: AssetMeta, leverage: int, is_cross: bool = False) -> WriteResult:
        return await self._write({"type": "updateLeverage", "asset": meta.asset, "isCross": is_cross,
                                  "leverage": int(leverage)})


async def with_retries(fn, *a, tries: int = 3, delay: float = 1.0):
    for i in range(tries):
        try:
            return await fn(*a)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            if i == tries - 1:
                raise
            await asyncio.sleep(delay * (2 ** i))
