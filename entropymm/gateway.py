"""Order gateways: Live (real orders) and Paper (simulated against the live
public feed, nothing sent). The bot only talks to this interface.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from . import queue as q
from .hl import HlRest
from .meta import AssetMeta

log = logging.getLogger("emm.gw")
D = Decimal

GONE_HINTS = ("never placed", "already canceled", "already cancelled", "filled", "not found")


@dataclass
class LiveOrder:
    oid: int
    coin: str
    is_buy: bool
    price: Decimal
    size: Decimal
    reduce_only: bool
    filled: Decimal = D(0)
    last_change: float = field(default_factory=time.monotonic)
    pending: bool = False

    @property
    def remaining(self) -> Decimal:
        return self.size - self.filled

    @property
    def side(self) -> str:
        return "BUY" if self.is_buy else "SELL"


# on_fill(coin, is_buy, px, sz, fee, oid, tid, crossed, ts_ms)
FillCb = Callable[[str, bool, Decimal, Decimal, Decimal, int, str, bool, int], None]


class LiveGateway:
    paper = False

    def __init__(self, rest: HlRest, dex: str = "io"):
        self.rest = rest
        self.dex = dex
        self.last_error = ""

    async def place(self, meta: AssetMeta, is_buy: bool, price: Decimal, size: Decimal,
                    reduce_only: bool = False, tif: str = "Alo") -> LiveOrder | None:
        r = await self.rest.place(meta, is_buy, price, size, reduce_only, tif)
        self.last_error = r.error
        if not r.ok or r.oid is None or r.filled_sz > 0 and tif == "Alo":
            if r.error:
                log.info("place %s %s %s@%s refused: %s", meta.coin, "BUY" if is_buy else "SELL", size, price, r.error)
            return None
        return LiveOrder(r.oid, meta.coin, is_buy, price, size, reduce_only)

    async def modify(self, meta: AssetMeta, o: LiveOrder, price: Decimal, size: Decimal) -> bool | None:
        """True = modified; None = the exchange refused it for this order -> treat
        the order as GONE: caller cancels (not-found is fine), drops, re-places.
        (Arcus: a crossing ALO modify removed the order and later modifies got a
        misleading error. Same rule here until verified on Hyperliquid.)
        Network errors raise; the caller's `finally` clears `pending`."""
        r = await self.rest.modify(meta, o.oid, o.is_buy, price, size, o.reduce_only)
        self.last_error = r.error
        if r.ok:
            if r.oid:
                o.oid = r.oid                         # a modify may assign a new oid
            o.price, o.size, o.filled, o.last_change = price, size, D(0), time.monotonic()
            return True
        log.warning("modify of %s %s refused (%s); will cancel and re-place", meta.coin, o.oid, r.error)
        return None

    async def cancel(self, meta: AssetMeta, o: LiveOrder) -> bool:
        r = await self.rest.cancel(meta, o.oid)
        self.last_error = r.error
        return r.ok or any(h in r.error.lower() for h in GONE_HINTS)

    async def cancel_all(self, metas: dict[str, AssetMeta]) -> int:
        rows = await self.rest.open_orders(self.dex)
        pairs = [(metas[r["coin"]].asset, int(r["oid"])) for r in rows if r.get("coin") in metas]
        unknown = [r.get("coin") for r in rows if r.get("coin") not in metas]
        if unknown:
            log.warning("open orders on coins the bot does not know (left alone): %s", unknown)
        if pairs:
            await self.rest.cancel_many(pairs)
        return len(pairs)

    async def positions(self) -> dict[str, Decimal]:
        return await self.rest.positions(self.dex)


