"""Back-of-queue fill model, shared by the paper gateway and tools/hl_join.py.

An order joining a level sits behind everything resting there. Prints AT
our price eat the queue ahead first; a print THROUGH our price fills us.
When the visible level shrinks below our queue-ahead (cancels), we move up
(cancels are assumed to come from ahead of us: optimistic, which is why sim
results are treated as optimistic; Arcus's sim was ~1.4x high on volume).
Prices/sizes are floats here: this is simulation, nothing is sent.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SimOrder:
    is_buy: bool
    px: float
    sz: float
    ahead: float
    filled: float = 0.0

    @property
    def remaining(self) -> float:
        return self.sz - self.filled


def join(is_buy: bool, px: float, sz: float, level_size: float) -> SimOrder:
    return SimOrder(is_buy, px, sz, max(0.0, level_size))


def on_trade(o: SimOrder, px: float, sz: float) -> float:
    """Apply one public print; returns the size filled for `o`."""
    if o.remaining <= 0:
        return 0.0
    through = px < o.px if o.is_buy else px > o.px
    if through:
        f = min(sz, o.remaining)
        o.filled += f
        return f
    if px != o.px:
        return 0.0
    eat = min(sz, o.ahead)
    o.ahead -= eat
    left = sz - eat
    if left <= 0:
        return 0.0
    f = min(left, o.remaining)
    o.filled += f
    return f


def on_level(o: SimOrder, level_size_ex_ours: float) -> None:
    """Visible size at our price (our own size removed) shrank: queue ahead can't exceed it."""
    o.ahead = min(o.ahead, max(0.0, level_size_ex_ours))


def crossed(o: SimOrder, best_bid: float, best_ask: float) -> bool:
    """The opposite touch moved through our price: we would have been filled."""
    return (o.is_buy and 0 < best_ask <= o.px) or (not o.is_buy and best_bid >= o.px > 0)
