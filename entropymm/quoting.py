"""Pure quote engine: market state in -> desired two-sided quote out. No I/O.

Model (port of arcus-mm, simplified to what measured best on io):
  * JOIN the touch at the back of the queue, $12 a side. The touch is the best
    price of OTHER participants (our own resting size removed), so we never
    end up alone at a price, i.e. quoting inside (that lost in the sample).
  * Back-offs, in ticks, on the side that would ADD inventory:
      skew       ceil(inventory_ratio * skew_ticks)
      toxicity   round((tox_mult - 1) * toxic_backoff_ticks)
      funding    funding_lean_ticks when the paying side would grow
  * Fair-value guard (xyz): gap = (fair - io_mid) / io_mid. gap > +g: io is
    about to rise, back the ASK off; gap < -g: back the BID off. Past
    guard_pull_bps the endangered side is pulled unless it reduces inventory.
  * Hard inventory cap: the adding side is pulled; the reducing side is always
    quoted. Volatility guard tripped: only the reducing side.
  * Flatten-only (budget, pre-blackout, off-session): one reduce-only quote at
    the touch for the whole position.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .config import MarketCfg
from .meta import MIN_ORDER_USD, AssetMeta

D = Decimal
Level = tuple[Decimal, Decimal]


@dataclass
class Side:
    price: Decimal
    size: Decimal
    reduce_only: bool = False


@dataclass
class Quote:
    bid: Side | None
    ask: Side | None
    note: str = ""
    inv_ratio: float = 0.0


@dataclass
class QuoteInputs:
    bids: list[Level]                 # best first, as on the exchange (our own size included)
    asks: list[Level]
    own_bid: Level | None             # (price, remaining) of our resting bid
    own_ask: Level | None
    position: Decimal
    regime: str                       # session name, "flatten" or "blackout"
    session_ok: bool = True           # this market quotes in this session (quote_regimes)
    flatten_only: bool = False
    toxicity_mult: float = 1.0
    gap_bps: float | None = None      # None: fair value not available (stale or warming up)
    vol_tripped: bool = False
    funding_apr: float = 0.0


def touch_ex_own(levels: list[Level], own: Level | None) -> Decimal | None:
    """Best price resting from someone other than us."""
    for px, sz in levels:
        if own is not None and px == own[0]:
            sz = sz - own[1]
        if sz > 0:
            return px
    return None


def compute_quote(x: QuoteInputs, meta: AssetMeta, cfg: MarketCfg) -> Quote:
    if x.regime == "blackout":
        return Quote(None, None, "blackout")
    if not x.bids or not x.asks:
        return Quote(None, None, "empty book")
    best_bid, best_ask = x.bids[0][0], x.asks[0][0]
    if best_bid <= 0 or best_bid >= best_ask:
        return Quote(None, None, "bad book")
    tb = touch_ex_own(x.bids, x.own_bid)
    ta = touch_ex_own(x.asks, x.own_ask)
    if tb is None or ta is None:
        return Quote(None, None, "no other liquidity at a touch")
    mid = (best_bid + best_ask) / 2
    tick = meta.tick_for(mid)
    pos = x.position
    inv_usd = float(pos * mid)
    max_inv = max(cfg.max_inventory_usd, cfg.clip_usd)
    ratio = max(-1.5, min(1.5, inv_usd / max_inv))

    # -------- flatten-only: one reduce-only quote at the touch ------------------
    if x.flatten_only or x.regime == "flatten" or not x.session_ok:
        why = "flatten-only" if x.flatten_only else ("pre-blackout" if x.regime == "flatten" else "off-session")
        if pos == 0:
            return Quote(None, None, f"flat ({why})", ratio)
        if abs(inv_usd) < float(MIN_ORDER_USD):
            return Quote(None, None, f"dust {pos} below the $10 minimum ({why})", ratio)
        size = abs(pos)
        if pos > 0:
            return Quote(None, Side(max(ta, best_bid + tick), size, True), why, ratio)
        return Quote(Side(min(tb, best_ask - tick), size, True), None, why, ratio)

    # -------- normal two-sided quote --------------------------------------------
    if x.gap_bps is None:
        return Quote(None, None, "no fair value (xyz stale or premium warming up)", ratio)
    reduce_bid, reduce_ask = pos < 0, pos > 0
    back_b = back_a = 0
    if ratio > 0:
        back_b += math.ceil(ratio * cfg.skew_ticks - 1e-9)
        if ratio >= cfg.soft_inventory_frac:
            back_b += 1
    elif ratio < 0:
        back_a += math.ceil(-ratio * cfg.skew_ticks - 1e-9)
        if -ratio >= cfg.soft_inventory_frac:
            back_a += 1
    tox = int(round((x.toxicity_mult - 1.0) * cfg.toxic_backoff_ticks))
    if tox > 0:                                 # reducing side exempt (Arcus 2026-10-16)
        if not reduce_bid:
            back_b += tox
        if not reduce_ask:
            back_a += tox
    if cfg.funding_lean_apr > 0:
        if x.funding_apr >= cfg.funding_lean_apr and not reduce_bid:
            back_b += cfg.funding_lean_ticks    # longs pay: don't grow a long
        elif x.funding_apr <= -cfg.funding_lean_apr and not reduce_ask:
            back_a += cfg.funding_lean_ticks
    want_b = want_a = True
    notes = []
    g = x.gap_bps
    if g > cfg.guard_bps:                        # io cheap vs xyz: our ask gets lifted
        back_a += cfg.guard_backoff_ticks
        if g > cfg.guard_pull_bps and not reduce_ask:
            want_a = False
        notes.append(f"guard ask {g:+.1f}bp")
    elif g < -cfg.guard_bps:
        back_b += cfg.guard_backoff_ticks
        if g < -cfg.guard_pull_bps and not reduce_bid:
            want_b = False
        notes.append(f"guard bid {g:+.1f}bp")
    if x.vol_tripped:
        want_b, want_a = want_b and reduce_bid, want_a and reduce_ask
        notes.append("vol guard")

    b = tb - tick * back_b
    a = ta + tick * back_a
    b = min(b, best_ask - tick)                   # post-only: never cross
    a = max(a, best_bid + tick)
    if b >= a:
        return Quote(None, None, "quotes collapsed", ratio)
    clip_b = meta.size_for_usd(cfg.clip_usd, b)
    clip_a = meta.size_for_usd(cfg.clip_usd, a)
    # nominal clip + 1% slack: size rounding makes a $12 clip worth $12.02, and
    # 2 x $12.02 must still fit a $24 cap
    lim = max_inv * 1.01
    can_buy = reduce_bid or inv_usd + cfg.clip_usd <= lim
    can_sell = reduce_ask or -inv_usd + cfg.clip_usd <= lim
    if not can_buy or not can_sell:
        notes.append("inventory cap")
    return Quote(Side(b, clip_b) if (want_b and can_buy) else None,
                 Side(a, clip_a) if (want_a and can_sell) else None,
                 ", ".join(notes), ratio)