class PaperGateway:
    """Simulated fills with the back-of-queue model (entropymm/queue.py): our
    order joins behind the visible size at its price; prints at our price eat
    that queue first, prints through our price fill us. Fees at the configured
    growth-mode rates so paper cost per $ is comparable to live."""
    paper = True

    def __init__(self, on_fill: FillCb, maker_fee_bps: float = 0.3, taker_fee_bps: float = 0.9):
        self.on_fill = on_fill
        self.maker_fee = D(str(maker_fee_bps)) / D(10_000)
        self.taker_fee = D(str(taker_fee_bps)) / D(10_000)
        self.orders: dict[int, LiveOrder] = {}
        self.sims: dict[int, q.SimOrder] = {}
        self.pos: dict[str, Decimal] = {}
        self.books: dict[str, tuple[list, list]] = {}
        self._n = 0
        self.last_error = ""

    def _level(self, coin: str, is_buy: bool, px: Decimal) -> float:
        bids, asks = self.books.get(coin, ([], []))
        for lp, ls in (bids if is_buy else asks):
            if lp == px:
                return float(ls)
        return 0.0

    async def place(self, meta, is_buy, price, size, reduce_only=False, tif="Alo"):
        bids, asks = self.books.get(meta.coin, ([], []))
        if tif == "Alo" and ((is_buy and asks and price >= asks[0][0]) or (not is_buy and bids and price <= bids[0][0])):
            self.last_error = "Post only order would have immediately matched"
            return None
        if reduce_only and (self.pos.get(meta.coin, D(0)) == 0
                            or (self.pos[meta.coin] > 0) == is_buy):
            self.last_error = "Reduce only order would increase position"
            return None
        self._n += 1
        o = LiveOrder(self._n, meta.coin, is_buy, price, size, reduce_only)
        if tif == "Ioc":
            touch = asks[0][0] if is_buy and asks else bids[0][0] if bids else None
            if touch is not None and ((is_buy and price >= touch) or (not is_buy and price <= touch)):
                self._fill(o, size, touch, crossed=True)
            return None
        self.orders[o.oid] = o
        self.sims[o.oid] = q.join(is_buy, float(price), float(size), self._level(meta.coin, is_buy, price))
        return o

    async def modify(self, meta, o, price, size):
        if o.oid not in self.orders:
            return None
        if o.price != price:
            self.sims[o.oid] = q.join(o.is_buy, float(price), float(size), self._level(meta.coin, o.is_buy, price))
        else:
            self.sims[o.oid].sz = float(size)
        o.price, o.size, o.filled, o.last_change = price, size, D(0), time.monotonic()
        self.sims[o.oid].filled = 0.0
        return True

    async def cancel(self, meta, o):
        self.orders.pop(o.oid, None)
        self.sims.pop(o.oid, None)
        return True

    async def cancel_all(self, metas=None):
        n = len(self.orders)
        self.orders.clear()
        self.sims.clear()
        return n

    async def positions(self):
        return dict(self.pos)

    def _fill(self, o: LiveOrder, size: Decimal, px: Decimal | None = None, crossed: bool = False) -> None:
        size = min(size, o.remaining)
        if size <= 0:
            return
        px = px if px is not None else o.price
        sgn = 1 if o.is_buy else -1
        self.pos[o.coin] = self.pos.get(o.coin, D(0)) + sgn * size
        o.filled += size
        self._n += 1
        fee = (px * size * (self.taker_fee if crossed else self.maker_fee)).quantize(D("0.000001"))
        if o.remaining <= 0:
            self.orders.pop(o.oid, None)
            self.sims.pop(o.oid, None)
        self.on_fill(o.coin, o.is_buy, px, size, fee, o.oid, f"paper-{self._n}", crossed, int(time.time() * 1000))

    def on_book(self, coin: str, bids: list, asks: list) -> None:
        self.books[coin] = (bids, asks)
        if not bids or not asks:
            return
        for oid, o in list(self.orders.items()):
            if o.coin != coin:
                continue
            s = self.sims[oid]
            if q.crossed(s, float(bids[0][0]), float(asks[0][0])):
                self._fill(o, o.remaining)
                continue
            q.on_level(s, self._level(coin, o.is_buy, o.price))

    def on_trade(self, coin: str, px: Decimal, sz: Decimal) -> None:
        for oid, o in list(self.orders.items()):
            if o.coin != coin or oid not in self.sims:
                continue
            f = q.on_trade(self.sims[oid], float(px), float(sz))
            if f > 0:
                self._fill(o, D(str(round(f, 10))))
