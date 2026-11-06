"""Online estimators: volatility, fair value off xyz, fill markouts (toxicity)."""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass


class EwmaVol:
    """Volatility of mid in bps per sqrt(second), EWMA of squared log-returns."""

    def __init__(self, halflife_s: float = 300.0, prior_bps: float = 0.5, max_bps: float = 50.0):
        self.hl = halflife_s
        self.var = prior_bps ** 2
        self.max = max_bps
        self.n = 0
        self._last: tuple[float, float] | None = None

    def update(self, mid: float, t: float | None = None) -> None:
        t = t if t is not None else time.monotonic()
        if mid <= 0:
            return
        if self._last is None:
            self._last = (t, mid)
            return
        t0, m0 = self._last
        dt = t - t0
        if dt < 0.5:
            return
        r_bps = math.log(mid / m0) * 1e4
        inst = min(r_bps * r_bps / dt, (self.max ** 2) * 4)
        a = 1 - 0.5 ** (dt / self.hl)
        self.var += a * (inst - self.var)
        self.n += 1
        self._last = (t, mid)

    @property
    def sigma_bps(self) -> float:
        return min(math.sqrt(max(self.var, 0.0)), self.max)


class VolGuard:
    """fast sigma vs slow ("typical") sigma. Event nights cost Arcus ~1.3x on
    every market and it had no such guard; here only the reducing side is
    quoted while fast >= mult * slow."""

    def __init__(self, mult: float, fast_hl: float = 300.0, slow_hl: float = 6 * 3600.0, warmup: int = 120):
        self.fast = EwmaVol(fast_hl)
        self.slow = EwmaVol(slow_hl)
        self.mult = mult
        self.warmup = warmup

    def update(self, mid: float, t: float | None = None) -> None:
        self.fast.update(mid, t)
        self.slow.update(mid, t)

    @property
    def tripped(self) -> bool:
        if self.mult <= 0 or self.fast.n < self.warmup:
            return False
        return self.fast.sigma_bps >= self.mult * self.slow.sigma_bps


class FairValue:
    """fair = xyz_mid * premium, premium = time-EWMA of io_mid / xyz_mid.

    xyz (trade.xyz) has 10-100x io's volume on the same stocks and io follows
    it with a short lag plus a persistent premium, so this is the price io is
    about to move to. gap_bps > 0 means io is cheap vs fair (an ask is in danger).
    """

    def __init__(self, halflife_s: float = 120.0, warmup_s: float = 60.0):
        self.hl = halflife_s
        self.warmup = warmup_s
        self.premium: float | None = None
        self._t: float | None = None
        self._t0: float | None = None

    def update(self, io_mid: float, ref_mid: float, t: float | None = None) -> None:
        t = t if t is not None else time.monotonic()
        if io_mid <= 0 or ref_mid <= 0:
            return
        r = io_mid / ref_mid
        if self.premium is None:
            self.premium, self._t, self._t0 = r, t, t
            return
        dt = max(0.0, t - self._t)
        a = 1 - 0.5 ** (dt / self.hl) if self.hl > 0 else 1.0
        self.premium += a * (r - self.premium)
        self._t = t

    def warm(self, t: float | None = None) -> bool:
        t = t if t is not None else time.monotonic()
        return self.premium is not None and t - self._t0 >= self.warmup

    def fair(self, ref_mid: float) -> float | None:
        if self.premium is None or ref_mid <= 0:
            return None
        return ref_mid * self.premium

    def gap_bps(self, io_mid: float, ref_mid: float) -> float | None:
        f = self.fair(ref_mid)
        if f is None or io_mid <= 0:
            return None
        return (f - io_mid) / io_mid * 1e4


@dataclass
class _Pending:
    t: float
    sign: int
    price: float
    notional: float
    done: set
    tag: str = ""
    ref: str = ""


class Markouts:
    """markout_h = sign * (px(t+h) - fill_px) / fill_px in bps. Positive = good.

    One instance PER MARKET and per reference price (io mid, fair): a shared
    tracker marks fills against the wrong market's price. The toxicity
    multiplier decays with a half-life: without decay it latched at its max
    on Arcus (backed-off quotes stop getting fills, so no new evidence) and
    quoting stalled.
    """
    HORIZONS = (5.0, 30.0, 120.0)

    def __init__(self, toxic_bps: float, kill_bps: float, window: int = 20, kill_window: int = 50,
                 on_done=None, halflife_s: float = 600.0, adaptive: bool = True):
        self.on_done = on_done
        self.halflife = halflife_s
        self.adaptive = adaptive
        self._t_decay: float | None = None
        self.toxic = toxic_bps
        self.kill = kill_bps
        self.pending: list[_Pending] = []
        self.done: dict[float, deque] = {h: deque(maxlen=max(window, kill_window, 200)) for h in self.HORIZONS}
        self.window, self.kill_window = window, kill_window
        self.mult = 1.0

    def on_fill(self, sign: int, price: float, notional: float, t: float | None = None,
                tag: str = "", ref: str = "") -> None:
        t = t if t is not None else time.monotonic()
        self.pending.append(_Pending(t, sign, price, notional, set(), tag, ref))

    def on_price(self, px: float, t: float | None = None) -> None:
        t = t if t is not None else time.monotonic()
        if self._t_decay is not None and self.halflife > 0 and self.mult > 1.0:
            self.mult = 1.0 + (self.mult - 1.0) * 0.5 ** ((t - self._t_decay) / self.halflife)
        self._t_decay = t
        if px <= 0:
            return
        keep = []
        for p in self.pending:
            for h in self.HORIZONS:
                if h not in p.done and t - p.t >= h:
                    mo = p.sign * (px - p.price) / p.price * 1e4
                    self.done[h].append(mo)
                    p.done.add(h)
                    if self.on_done:
                        self.on_done(h, mo, p.tag, p.notional, p.ref)
                    if h == 30.0 and self.adaptive:
                        self._adapt()
            if len(p.done) < len(self.HORIZONS):
                keep.append(p)
        self.pending = keep

    def mean(self, h: float, n: int) -> float | None:
        d = list(self.done[h])[-n:]
        return sum(d) / len(d) if len(d) >= min(n, 5) else None

    def _adapt(self) -> None:
        m = self.mean(30.0, self.window)
        if m is None:
            return
        if m < -self.toxic:
            self.mult = min(3.0, self.mult * 1.25)
        else:
            self.mult = max(1.0, self.mult * 0.97)

    @property
    def kill_signal(self) -> bool:
        if not self.adaptive or len(self.done[30.0]) < self.kill_window:
            return False
        m = self.mean(30.0, self.kill_window)
        return m is not None and m < -self.kill

    def summary(self, prefix: str = "mo") -> dict:
        out = {}
        for h in self.HORIZONS:
            m = self.mean(h, 50)
            out[f"{prefix}{int(h)}s_bps"] = round(m, 3) if m is not None else None
        if self.adaptive:
            out["tox_mult"] = round(self.mult, 3)
        return out
