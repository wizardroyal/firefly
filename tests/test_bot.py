"""Bot-level safety rules against a fake exchange (no network)."""
import asyncio
import time
from decimal import Decimal as D

import pytest

from entropymm.bot import Bot, MarketState
from entropymm.config import MarketCfg, Settings
from entropymm.estimators import FairValue, VolGuard
from entropymm.gateway import LiveGateway, LiveOrder
from entropymm.hl import WriteResult
from entropymm.meta import AssetMeta
from entropymm.quoting import Side
from entropymm.risk import Mode

KEY = "0x" + "22" * 32


class FakeRest:
    def __init__(self):
        self.calls = []
        self.dms_ok = True
        self.modify_res = WriteResult(True, oid=None)
        self.modify_raises = False
        self.open = []
        self.state_ = {"marginSummary": {"accountValue": "19", "totalMarginUsed": "4"}, "assetPositions": []}
        self.next_oid = 100

    async def schedule_cancel(self, at):
        self.calls.append(("dms", at))
        return WriteResult(self.dms_ok, error="" if self.dms_ok else "not eligible")

    async def place(self, meta, is_buy, px, sz, ro=False, tif="Alo"):
        self.calls.append(("place", is_buy, px, sz, ro, tif))
        self.next_oid += 1
        return WriteResult(True, oid=self.next_oid)

    async def modify(self, meta, oid, is_buy, px, sz, ro=False, tif="Alo"):
        self.calls.append(("modify", oid, px))
        if self.modify_raises:
            raise asyncio.TimeoutError()
        return self.modify_res

    async def cancel(self, meta, oid):
        self.calls.append(("cancel", oid))
        return WriteResult(False, error="Order was never placed, already canceled, or filled.")

    async def open_orders(self, dex):
        return self.open

    async def state(self, dex):
        return self.state_

    async def positions(self, dex):
        return {}

    async def fills_since(self, ms):
        return []


