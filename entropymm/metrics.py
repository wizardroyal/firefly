"""Event log (JSONL), raw frame dump, and the weekly/daily report.

The numbers that matter for a volume bot: volume, fees, funding, taker
volume, markouts (vs io mid and vs xyz fair), and cost per $ of volume.
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timezone


class EventLog:
    def __init__(self, log_dir: str, tag: str, on_write=None):
        os.makedirs(log_dir, exist_ok=True)
        self.dir, self.tag = log_dir, tag
        self.on_write = on_write

    def write(self, kind: str, **data) -> None:
        now = datetime.now(timezone.utc)
        path = os.path.join(self.dir, f"{self.tag}-{now:%Y%m%d}.jsonl")
        rec = {"ts": now.isoformat(timespec="milliseconds"), "kind": kind, **data}
        with open(path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        if self.on_write:
            try:
                self.on_write(kind, data)
            except Exception:
                pass


class FrameDump:
    """--dump-frames: raw WS messages and REST write responses (never the key)."""

    def __init__(self, log_dir: str, name: str = "frames.jsonl"):
        os.makedirs(log_dir, exist_ok=True)
        self._f = open(os.path.join(log_dir, name), "a", buffering=1)

    def __call__(self, src: str, **data) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "src": src, **data}
        self._f.write(json.dumps(rec, default=str) + "\n")

    def close(self) -> None:
        self._f.close()


def _mean(v):
    return sum(v) / len(v) if v else None


def summarize(log_dir: str, tag: str, since: str | None = None) -> dict:
    """Aggregate fills/markouts/funding from event logs at or after `since` (ISO UTC)."""
    files = sorted(f for f in glob.glob(os.path.join(log_dir, f"{tag}-*.jsonl")))
    by = defaultdict(lambda: {"fills": 0, "volume": 0.0, "fees": 0.0, "taker_volume": 0.0, "funding": 0.0,
                              "by_session": defaultdict(float)})
    marks = defaultdict(list)            # (sym, ref, h) -> bps
    marks_sess = defaultdict(list)       # (sym, ref, session, h)
    last_status, flattens, rejects, bad = None, 0, defaultdict(int), 0
    for fp in files:
        for line in open(fp):
            try:
                e = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if since and e.get("ts", "") < since:
                continue
            k = e.get("kind")
            if k == "fill":
                m = by[e["symbol"]]
                m["fills"] += 1
                m["volume"] += e["notional"]
                m["fees"] += float(e.get("fee") or 0)
                if e.get("crossed"):
                    m["taker_volume"] += e["notional"]
                m["by_session"][e.get("session", "?")] += e["notional"]
            elif k == "funding":
                by[e["symbol"]]["funding"] += float(e["usdc"])
            elif k == "markout":
                marks[(e["symbol"], e.get("ref", "mid"), e["h"])].append(e["bps"])
                marks_sess[(e["symbol"], e.get("ref", "mid"), e.get("session", "?"), e["h"])].append(e["bps"])
            elif k in ("status", "stop"):
                last_status = e
            elif k == "flatten":
                flattens += 1
            elif k == "reject":
                rejects[e.get("reason", "?")[:60]] += 1
    out = {"markets": {}, "flattens": flattens, "rejects": dict(rejects), "bad_lines": bad,
           "last_status": last_status}
    for sym, m in by.items():
        mo = {f"{ref}{h}": _mean(marks.get((sym, ref, h))) for ref in ("mid", "fair") for h in (5, 30, 120)}
        mo_n = len(marks.get((sym, "fair", 30), []))
        sess = {}
        for (s2, ref, ses, h), v in marks_sess.items():
            if s2 == sym and h == 30:
                sess.setdefault(ses, {})[f"{ref}30"] = (round(_mean(v), 3), len(v))
        out["markets"][sym] = {**{k: v for k, v in m.items() if k != "by_session"},
                               "by_session": dict(m["by_session"]), "markouts": mo, "n_fair30": mo_n,
                               "markouts_by_session": sess}
    return out


def format_report(s: dict, week_loss: float | None = None) -> str:
    lines = []
    tot_v = sum(m["volume"] for m in s["markets"].values())
    hdr = f"{'market':<10}{'fills':>6}{'volume':>10}{'fees':>9}{'fund':>8}{'taker$':>8}" \
          f"{'mid30':>7}{'mid120':>7}{'fair30':>7}{'fair120':>8}"
    lines.append(hdr)
    for sym, m in sorted(s["markets"].items(), key=lambda kv: -kv[1]["volume"]):
        mo = m["markouts"]
        f = lambda x: "-" if x is None else f"{x:+.2f}"   # noqa: E731
        lines.append(f"{sym:<10}{m['fills']:>6}{m['volume']:>10,.0f}{m['fees']:>9.4f}{m['funding']:>8.4f}"
                     f"{m['taker_volume']:>8,.0f}{f(mo['mid30']):>7}{f(mo['mid120']):>7}{f(mo['fair30']):>7}"
                     f"{f(mo['fair120']):>8}")
        if m["by_session"]:
            lines.append("   volume by session: " + ", ".join(f"{k} ${v:,.0f}" for k, v in m["by_session"].items()))
        if m["markouts_by_session"]:
            lines.append("   30s markouts by session (bp, n): " + ", ".join(
                f"{ses} {v}" for ses, v in m["markouts_by_session"].items()))
    lines.append(f"total volume ${tot_v:,.0f}   emergency flattens {s['flattens']}   rejects {s['rejects']}")
    if week_loss is not None and tot_v:
        lines.append(f"loss ${week_loss:.3f} on ${tot_v:,.0f} = {week_loss / tot_v * 1e4:.2f} bp per $ of volume")
    return "\n".join(lines)
