"""Address request-allowance governor.

Hyperliquid limits each address to (10,000 + cumulative $ traded) requests
over its lifetime; once used up it is down to one request per 10 seconds.
Verified live 2026-11-06 via info{type: userRateLimit}: nRequestsCap =
cumVlm + 10000, and nRequestsUsed counts up. Each order/cancel in a batch is
believed to count as one request (UNVERIFIED, see CLAUDE.md), so every write
here is counted per order.

A $19 bot starts with ~10k requests. Repricing ~28k bbo changes/h is out of
the question; the bot reprices only when its order is off the touch, and this
governor slows that further as the surplus shrinks.
"""
from __future__ import annotations

import time


class RateBudget:
    def __init__(self, min_surplus: int = 300):
        self.surplus: int | None = None   # cap - used, refreshed from REST
        self.min_surplus = min_surplus
        self.actions = 0
        self.t0 = time.monotonic()

    def on_rest(self, row: dict) -> None:
        try:
            self.surplus = int(float(row["nRequestsCap"])) - int(row["nRequestsUsed"])
        except (KeyError, TypeError, ValueError):
            pass

    def spend(self, n: int = 1) -> None:
        self.actions += n
        if self.surplus is not None:
            self.surplus -= n

    def exhausted(self) -> None:
        """The exchange said we are out of requests."""
        self.surplus = 0

    @property
    def scale(self) -> float:
        """Multiplier on requote threshold and minimum interval (1 = unconstrained)."""
        s = self.surplus if self.surplus is not None else 10**9
        if s > 3000:
            return 1.0
        if s > 1000:
            return 3.0
        return 10.0

    @property
    def critical_only(self) -> bool:
        """Below this only safety moves (cancels, reducing quotes) are allowed."""
        return self.surplus is not None and self.surplus < self.min_surplus

    def per_hour(self) -> float:
        return self.actions / max((time.monotonic() - self.t0) / 3600, 1e-6)
