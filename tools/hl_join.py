"""Back-of-queue JOIN simulation on hl_record.py recordings, with a guard sweep.

Runs the bot's own quote engine (entropymm.quoting.compute_quote) and queue
model (entropymm.queue) over a recording: $clip per side at the touch,
inventory cap, tick skew, the xyz fair-value guard at each threshold g
(or "off"). Per session it prints fills/h, $/h, actions/h, markouts at
30/120 s vs io mid AND vs fair, and the simulated PnL as cost bp of volume
(fees at the growth-mode maker rate included).

    python tools/hl_join.py rec-*.jsonl --coin io:SNDK --g off,1,1.5,2,3 [--session overnight,weekend]

TREAT AS OPTIMISTIC: Arcus's sim was 1.4x high on volume and 0.4-2 bp too
cheap. Cancels in front of us are assumed to come from ahead in the queue.
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropymm import queue as q                       # noqa: E402
from entropymm.config import MarketCfg                 # noqa: E402
from entropymm.estimators import FairValue             # noqa: E402
from entropymm.meta import AssetMeta                   # noqa: E402
from entropymm.quoting import QuoteInputs, compute_quote  # noqa: E402
from entropymm.session import session                  # noqa: E402

D = Decimal
HORIZONS = (30.0, 120.0)


def load(paths):
    rows = []
    for p in paths:
        for line in open(p):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    rows.sort(key=lambda r: r["t"])
    return rows


def simulate(rows, coin, ref, g, sessions, clip, max_inv, sz_dec, maker_bps, min_requote_s):
    cfg = MarketCfg(symbol=coin, clip_usd=clip, max_inventory_usd=max_inv)
    if g is not None:
        cfg.guard_bps = g
    meta = AssetMeta(coin, 0, sz_dec, 3)
    fv = FairValue(cfg.premium_halflife_s, cfg.premium_warmup_s)
    bids = asks = None
    ref_mid, ref_t, last_fair_t = 0.0, -1e9, -1e9
    pos, cash, fees = D(0), 0.0, 0.0
    orders = {"BUY": None, "SELL": None}           # (SimOrder, placed_t)
    st = defaultdict(lambda: {"secs": 0.0, "fills": 0, "vol": 0.0, "actions": 0, "pnl": 0.0,
                              "mo": defaultdict(list)})
    pending = []                                  # [t, sign, px, notional, session, done:set]
    prev_t, prev_val = None, 0.0

    def mid():
        return float(bids[0][0] + asks[0][0]) / 2 if bids and asks else 0.0

    def mark(t):
        m = mid()
        fair = fv.fair(ref_mid) if t - ref_t <= cfg.ref_stale_s and fv.warm(t) else None
        keep = []
        for p in pending:
            for h in HORIZONS:
                if h not in p[5] and t - p[0] >= h:
                    p[5].add(h)
                    if m > 0:
                        st[p[4]]["mo"][("mid", h)].append(p[1] * (m - p[2]) / p[2] * 1e4)
                    if fair:
                        st[p[4]]["mo"][("fair", h)].append(p[1] * (fair - p[2]) / p[2] * 1e4)
            if len(p[5]) < len(HORIZONS):
                keep.append(p)
        pending[:] = keep

    def fill(side, sz, px, t, ses):
        nonlocal pos, cash, fees
        sgn = 1 if side == "BUY" else -1
        szd = D(str(round(sz, 10)))
        pos += sgn * szd
        n = sz * px
        fee = n * maker_bps / 1e4
        cash -= sgn * n + fee
        fees += fee
        s = st[ses]
        s["fills"] += 1
        s["vol"] += n
        pending.append([t, sgn, px, n, ses, set()])

    for r in rows:
        t, ch, d = r["t"], r["ch"], r["d"]
        ses = session(datetime.fromtimestamp(t, timezone.utc))
        # account time + PnL change to the session it happened in
        if prev_t is not None and bids and asks:
            st[ses]["secs"] += min(t - prev_t, 10.0)
            val = cash + float(pos) * mid()
            st[ses]["pnl"] += val - prev_val
            prev_val = val
        elif bids and asks:
            prev_val = cash + float(pos) * mid()
        prev_t = t
        if ch == "bbo" and d["coin"] == ref:
            b = d.get("bbo") or [None, None]
            if b[0] and b[1]:
                ref_mid, ref_t = (float(b[0]["px"]) + float(b[1]["px"])) / 2, t
            continue
        if ch == "l2Book" and d["coin"] == coin:
            lv = d["levels"]
            bids = [(D(x["px"]), D(x["sz"])) for x in lv[0]]
            asks = [(D(x["px"]), D(x["sz"])) for x in lv[1]]
            if not bids or not asks:
                continue
            for side, o in list(orders.items()):
                if not o:
                    continue
                so = o[0]
                if q.crossed(so, float(bids[0][0]), float(asks[0][0])):
                    fill(side, so.remaining, so.px, t, ses)
                    orders[side] = None
                    continue
                lvl = next((float(s) for p, s in (bids if so.is_buy else asks) if float(p) == so.px), 0.0)
                q.on_level(so, lvl)
            if t - ref_t <= cfg.ref_stale_s:
                fv.update(mid(), ref_mid, t)
        elif ch == "trades" and d and d[0]["coin"] == coin:
            for tr in d:
                for side, o in list(orders.items()):
                    if o:
                        f = q.on_trade(o[0], float(tr["px"]), float(tr["sz"]))
                        if f > 0:
                            fill(side, f, o[0].px, t, ses)
                            if o[0].remaining <= 1e-12:
                                orders[side] = None
        else:
            continue
        if not bids or not asks:
            continue
        mark(t)
        if sessions and ses not in sessions:
            for side in orders:
                orders[side] = None
            continue
        if t - ref_t > cfg.ref_stale_s or not fv.warm(t):
            gap = None
        else:
            f_ = fv.fair(ref_mid)
            gap = (f_ - mid()) / mid() * 1e4 if g is not None else 0.0
        x = QuoteInputs(bids, asks, None, None, pos, ses, gap_bps=gap)
        qt = compute_quote(x, meta, cfg)
        for side, want in (("BUY", qt.bid), ("SELL", qt.ask)):
            o = orders[side]
            if want is None:
                orders[side] = None
                continue
            px = float(want.price)
            if o and o[0].px == px:
                continue
            if o and t - o[1] < min_requote_s and not ((side == "BUY" and o[0].px > px) or (side == "SELL" and o[0].px < px)):
                continue
            lvl = next((float(s) for p, s in (bids if side == "BUY" else asks) if p == want.price), 0.0)
            orders[side] = (q.join(side == "BUY", px, float(want.size), lvl), t)
            st[ses]["actions"] += 1
    return st, fees


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--coin", default="io:SNDK")
    ap.add_argument("--ref", default="")
    ap.add_argument("--g", default="off,1,1.5,2,3")
    ap.add_argument("--session", default="", help="comma list: rth,pre,post,overnight,weekend")
    ap.add_argument("--clip", type=float, default=12.0)
    ap.add_argument("--max-inv", type=float, default=24.0)
    ap.add_argument("--sz-decimals", type=int, default=4)
    ap.add_argument("--maker-bps", type=float, default=0.3)
    ap.add_argument("--min-requote-s", type=float, default=0.75)
    a = ap.parse_args()
    ref = a.ref or "xyz:" + a.coin.split(":")[1]
    rows = load(a.files)
    sessions = set(filter(None, a.session.split(",")))
    print(f"{a.coin} vs {ref}: {len(rows)} messages")
    print(f"{'g':>5} {'session':<10}{'hours':>6}{'fills/h':>8}{'$/h':>8}{'act/h':>7}{'mid30':>7}{'mid120':>7}"
          f"{'fair30':>7}{'fair120':>8}{'n':>5}{'cost_bp':>8}")
    for gs in a.g.split(","):
        g = None if gs == "off" else float(gs)
        st, _ = simulate(rows, a.coin, ref, g, sessions, a.clip, a.max_inv, a.sz_decimals, a.maker_bps,
                         a.min_requote_s)
        for ses, s in sorted(st.items()):
            h = s["secs"] / 3600
            if h <= 0:
                continue
            mo = lambda k: (f"{sum(s['mo'][k]) / len(s['mo'][k]):+.2f}" if s["mo"][k] else "-")  # noqa: E731
            cost = -s["pnl"] / s["vol"] * 1e4 if s["vol"] else float("nan")
            print(f"{gs:>5} {ses:<10}{h:>6.2f}{s['fills'] / h:>8.0f}{s['vol'] / h:>8.0f}{s['actions'] / h:>7.0f}"
                  f"{mo(('mid', 30.0)):>7}{mo(('mid', 120.0)):>7}{mo(('fair', 30.0)):>7}{mo(('fair', 120.0)):>8}"
                  f"{len(s['mo'][('fair', 30.0)]):>5}{cost:>8.2f}")


if __name__ == "__main__":
    main()
