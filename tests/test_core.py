"""Pure pieces: tick math, wire format + signing vs the SDK, sessions, estimators, queue, quoting."""
from datetime import datetime, timezone
from decimal import Decimal as D

import msgpack
import pytest

from entropymm import queue as q
from entropymm.config import MarketCfg
from entropymm.estimators import FairValue, Markouts, VolGuard
from entropymm.hl import Signer, order_wire, parse_status
from entropymm.meta import AssetMeta, asset_id, build_metas, price_decimals, snap_price, tick_for, wire
from entropymm.quoting import QuoteInputs, compute_quote, touch_ex_own
from entropymm.session import Calendar, day_start, session, week_start

KEY = "0x" + "11" * 32


# ---------------------------------------------------------------- meta / ticks
def test_asset_id_matches_sdk_offsets():
    # SDK: builder dexs start at 110000 + i*10000 for perpDexs()[1:] -> io (index 10) = 200000
    assert asset_id(10, 2) == 200002
    assert asset_id(1, 0) == 110000
    assert asset_id(0, 5) == 5


def test_build_metas_uses_dex_position():
    dexs = [None, {"name": "xyz"}, {"name": "io"}]
    u = {"universe": [{"name": "io:A", "szDecimals": 2, "maxLeverage": 5}, {"name": "io:SNDK", "szDecimals": 4,
                                                                          "maxLeverage": 15, "growthMode": "enabled"}]}
    c = [{"markPx": "1"}, {"markPx": "828.2", "funding": "0.00002", "midPx": "828"}]
    m = build_metas(dexs, {"io": (u, c)})
    assert m["io:SNDK"].asset == 100000 + 2 * 10000 + 1
    assert m["io:SNDK"].growth_mode == "enabled" and m["io:A"].growth_mode is None


def test_price_rules_five_sig_figs_and_decimals():
    assert price_decimals(D("828.2"), 4) == 2          # SNDK: 5 sig figs -> 0.01
    assert tick_for(D("129.22"), 2) == D("0.01")       # NBIS
    assert tick_for(D("39.21"), 2) == D("0.001")       # DRAM: 5 sig figs -> 3 decimals
    assert tick_for(D("0.8881"), 1) == D("0.00001")    # max(6 - szDec) decimals
    assert tick_for(D("12345.6"), 0) == D("1")
    assert tick_for(D("123456"), 0) == D("1")          # integer prices always allowed
    assert snap_price(D("828.237"), 4, up=False) == D("828.23")
    assert snap_price(D("828.231"), 4, up=True) == D("828.24")
    assert snap_price(D("999.996"), 4, up=True) == D("1000.0")


def test_size_for_usd_meets_10_dollar_minimum():
    m = AssetMeta("io:SNDK", 200002, 4, 15)
    s = m.size_for_usd(12, D("828.82"))
    assert s == D("0.0145") and s * D("828.82") >= 12
    assert m.size_for_usd(5, D("828.82")) * D("828.82") >= 10


def test_wire_matches_sdk_float_to_wire():
    from hyperliquid.utils.signing import float_to_wire
    for x in ("828.80", "828.8", "0.0145", "1000.0", "39.210", "0.00001", "12"):
        assert wire(D(x)) == float_to_wire(float(x))


def test_order_action_and_signature_identical_to_sdk():
    from eth_account import Account
    from hyperliquid.utils.signing import order_request_to_order_wire, order_wires_to_order_action, sign_l1_action
    sdk = order_wires_to_order_action([order_request_to_order_wire(
        {"coin": "io:SNDK", "is_buy": True, "sz": 0.0145, "limit_px": 828.8, "order_type": {"limit": {"tif": "Alo"}},
         "reduce_only": False}, 200002)])
    ours = {"type": "order", "orders": [order_wire(200002, True, D("828.80"), D("0.0145"), False, "Alo")],
            "grouping": "na"}
    assert msgpack.packb(ours) == msgpack.packb(sdk)
    w = Account.from_key(KEY)
    vault = "0x" + "ab" * 20
    assert sign_l1_action(w, ours, vault, 123, None, True) == sign_l1_action(w, sdk, vault, 123, None, True)


def test_nonces_strictly_increase_within_one_millisecond():
    s = Signer(KEY, None, True)
    ns = [s.next_nonce() for _ in range(1000)]
    assert len(set(ns)) == 1000 and ns == sorted(ns)