def make_bot(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTROPY_AGENT_KEY", KEY)
    s = Settings(dry_run=False, account_address="0x" + "ab" * 20, state_dir=str(tmp_path / "state"),
                 log_dir=str(tmp_path / "logs"), blackouts=[])
    cfg = MarketCfg("io:SNDK")
    s.markets = {"io:SNDK": cfg}
    b = Bot(s)
    rest = FakeRest()
    b.rest = rest
    b.gw = LiveGateway(rest)
    meta = AssetMeta("io:SNDK", 200002, 4, 15, growth_mode="enabled", mark=D("828.8"))
    ms = MarketState(cfg, meta, vol=VolGuard(3.0), fair=FairValue(120, 60))
    ms.bids = [(D("828.80"), D("1")), (D("828.75"), D("1"))]
    ms.asks = [(D("828.85"), D("1")), (D("828.90"), D("1"))]
    ms.book_t = time.monotonic()
    b.mkts["io:SNDK"] = ms
    b.ledger.init_market("io:SNDK", D(0), D("828.8"), 0)
    return b, rest, ms


def run(c):
    return asyncio.run(c)


def test_no_order_without_fresh_dead_mans_switch(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    rest.dms_ok = False
    run(b._sync_side(ms, "BUY", Side(D("828.80"), D("0.0145"))))
    assert not [c for c in rest.calls if c[0] == "place"]
    rest.dms_ok = True
    run(b._sync_side(ms, "BUY", Side(D("828.80"), D("0.0145"))))
    assert [c for c in rest.calls if c[0] == "place"] and ms.orders["BUY"] is not None


def test_pending_cleared_when_modify_raises(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    lo = LiveOrder(5, "io:SNDK", True, D("828.70"), D("0.0145"), False, last_change=0)
    ms.orders["BUY"] = lo
    rest.modify_raises = True
    with pytest.raises(asyncio.TimeoutError):
        run(b._sync_side(ms, "BUY", Side(D("828.80"), D("0.0145"))))
    assert lo.pending is False


def test_refused_modify_means_order_gone(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    ms.orders["BUY"] = LiveOrder(5, "io:SNDK", True, D("828.70"), D("0.0145"), False, last_change=0)
    rest.modify_res = WriteResult(False, error="Cannot modify canceled or filled order")
    run(b._sync_side(ms, "BUY", Side(D("828.80"), D("0.0145"))))
    assert ("cancel", 5) in rest.calls and ms.orders["BUY"] is None   # not-found cancel is fine
    run(b._sync_side(ms, "BUY", Side(D("828.80"), D("0.0145"))))
    assert rest.calls[-1][0] == "place"


def test_modify_takes_new_oid(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    lo = LiveOrder(5, "io:SNDK", True, D("828.70"), D("0.0145"), False, last_change=0)
    ms.orders["BUY"] = lo
    rest.modify_res = WriteResult(True, oid=9)
    run(b._sync_side(ms, "BUY", Side(D("828.80"), D("0.0145"))))
    assert lo.oid == 9 and lo.price == D("828.80")


def test_no_reprice_on_every_tick(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    ms.orders["BUY"] = LiveOrder(5, "io:SNDK", True, D("828.80"), D("0.0145"), False)
    run(b._sync_side(ms, "BUY", Side(D("828.80"), D("0.0146"))))     # same price: nothing to do
    assert not [c for c in rest.calls if c[0] in ("modify", "place")]


def test_ghost_orders_dropped(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    ms.orders["SELL"] = LiveOrder(7, "io:SNDK", False, D("828.85"), D("0.0145"), False, last_change=0)
    run(b._reconcile())
    assert ms.orders["SELL"] is None


def test_orphan_swept(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    rest.open = [{"coin": "io:SNDK", "oid": 42, "timestamp": 0}]
    run(b._reconcile())
    assert ("cancel", 42) in rest.calls


def test_xyz_stale_pulls_both_sides(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    ms.orders["BUY"] = LiveOrder(5, "io:SNDK", True, D("828.80"), D("0.0145"), False)
    ms.ref_t = 0.0                                                     # never seen xyz
    run(b._quote_market(ms))
    assert ("cancel", 5) in rest.calls
    assert not [c for c in rest.calls if c[0] == "place"]


def test_quotes_when_fair_value_warm(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    now = time.monotonic()
    for t in range(70):
        ms.fair.update(828.825, 828.0, t=now - 70 + t)
    ms.ref_bid, ms.ref_ask, ms.ref_t = D("827.99"), D("828.01"), now
    run(b._quote_market(ms))
    placed = [c for c in rest.calls if c[0] == "place"]
    assert {c[1] for c in placed} == {True, False} and all(c[5] == "Alo" for c in placed)


def test_fee_kill_halts(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    for i in range(3):   # 3 bp maker fee = growth mode off
        b._on_api_fill({"coin": "io:SNDK", "side": "B", "px": "800", "sz": "0.015", "fee": "0.0036",
                        "tid": i + 1, "oid": 1, "crossed": False, "time": 1})
    assert b.riskview.mode == Mode.HALT and "maker fee" in b.risk.s["halt_reason"]


def test_api_fill_dedupe_and_ledger(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    f = {"coin": "io:SNDK", "side": "B", "px": "800", "sz": "0.015", "fee": "0.00036", "tid": 1, "oid": 1,
         "crossed": False, "time": 5}
    b._on_api_fill(f)
    b._on_api_fill(f)
    assert ms.position == D("0.015") and b.ledger.totals("io:SNDK")["fills"] == 1
    b._on_api_fill({**f, "coin": "xyz:SNDK", "tid": 2})                # not ours
    assert b.ledger.totals("io:SNDK")["fills"] == 1


def test_growth_mode_off_kills(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)

    async def meta_ctxs(dex):
        return ({"universe": [{"name": "io:SNDK", "szDecimals": 4, "maxLeverage": 15}]},
                [{"markPx": "828", "midPx": "828"}])
    rest.meta_ctxs = meta_ctxs
    run(b._refresh_meta())
    assert b.riskview.mode == Mode.HALT
    run(b._quote_market(ms))
    assert ms.last_note == "halted"


def test_flatten_price_bounded_by_mark(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    ms.bids = [(D("700"), D("1"))]                                     # touch collapsed far below mark
    px, ref = b._flatten_price(ms, is_buy=False)
    assert px >= D("828.8") * D("0.99")


def test_emergency_flatten_is_reduce_only_ioc_and_capped(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    ms.position = D("0.03")
    run(b._emergency_flatten(ms, "test"))
    p = [c for c in rest.calls if c[0] == "place"][-1]
    assert p[1] is False and p[4] is True and p[5] == "Ioc"
    ms.position, ms.last_flatten = D("1"), -1e9                       # $828: beyond max_taker_flatten_usd
    n = len(rest.calls)
    run(b._emergency_flatten(ms, "test"))
    assert len(rest.calls) == n


def test_margin_flatten_only(tmp_path, monkeypatch):
    b, rest, ms = make_bot(tmp_path, monkeypatch)
    b._check_margin({"marginSummary": {"accountValue": "10", "totalMarginUsed": "9"}, "assetPositions": []})
    assert b.margin_flatten
