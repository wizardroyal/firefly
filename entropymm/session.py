"""US Eastern session calendar for io markets, and the loss-budget clock.

Sessions (ET):
  rth        09:30-16:00 on trading days
  pre        04:00-09:30 on trading days
  post       16:00-20:00 on trading days
  overnight  20:00-04:00 between two trading days (Sun 20:00 -> Mon 04:00 too)
  weekend    Fri 20:00 -> Sun 20:00, and whole exchange holidays
Blackouts (config `bot.blackouts`) are transition windows where io can gap
(oracle switch, US open/close, Sunday reopen): no quotes inside, and
flatten-only for `flatten_lead_min` before each one. Which sessions are worth
quoting is decided per market by `quote_regimes`, from Phase 0 recordings.

Budgets: the week starts Monday 00:00 UTC, the day at 00:00 UTC (01:00 WAT).
Reports are shown in WAT (UTC+1, no DST).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
WAT = timezone(timedelta(hours=1), "WAT")

HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def _et(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(ET)


def session(now: datetime | None = None) -> str:
    t_et = _et(now)
    d, t = t_et.date(), t_et.time()
    if is_trading_day(d):
        if time(9, 30) <= t < time(16, 0):
            return "rth"
        if time(4, 0) <= t < time(9, 30):
            return "pre"
        if time(16, 0) <= t < time(20, 0):
            return "post"
        if t >= time(20, 0):
            return "overnight" if is_trading_day(d + timedelta(days=1)) else "weekend"
        return "overnight"                 # before 04:00 on a trading day
    # non-trading day: weekend, except Sunday (or holiday) evening before a trading day
    if t >= time(20, 0) and is_trading_day(d + timedelta(days=1)):
        return "overnight"
    return "weekend"


@dataclass(frozen=True)
class Window:
    sunday: bool
    start: time
    end: time

    @classmethod
    def parse(cls, spec: str) -> "Window":
        s = spec.strip().lower()
        sunday = s.startswith("sun ")
        if sunday:
            s = s[4:].strip()
        a, b = s.split("-")
        ha, ma = (int(x) for x in a.split(":"))
        hb, mb = (int(x) for x in b.split(":"))
        return cls(sunday, time(ha, ma), time(hb, mb))

    def applies(self, d: date) -> bool:
        return d.weekday() == 6 if self.sunday else is_trading_day(d)

    def bounds(self, d: date) -> tuple[datetime, datetime]:
        return datetime.combine(d, self.start, ET), datetime.combine(d, self.end, ET)


class Calendar:
    def __init__(self, blackouts: list[str], flatten_lead_min: float):
        self.windows = [Window.parse(b) for b in blackouts]
        self.lead = timedelta(minutes=flatten_lead_min)

    def _upcoming(self, now: datetime):
        t = _et(now)
        for dd in (-1, 0, 1):
            d = t.date() + timedelta(days=dd)
            for w in self.windows:
                if w.applies(d):
                    yield w.bounds(d)

    def in_blackout(self, now: datetime | None = None) -> bool:
        t = _et(now)
        return any(a <= t < b for a, b in self._upcoming(t))

    def minutes_to_blackout(self, now: datetime | None = None) -> float | None:
        """Minutes until the next blackout starts if it is within flatten_lead_min, else None."""
        t = _et(now)
        best = None
        for a, _ in self._upcoming(t):
            if t < a <= t + self.lead:
                m = (a - t).total_seconds() / 60
                best = m if best is None else min(best, m)
        return best

    def regime(self, now: datetime | None = None) -> str:
        """session name, or 'blackout' / 'flatten' (pre-blackout lead)."""
        if self.in_blackout(now):
            return "blackout"
        if self.minutes_to_blackout(now) is not None:
            return "flatten"
        return session(now)


def week_start(now: datetime | None = None) -> datetime:
    n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    d = datetime.combine(n.date(), time(0), timezone.utc)
    return d - timedelta(days=d.weekday())


def day_start(now: datetime | None = None) -> datetime:
    n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return datetime.combine(n.date(), time(0), timezone.utc)
