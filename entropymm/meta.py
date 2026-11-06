"""Per-asset metadata and Hyperliquid price/size rules.

Verified live 2026-11-06 (perpDexs + metaAndAssetCtxs dex "io"):
  * io is perpDexs[10]; the SDK maps builder dex k (1-based in that list) to
    asset offset 100000 + k * 10000, so io:SNDK (universe index 2) = 200002.
  * prices: at most 5 significant figures and at most (6 - szDecimals)
    decimals; integer prices are always allowed. Sizes: szDecimals.
    io:SNDK szDecimals 4, price ~828 -> tick 0.01 (~0.12 bp).
  * universe rows carry growthMode ("enabled"), isDelisted, onlyIsolated,
    marginMode; asset ctxs carry markPx, oraclePx, midPx, funding (hourly).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

D = Decimal
MIN_ORDER_USD = D(10)


def asset_id(dex_index: int, index_in_dex: int) -> int:
    """dex_index: position in the perpDexs list (0 = the main dex, which is null there)."""
    if dex_index == 0:
        return index_in_dex
    return 100000 + dex_index * 10000 + index_in_dex


def price_decimals(price: Decimal, sz_decimals: int) -> int:
    """Decimals allowed at this price: min(6 - szDecimals, 5 - integer digits), >= 0."""
    p = abs(price)
    int_digits = len(str(int(p))) if p >= 1 else 0
    return max(0, min(6 - sz_decimals, 5 - int_digits))


def tick_for(price: Decimal, sz_decimals: int) -> Decimal:
    return D(1).scaleb(-price_decimals(price, sz_decimals))


def snap_price(price: Decimal, sz_decimals: int, up: bool) -> Decimal:
    """Round to a valid price, toward +inf if up else toward -inf. Crossing a
    power of ten changes the tick, so re-snap once at the result's precision."""
    rnd = ROUND_CEILING if up else ROUND_FLOOR
    out = price.quantize(tick_for(price, sz_decimals), rounding=rnd)
    out2 = out.quantize(tick_for(out, sz_decimals), rounding=rnd)
    return out2


def wire(x: Decimal) -> str:
    """Canonical number string for the exchange (no exponent, no trailing zeros).
    The signature covers the exact string, so it must match the SDK's float_to_wire."""
    s = f"{x.normalize():f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


@dataclass
class AssetMeta:
    coin: str
    asset: int
    sz_decimals: int
    max_leverage: int
    only_isolated: bool = True
    growth_mode: str | None = None
    delisted: bool = False
    mark: Decimal = D(0)
    oracle: Decimal = D(0)
    funding: float = 0.0                  # hourly rate; >0 = longs pay
    mid_present: bool = True              # ctx midPx; None while halted or empty

    @property
    def step(self) -> Decimal:
        return D(1).scaleb(-self.sz_decimals)

    def tick_for(self, price: Decimal) -> Decimal:
        return tick_for(price, self.sz_decimals)

    def snap(self, price: Decimal, up: bool) -> Decimal:
        return snap_price(price, self.sz_decimals, up)

    def round_size(self, size: Decimal, up: bool = False) -> Decimal:
        return size.quantize(self.step, rounding=ROUND_CEILING if up else ROUND_FLOOR)

    def size_for_usd(self, usd: float, price: Decimal) -> Decimal:
        """Smallest size worth >= max(usd, $10) at `price`."""
        if price <= 0:
            return D(0)
        target = max(D(str(usd)), MIN_ORDER_USD)
        return self.round_size(target / price, up=True)

    def update_ctx(self, u: dict, c: dict) -> None:
        self.growth_mode = u.get("growthMode")
        self.delisted = bool(u.get("isDelisted"))
        self.only_isolated = bool(u.get("onlyIsolated", self.only_isolated))
        self.mark = D(c.get("markPx") or 0)
        self.oracle = D(c.get("oraclePx") or 0)
        self.funding = float(c.get("funding") or 0)
        self.mid_present = c.get("midPx") is not None

    @property
    def funding_apr(self) -> float:
        return self.funding * 24 * 365


def build_metas(perp_dexs: list, dex_meta: dict[str, tuple[dict, list]]) -> dict[str, AssetMeta]:
    """perp_dexs: info{type: perpDexs}; dex_meta: {dex: metaAndAssetCtxs(dex)}."""
    idx = {d["name"]: i for i, d in enumerate(perp_dexs) if d}
    out = {}
    for dex, (meta, ctxs) in dex_meta.items():
        di = idx.get(dex, 0) if dex else 0
        for i, (u, c) in enumerate(zip(meta["universe"], ctxs)):
            m = AssetMeta(u["name"], asset_id(di, i), int(u["szDecimals"]), int(u.get("maxLeverage", 1)))
            m.update_ctx(u, c)
            out[u["name"]] = m
    return out
