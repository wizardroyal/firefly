"""Loss budgets, persisted across restarts (port of arcus-mm risk.py).

`pnl` is the bot ledger's PnL (fills + fees + funding, valued at the
exchange position). Rules carried over from Arcus, each learned the hard way:
  * a breach must show on TWO consecutive readings before it halts; the first
    one goes flatten-only (a single bad snapshot must not halt a whole week);
  * an unreadable state file halts forever: never hand back a fresh budget;
  * set_basis carries the used loss over to a new basis;
  * halts are persisted, so a restart never clears them.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .config import RiskCfg
from .session import day_start, week_start

log = logging.getLogger("emm.risk")

HALT_FOREVER = datetime(9999, 1, 1, tzinfo=timezone.utc)


class Mode(str, Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    FLATTEN_ONLY = "flatten"
    HALT = "halt"


@dataclass
class RiskView:
    mode: Mode
    week_loss: float
    day_loss: float
    reason: str = ""


@dataclass
class MarketView:
    mode: Mode
    week_loss: float
    reason: str = ""


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


class RiskManager:
    def __init__(self, cfg: RiskCfg, state_dir: str, tag: str):
        self.cfg = cfg
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, f"risk_{tag}.json")
        self.s = self._load()
        self._breach_seen = False
        self._mkt_breach: set[str] = set()

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                s = json.load(f)
            self._validate(s)
            return s
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, KeyError) as e:
            aside = f"{self.path}.corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
            try:
                os.replace(self.path, aside)
            except OSError:
                aside = self.path
            reason = f"risk state {self.path} unreadable ({e}); saved as {aside}. Restore it, then delete halt_until"
            log.error("HALT: %s", reason)
            return {"halt_until": HALT_FOREVER.isoformat(), "halt_reason": reason}

    @staticmethod
    def _validate(s) -> None:
        if not isinstance(s, dict):
            raise TypeError("not a JSON object")
        if "halt_until" in s:
            datetime.fromisoformat(s["halt_until"])
        if "week_start" in s or "day_start" in s:      # a halt-only file has neither
            for k in ("week_start", "day_start"):
                datetime.fromisoformat(s[k])
            for k in ("week_base", "day_base"):
                if not _num(s[k]):
                    raise TypeError(f"{k} is not a number")
        if "last_pnl" in s and not _num(s["last_pnl"]):
            raise TypeError("last_pnl is not a number")
        for sym, m in s.get("markets", {}).items():
            datetime.fromisoformat(m["week_start"])
            if not _num(m["week_base"]):
                raise TypeError(f"markets.{sym}.week_base is not a number")
            if "halt_until" in m:
                datetime.fromisoformat(m["halt_until"])

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.s, f, indent=1)
        os.replace(tmp, self.path)

    def set_basis(self, basis: str, pnl: float) -> None:
        """Switch what `pnl` measures WITHOUT resetting the budget: used loss carries over."""
        old = self.s.get("basis")
        if old == basis:
            return
        if old is not None and "last_pnl" in self.s and "week_base" in self.s:
            wl = self.s["week_base"] - self.s["last_pnl"]
            dl = self.s["day_base"] - self.s["last_pnl"]
            self.s.update(week_base=pnl + wl, day_base=pnl + dl, last_pnl=pnl)
            log.warning("loss budget basis %s -> %s: carrying week loss %.4f, day loss %.4f", old, basis, wl, dl)
        self.s["basis"] = basis
        self._save()

    def halt(self, reason: str, until: datetime) -> None:
        self.s["halt_until"] = until.isoformat()
        self.s["halt_reason"] = reason
        self._save()
        log.error("HALT until %s: %s", until.isoformat(), reason)

    def update(self, pnl: float, now: datetime | None = None) -> RiskView:
        now = now or datetime.now(timezone.utc)
        ws, ds = week_start(now), day_start(now)
        if self.s.get("week_start") != ws.isoformat():
            self.s.update(week_start=ws.isoformat(), week_base=pnl)
        if self.s.get("day_start") != ds.isoformat():
            self.s.update(day_start=ds.isoformat(), day_base=pnl)
        self.s["last_pnl"] = pnl
        self._save()
        return self._view(pnl, now, ws, ds)

    def _view(self, pnl: float, now: datetime, ws: datetime, ds: datetime) -> RiskView:
        wl = self.s["week_base"] - pnl
        dl = self.s["day_base"] - pnl
        c = self.cfg
        hu = self.s.get("halt_until")
        if hu and datetime.fromisoformat(hu) > now:
            return RiskView(Mode.HALT, wl, dl, self.s.get("halt_reason", ""))
        breach = None
        if wl >= c.weekly_loss_limit_usd:
            breach = ("weekly", f"weekly loss {wl:.3f} >= {c.weekly_loss_limit_usd}", ws + timedelta(days=7))
        elif dl >= c.daily_loss_limit_usd:
            breach = ("daily", f"daily loss {dl:.3f} >= {c.daily_loss_limit_usd}", ds + timedelta(days=1))
        if breach:
            kind, why, until = breach
            if not self._breach_seen:
                self._breach_seen = True
                log.warning("unconfirmed %s; flatten-only until the next reading confirms it", why)
                return RiskView(Mode.FLATTEN_ONLY, wl, dl, f"unconfirmed {kind} limit")
            self.halt(why, until)
            return RiskView(Mode.HALT, wl, dl, f"{kind} limit")
        self._breach_seen = False
        if wl >= c.weekly_loss_limit_usd * c.flatten_only_frac:
            return RiskView(Mode.FLATTEN_ONLY, wl, dl, "near weekly limit")
        if c.caution_frac < 1 and wl >= c.weekly_loss_limit_usd * c.caution_frac:
            return RiskView(Mode.CAUTION, wl, dl, "budget past caution_frac")
        return RiskView(Mode.NORMAL, wl, dl)

    def update_market(self, sym: str, pnl: float, limit: float, now: datetime | None = None) -> MarketView:
        now = now or datetime.now(timezone.utc)
        ws = week_start(now)
        mk = self.s.setdefault("markets", {})
        m = mk.get(sym)
        if m is None or m.get("week_start") != ws.isoformat():
            m = mk[sym] = {"week_start": ws.isoformat(), "week_base": pnl}
            self._mkt_breach.discard(sym)
        loss = m["week_base"] - pnl
        hu = m.get("halt_until")
        if hu and datetime.fromisoformat(hu) > now:
            self._save()
            return MarketView(Mode.HALT, loss, m.get("halt_reason", ""))
        mode, why = Mode.NORMAL, ""
        if limit > 0 and loss >= limit:
            if sym not in self._mkt_breach:
                self._mkt_breach.add(sym)
                mode, why = Mode.FLATTEN_ONLY, f"unconfirmed {sym} limit"
            else:
                why = f"{sym} weekly loss {loss:.3f} >= {limit}"
                m.update(halt_until=(ws + timedelta(days=7)).isoformat(), halt_reason=why)
                log.error("HALT %s until the week ends: %s", sym, why)
                mode = Mode.HALT
        else:
            self._mkt_breach.discard(sym)
            if limit > 0 and loss >= limit * self.cfg.flatten_only_frac:
                mode, why = Mode.FLATTEN_ONLY, f"near {sym} limit"
        self._save()
        return MarketView(mode, loss, why)

    def headroom(self, pnl: float) -> float:
        return self.cfg.weekly_loss_limit_usd - (self.s.get("week_base", pnl) - pnl)
