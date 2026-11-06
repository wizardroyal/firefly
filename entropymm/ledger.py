"""Bot-only PnL: a persisted cash ledger of the bot's own markets.

    bot pnl = sum over markets of (cash) + position * mark
    cash   += -side * size * price   per fill (deduped by tid)
    cash   -= fee                    per fill (negative fee = rebate)
    cash   += funding usdc           per hourly funding payment (deduped by time+coin)

Fees and funding are ALSO kept as their own running totals, so reports show
what volume cost in fees vs. adverse selection vs. funding. A market first
seen with an open position starts at zero PnL (cash = -position * mark).
The bot runs on a dedicated subaccount, so every fill in its markets is its own.
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

log = logging.getLogger("emm.ledger")

SEEN_KEEP = 5000


class LedgerError(RuntimeError):
    pass


def _new_market(cash: float = 0.0, start_ms: int = 0) -> dict:
    return {"cash": cash, "start_ms": start_ms, "fees": 0.0, "funding": 0.0, "volume": 0.0, "taker_volume": 0.0, "fills": 0}


class Ledger:
    def __init__(self, state_dir: str, tag: str):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, f"ledger_{tag}.json")
        self.s = self._load()
        self._seen = set(self.s["seen"])
        self._fseen = set(self.s["funding_seen"])

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                s = json.load(f)
        except FileNotFoundError:
            return {"markets": {}, "seen": [], "funding_seen": [], "last_fill_ms": 0, "last_funding_ms": 0}
        except (OSError, ValueError) as e:
            raise LedgerError(f"{self.path} unreadable ({e})") from None
        if not (isinstance(s, dict) and isinstance(s.get("markets"), dict) and isinstance(s.get("seen"), list)
                and all(isinstance(m.get("cash"), (int, float)) for m in s["markets"].values())):
            raise LedgerError(f"{self.path} has an unexpected shape")
        s.setdefault("funding_seen", [])
        s.setdefault("last_fill_ms", 0)
        s.setdefault("last_funding_ms", 0)
        for m in s["markets"].values():
            for k, v in _new_market().items():
                m.setdefault(k, v)
        return s

    def save(self) -> None:
        self.s["seen"] = self.s["seen"][-SEEN_KEEP:]
        self.s["funding_seen"] = self.s["funding_seen"][-SEEN_KEEP:]
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.s, f)
        os.replace(tmp, self.path)

    def is_new(self, sym: str) -> bool:
        return sym not in self.s["markets"]

    def init_market(self, sym: str, position: Decimal, mark: Decimal, start_ms: int, seen_tids=()) -> None:
        """Start at zero PnL from `position`; fills before start_ms are already in it."""
        self.s["markets"][sym] = _new_market(-float(position) * float(mark), int(start_ms))
        for tid in seen_tids:
            self._mark(self._seen, self.s["seen"], str(tid))
        self.save()
        log.info("ledger: tracking %s from position %s @ %s", sym, position, mark)

    def apply_fill(self, sym: str, is_buy: bool, price: Decimal, size: Decimal, fee: Decimal,
                   tid: str, ts_ms: int = 0, crossed: bool = False) -> bool:
        """Book one fill. False if not a bot market or already booked."""
        m = self.s["markets"].get(sym)
        tid = str(tid)
        if m is None or not tid or tid in self._seen:
            return False
        if ts_ms and ts_ms < m.get("start_ms", 0):
            return False                      # already inside the starting position
        sgn = 1 if is_buy else -1
        notional = float(size) * float(price)
        m["cash"] += -sgn * notional - float(fee)
        m["fees"] += float(fee)
        m["volume"] += notional
        m["fills"] += 1
        if crossed:
            m["taker_volume"] += notional
        self._mark(self._seen, self.s["seen"], tid)
        if ts_ms:
            self.s["last_fill_ms"] = max(self.s["last_fill_ms"], int(ts_ms))
        return True

    def apply_funding(self, sym: str, usdc: float, ts_ms: int) -> bool:
        m = self.s["markets"].get(sym)
        key = f"{sym}:{int(ts_ms)}"
        if m is None or key in self._fseen or ts_ms < m.get("start_ms", 0):
            return False
        m["cash"] += usdc
        m["funding"] += usdc
        self._mark(self._fseen, self.s["funding_seen"], key)
        self.s["last_funding_ms"] = max(self.s["last_funding_ms"], int(ts_ms))
        return True

    @staticmethod
    def _mark(seen: set, lst: list, key: str) -> None:
        if key not in seen:
            seen.add(key)
            lst.append(key)
            if len(lst) > 2 * SEEN_KEEP:
                drop = lst[:-SEEN_KEEP]
                del lst[:-SEEN_KEEP]
                seen.difference_update(drop)

    def pnl(self, marks: dict) -> float | None:
        by = self.pnl_by_market(marks)
        return None if by is None else sum(by.values())

    def pnl_by_market(self, marks: dict) -> dict[str, float] | None:
        """marks: {sym: (position, mark)}. None if a market with a position has no mark."""
        out = {}
        for sym, m in self.s["markets"].items():
            pos, mark = marks.get(sym, (Decimal(0), Decimal(0)))
            if pos != 0 and mark <= 0:
                return None
            out[sym] = m["cash"] + float(pos) * float(mark)
        return out

    def totals(self, sym: str) -> dict:
        return dict(self.s["markets"].get(sym, _new_market()))