def test_parse_status_shapes():
    assert parse_status({"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"resting": {"oid": 77}}]}}}).oid == 77
    r = parse_status({"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"error": "Post only order would have immediately matched"}]}}})
    assert not r.ok and "Post only" in r.error
    r = parse_status({"status": "err", "response": "Too many cumulative requests sent"})
    assert not r.ok and r.rate_limited
    assert parse_status({"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}).ok


# ---------------------------------------------------------------- sessions
def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_sessions_et():
    # 2026-11-06 is a Friday; EST (UTC-5) after Nov 1
    assert session(utc(2026, 11, 6, 15, 0)) == "rth"          # 10:00 ET
    assert session(utc(2026, 11, 6, 10, 0)) == "pre"          # 05:00 ET
    assert session(utc(2026, 11, 6, 22, 0)) == "post"         # 17:00 ET
    assert session(utc(2026, 11, 7, 2, 0)) == "weekend"       # Fri 21:00 ET
    assert session(utc(2026, 11, 8, 12, 0)) == "weekend"      # Sunday
    assert session(utc(2026, 11, 9, 2, 0)) == "overnight"     # Sun 21:00 ET
    assert session(utc(2026, 11, 10, 6, 0)) == "overnight"    # Tue 01:00 ET
    assert session(utc(2026, 11, 26, 16, 0)) == "weekend"     # Thanksgiving
    assert session(utc(2026, 11, 27, 2, 0)) == "overnight"    # Thu 21:00 ET before a trading Friday


def test_blackout_and_flatten_lead():
    cal = Calendar(["09:25-09:40", "sun 19:50-20:10"], flatten_lead_min=10)
    assert cal.regime(utc(2026, 11, 6, 14, 30)) == "blackout"   # 09:30 ET
    assert cal.regime(utc(2026, 11, 6, 14, 20)) == "flatten"    # 09:20 ET, 5 min before
    assert cal.minutes_to_blackout(utc(2026, 11, 6, 14, 20)) == pytest.approx(5)
    assert cal.regime(utc(2026, 11, 9, 0, 55)) == "blackout"    # Sun 19:55 ET
    assert cal.regime(utc(2026, 11, 7, 14, 30)) == "weekend"    # Saturday: weekday windows don't apply


def test_budget_clock_is_utc_monday():
    assert week_start(utc(2026, 11, 6, 15)) == utc(2026, 11, 2)
    assert day_start(utc(2026, 11, 6, 15)) == utc(2026, 11, 6)


# ---------------------------------------------------------------- estimators
def test_markouts_horizons_and_decay():
    seen = []
    m = Markouts(1.0, 3.0, on_done=lambda h, b, *_: seen.append((h, round(b, 6))), halflife_s=100)
    m.on_fill(+1, 100.0, 12, t=0)
    m.on_price(100.01, t=5)
    m.on_price(99.99, t=30)
    m.on_price(100.0, t=120)
    assert seen == [(5.0, 1.0), (30.0, -1.0), (120.0, 0.0)]
    m.mult = 3.0
    m.on_price(100, t=220)          # 100 s later: halfway back to 1
    assert m.mult == pytest.approx(2.0)


def test_markouts_toxicity_rises_on_bad_fills_and_kill_after_window():
    m = Markouts(1.0, 3.0, window=5, kill_window=10)
    for i in range(10):
        m.on_fill(+1, 100.0, 12, t=i)
    m.on_price(99.95, t=200)        # -5 bp each
    assert m.mult > 1.0 and m.kill_signal


def test_fair_value_premium_and_gap():
    fv = FairValue(halflife_s=120, warmup_s=60)
    for t in range(0, 120):
        fv.update(100.05, 100.0, t=t)          # io carries a +5 bp premium
    assert fv.warm(120)
    assert fv.gap_bps(100.05, 100.0) == pytest.approx(0, abs=1e-6)
    # xyz jumps +3 bp, io not yet: fair is above io -> positive gap (asks endangered)
    assert fv.gap_bps(100.05, 100.03) == pytest.approx(3.0, abs=0.05)


def test_vol_guard_trips_on_burst():
    g = VolGuard(3.0, fast_hl=30, slow_hl=3600, warmup=10)
    px = 100.0
    for t in range(0, 600):
        g.update(px * (1 + (0.00001 if t % 2 else -0.00001)), t=t)
    assert not g.tripped
    for t in range(600, 640):
        g.update(px * (1 + (0.002 if t % 2 else -0.002)), t=t)
    assert g.tripped


# ---------------------------------------------------------------- queue model
def test_queue_eats_ahead_then_fills():
    o = q.join(True, 100.0, 1.0, level_size=2.0)
    assert q.on_trade(o, 100.0, 1.5) == 0
    assert q.on_trade(o, 100.0, 1.0) == pytest.approx(0.5)
    assert q.on_trade(o, 99.9, 5.0) == pytest.approx(0.5)     # through: fills the rest
    o2 = q.join(False, 101.0, 1.0, 3.0)
    q.on_level(o2, 0.5)                                      # cancels ahead
    assert q.on_trade(o2, 101.0, 1.0) == pytest.approx(0.5)
    assert q.crossed(q.join(True, 100, 1, 0), best_bid=99, best_ask=100)


# ---------------------------------------------------------------- quoting
META = AssetMeta("io:SNDK", 200002, 4, 15, growth_mode="enabled")


def book(bb="828.80", ba="828.85", sz="1"):
    return [(D(bb), D(sz)), (D(bb) - D("0.05"), D(sz))], [(D(ba), D(sz)), (D(ba) + D("0.05"), D(sz))]


def quote(pos="0", gap=0.0, own_b=None, own_a=None, cfg=None, **kw):
    b, a = kw.pop("levels", book())
    x = QuoteInputs(b, a, own_b, own_a, D(pos), kw.pop("regime", "overnight"), gap_bps=gap, **kw)
    return compute_quote(x, META, cfg or MarketCfg("io:SNDK"))


def test_joins_touch_both_sides_flat():
    qt = quote()
    assert qt.bid.price == D("828.80") and qt.ask.price == D("828.85")
    assert qt.bid.size * qt.bid.price >= 12 and not qt.bid.reduce_only


def test_touch_excludes_our_own_size():
    b, _ = book()
    assert touch_ex_own(b, (D("828.80"), D("1"))) == D("828.75")   # we are alone at the top -> drop to others'
    assert touch_ex_own(b, (D("828.80"), D("0.4"))) == D("828.80")


def test_inventory_skew_backs_off_adding_side_and_cap_pulls_it():
    long_ = quote(pos="0.0145")                      # ~$12 long of $24 cap
    assert long_.ask.price == D("828.85")            # reducing side stays at the touch
    assert long_.bid.price < D("828.80")
    capped = quote(pos="0.029")                      # ~$24 -> at the hard cap
    assert capped.bid is None and capped.ask is not None


def test_guard_backs_off_endangered_side_and_pulls_past_threshold():
    up = quote(gap=+2.0)                             # fair above io: asks get lifted
    assert up.ask.price == D("828.87") and up.bid.price == D("828.80")
    pull = quote(gap=+8.0)
    assert pull.ask is None and pull.bid is not None
    keep = quote(pos="0.0145", gap=+8.0)             # long: the ask REDUCES -> kept (backed off)
    assert keep.ask is not None


def test_no_fair_value_means_no_quotes_but_flatten_still_works():
    assert quote(gap=None).bid is None and quote(gap=None).ask is None
    fl = quote(pos="0.0145", gap=None, flatten_only=True)
    assert fl.bid is None and fl.ask.reduce_only and fl.ask.size == D("0.0145")


def test_toxicity_backoff_exempts_reducing_side():
    qt = quote(pos="-0.0145", toxicity_mult=2.0)
    assert qt.bid.price == D("828.80")               # buying reduces the short: not backed off
    assert qt.ask.price > D("828.85")


def test_vol_guard_only_reducing_side():
    assert quote(vol_tripped=True).bid is None
    qt = quote(pos="0.0145", vol_tripped=True)
    assert qt.bid is None and qt.ask is not None


def test_blackout_off_session_and_dust():
    assert quote(regime="blackout").bid is None
    off = quote(pos="0.0145", session_ok=False)
    assert off.ask.reduce_only and off.bid is None
    dust = quote(pos="0.005", flatten_only=True)     # ~$4: below the $10 minimum
    assert dust.ask is None and "dust" in dust.note


def test_post_only_never_crosses_on_one_tick_book():
    qt = quote(levels=book("828.80", "828.81"))
    assert qt.bid.price < qt.ask.price
    assert qt.bid.price <= D("828.80") and qt.ask.price >= D("828.81")
