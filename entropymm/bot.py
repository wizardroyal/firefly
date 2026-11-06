"""Main market-making loop (port of arcus-mm bot.py onto Hyperliquid)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiohttp

from .alerts import Notifier
from .config import MarketCfg, Settings
from .estimators import FairValue, Markouts, VolGuard
from .gateway import LiveGateway, LiveOrder, PaperGateway
from .hl import HlRest, Signer
from .ledger import Ledger, LedgerError
from .meta import MIN_ORDER_USD, AssetMeta, build_metas
from .metrics import EventLog, FrameDump, format_report, summarize
from .quoting import QuoteInputs, Side, compute_quote
from .ratebudget import RateBudget
from .risk import HALT_FOREVER, MarketView, Mode, RiskManager, RiskView
from .session import WAT, Calendar

log = logging.getLogger("emm.bot")
D = Decimal

DEX = "io"
NETWORK_ERRORS = (aiohttp.ClientError, OSError, asyncio.TimeoutError)
FLATTEN_TAKER_MIN = 2.0      # cross to flat in the last N minutes before a blackout
FLATTEN_COOLDOWN_S = 5.0
FILL_SYNC_OVERLAP_MS = 120_000
FUNDING_OVERLAP_MS = 3 * 3600_000
DMS_LEAD_S = 45              # server cancels everything this long after the last refresh
DMS_FRESH_S = 35.0           # place only if the last refresh is younger than this
GHOST_GRACE_S = 5.0
BOOK_CONFIRM_FRAC = 0.6
WS_DOWN_ALERT_S = 300.0
FEE_CHECK_FILLS = 3          # maker fills before the fee kill rule can fire
MARGIN_REJECT_COOLDOWN_S = 30.0
ALO_REJECT_COOLDOWN_S = 0.5


@dataclass
class MarketState:
    cfg: MarketCfg
    meta: AssetMeta
    ref_meta: AssetMeta | None = None
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    book_t: float = 0.0
    ref_bid: Decimal = D(0)
    ref_ask: Decimal = D(0)
    ref_t: float = 0.0
    position: Decimal = D(0)
    vol: VolGuard | None = None
    fair: FairValue | None = None
    orders: dict = field(default_factory=lambda: {"BUY": None, "SELL": None})
    mo_mid: Markouts | None = None
    mo_fair: Markouts | None = None
    last_note: str = ""
    last_gap: float | None = None
    reject_cooldown_until: float = 0.0
    last_flatten: float = -1e9
    dust_logged: bool = False
    actions: deque = field(default_factory=deque)
    maker_fees: deque = field(default_factory=lambda: deque(maxlen=10))   # (fee, notional)
    mrisk: MarketView | None = None

    @property
    def mid(self) -> Decimal:
        if not self.bids or not self.asks:
            return D(0)
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def ref_mid(self) -> float:
        return float(self.ref_bid + self.ref_ask) / 2 if self.ref_bid > 0 and self.ref_ask > 0 else 0.0


def parse_levels(levels: list) -> list[tuple[Decimal, Decimal]]:
    return [(D(lv["px"]), D(lv["sz"])) for lv in levels]


class Bot:
    def __init__(self, s: Settings):
        self.s = s
        self.tag = f"{s.network}{'-paper' if s.dry_run else ''}"
        self.events = EventLog(s.log_dir, self.tag, on_write=self._on_event)
        self.risk = RiskManager(s.risk, s.state_dir, self.tag)
        self.rate = RateBudget(s.risk.min_rate_surplus)
        self.notify = Notifier(self.tag, s.ipv4_only)
        self.cal = Calendar(s.blackouts, s.flatten_lead_min)
        try:
            self.ledger = Ledger(s.state_dir, self.tag)
        except LedgerError as e:
            raise SystemExit(f"{e}. Not starting: the loss budget depends on it. Restore it, "
                             "or move it aside to restart the ledger from zero.") from None
        self.mkts: dict[str, MarketState] = {}
        self.metas: dict[str, AssetMeta] = {}
        self.seen_trades: set[str] = set()
        self.closed_ids: set[int] = set()
        self.streams = None
        self._fill_cursor_ms = int(time.time() * 1000)
        self._dms_ok_t = -1e9
        self._dms_warn_t = -1e9
        self._ws_down_since: float | None = None
        self._ws_alerted = False
        self._last_summary_day: str | None = None
        self.margin_frac = 0.0
        self.margin_flatten = False
        self.io_account_value = 0.0
        self.riskview = RiskView(Mode.NORMAL, 0, 0)
        self.pnl = 0.0
        self.pnl0: float | None = None
        self.market_pnl: dict[str, float] = {}
        self.running = True
        self.dump = FrameDump(s.log_dir) if s.dump_frames else None
        signer = None
        if not s.dry_run:
            signer = Signer(s.agent_key, s.account_address if s.is_subaccount else None, s.is_mainnet)
        self.rest = HlRest(s.rest_url, signer, s.account_address, self.dump, s.ipv4_only, on_write=self._on_write)
        self.gw = PaperGateway(self._on_gw_fill) if s.dry_run else LiveGateway(self.rest, DEX)

    # ------------------------------------------------------------------ helpers
    @property
    def paper(self) -> bool:
        return isinstance(self.gw, PaperGateway)

    def _regime(self) -> str:
        return self.cal.regime()

    def _market_quotes(self, ms: MarketState, reg: str) -> bool:
        """Two-sided quoting is this market's job in this session (quote_regimes)."""
        if reg in ("blackout", "flatten"):
            return False
        return not ms.cfg.quote_regimes or reg in ms.cfg.quote_regimes

    def _on_write(self, res) -> None:
        if res.rate_limited:
            self.rate.exhausted()
            self.events.write("alert", what="rate_limited", error=res.error[:200])

    def _mo(self, ms: MarketState) -> tuple[Markouts, Markouts]:
        if ms.mo_mid is None:
            r, sym = self.s.risk, ms.meta.coin
            ms.mo_fair = Markouts(r.toxic_markout_bps, r.kill_markout_bps, halflife_s=r.toxic_halflife_s,
                                  on_done=lambda h, b, tag, n, ref: self._on_markout(sym, "fair", h, b, tag, n, ref))
            ms.mo_mid = Markouts(1e9, 1e9, adaptive=False,
                                 on_done=lambda h, b, tag, n, ref: self._on_markout(sym, "mid", h, b, tag, n, ref))
        return ms.mo_mid, ms.mo_fair

    def _on_markout(self, sym, ref, h, bps, tag, notional, trade_id) -> None:
        self.events.write("markout", symbol=sym, ref=ref, h=int(h), bps=round(bps, 3), session=tag,
                          notional=round(notional, 4), trade_id=trade_id)

    def _fair(self, ms: MarketState, now: float | None = None) -> float | None:
        now = now if now is not None else time.monotonic()
        if now - ms.ref_t > ms.cfg.ref_stale_s or not ms.fair or not ms.fair.warm(now):
            return None
        return ms.fair.fair(ms.ref_mid)

    def _find(self, oid: int) -> tuple[MarketState, str, LiveOrder] | None:
        for ms in self.mkts.values():
            for side, o in ms.orders.items():
                if o and o.oid == oid:
                    return ms, side, o
        return None

    def _tracked_ids(self) -> set[int]:
        return {o.oid for ms in self.mkts.values() for o in ms.orders.values() if o}

    # ------------------------------------------------------------------ streams
    def on_message(self, msg: dict) -> None:
        ch, d = msg.get("channel"), msg.get("data")
        if d is None:
            return
        if ch == "l2Book":
            ms = self.mkts.get(d.get("coin"))
            if ms:
                self._apply_book(ms, d)
        elif ch == "bbo":
            bb = d.get("bbo") or [None, None]
            for ms in self.mkts.values():
                if ms.cfg.ref == d.get("coin"):
                    if bb[0] and bb[1]:
                        ms.ref_bid, ms.ref_ask, ms.ref_t = D(bb[0]["px"]), D(bb[1]["px"]), time.monotonic()
        elif ch == "trades":
            if self.paper:
                for t in d:
                    if t.get("coin") in self.mkts:
                        self.gw.on_trade(t["coin"], D(t["px"]), D(t["sz"]))
        elif ch == "orderUpdates":
            for u in d:
                self._on_order_update(u)
        elif ch == "userFills":
            for f in d.get("fills", []):
                self._on_api_fill(f)

    def _apply_book(self, ms: MarketState, d: dict) -> None:
        lv = d.get("levels") or [[], []]
        ms.bids, ms.asks = parse_levels(lv[0]), parse_levels(lv[1])
        ms.book_t = time.monotonic() if ms.bids and ms.asks else 0.0
        if self.paper:
            self.gw.on_book(ms.meta.coin, ms.bids, ms.asks)

    def _on_order_update(self, u: dict) -> None:
        o = u.get("order") or {}
        try:
            oid = int(o.get("oid"))
        except (TypeError, ValueError):
            return
        hit = self._find(oid)
        if not hit:
            return
        ms, side, lo = hit
        status = str(u.get("status", ""))
        if status in ("open", "triggered"):
            return
        if lo.pending and status == "canceled":
            return            # leg of a modify in flight; the ghost sweep catches a real cancel
        ms.orders[side] = None
        self.closed_ids.add(oid)
        if len(self.closed_ids) > 2000:
            self.closed_ids = set(list(self.closed_ids)[-1000:])
        if status not in ("filled", "canceled"):
            self.events.write("reject", symbol=ms.meta.coin, side=side, reason=status)
            if "margin" in status.lower() or "interest" in status.lower():
                ms.reject_cooldown_until = time.monotonic() + MARGIN_REJECT_COOLDOWN_S

    def _on_api_fill(self, f: dict) -> None:
        coin = f.get("coin")
        if coin not in self.mkts:
            return
        tid = str(f.get("tid", ""))
        if not tid or tid in self.seen_trades:
            return
        self.seen_trades.add(tid)
        is_buy = f.get("side") == "B"
        px, sz, fee = D(str(f["px"])), D(str(f["sz"])), D(str(f.get("fee") or 0))
        crossed, ts = bool(f.get("crossed")), int(f.get("time") or 0)
        self.ledger.apply_fill(coin, is_buy, px, sz, fee, tid, ts, crossed)
        self._fill_cursor_ms = max(self._fill_cursor_ms, ts)
        self._on_fill(coin, is_buy, px, sz, fee, int(f.get("oid") or 0), tid, crossed, ts)

    def _on_gw_fill(self, coin, is_buy, px, sz, fee, oid, tid, crossed, ts) -> None:
        """Paper gateway fills: same path as exchange fills."""
        self._on_api_fill({"coin": coin, "side": "B" if is_buy else "A", "px": str(px), "sz": str(sz),
                           "fee": str(fee), "oid": oid, "tid": tid, "crossed": crossed, "time": ts})

    def _on_fill(self, coin, is_buy, px, sz, fee, oid, tid, crossed, ts) -> None:
        ms = self.mkts[coin]
        sgn = 1 if is_buy else -1
        ms.position += sgn * sz
        hit = self._find(oid)
        if hit and not self.paper:
            _, s_, lo = hit
            lo.filled += sz
            if lo.remaining <= 0:
                ms.orders[s_] = None
        notional = float(px * sz)
        ses = self._regime()
        mo_mid, mo_fair = self._mo(ms)
        mo_mid.on_fill(sgn, float(px), notional, tag=ses, ref=tid)
        mo_fair.on_fill(sgn, float(px), notional, tag=ses, ref=tid)
        fair = self._fair(ms)
        self.events.write("fill", symbol=coin, side="BUY" if is_buy else "SELL", price=str(px), size=str(sz),
                          notional=round(notional, 4), fee=str(fee), crossed=crossed, session=ses,
                          position=str(ms.position), mid=str(ms.mid), fair=round(fair, 4) if fair else None,
                          gap_bps=round(ms.last_gap, 3) if ms.last_gap is not None else None, trade_id=tid)
        log.info("FILL %s %s %s @ %s fee=%s pos=%s%s", coin, "BUY" if is_buy else "SELL", sz, px, fee,
                 ms.position, " TAKER" if crossed else "")
        if not crossed:
            self._check_maker_fee(ms, float(fee), notional)

    def _check_maker_fee(self, ms: MarketState, fee: float, notional: float) -> None:
        """KILL RULE: growth mode off or maker rate above max_maker_fee_bps. Fees are
        deterministic, so a few fills are enough evidence."""
        ms.maker_fees.append((fee, notional))
        if len(ms.maker_fees) < FEE_CHECK_FILLS:
            return
        f = sum(x for x, _ in ms.maker_fees)
        n = sum(y for _, y in ms.maker_fees)
        bps = f / n * 1e4 if n else 0.0
        if bps > self.s.risk.max_maker_fee_bps and self.riskview.mode != Mode.HALT:
            why = f"{ms.meta.coin} maker fee {bps:.2f} bp > {self.s.risk.max_maker_fee_bps} bp (growth mode off?)"
            self.risk.halt(why, HALT_FOREVER)
            self.riskview = RiskView(Mode.HALT, self.riskview.week_loss, self.riskview.day_loss, why)
            self.events.write("alert", what="fee_kill", symbol=ms.meta.coin, maker_fee_bps=round(bps, 3))
            self.events.write("mode", mode="halt", was="normal", reason=why, week_loss=round(self.riskview.week_loss, 4))

    # ------------------------------------------------------------------ quoting
    def _spend(self, ms: MarketState) -> None:
        self.rate.spend()
        if ms.cfg.max_actions_per_min > 0:
            ms.actions.append(time.monotonic())

    def _throttled(self, ms: MarketState) -> bool:
        cap = ms.cfg.max_actions_per_min
        if cap <= 0:
            return False
        now = time.monotonic()
        while ms.actions and now - ms.actions[0] > 60:
            ms.actions.popleft()
        return len(ms.actions) >= cap

    async def _cancel_side(self, ms: MarketState, side: str) -> None:
        lo = ms.orders[side]
        if lo is None or lo.pending:
            return
        lo.pending = True
        try:
            ok = await self.gw.cancel(ms.meta, lo)
        finally:
            lo.pending = False
            self._spend(ms)
        if ok and ms.orders[side] is lo:
            ms.orders[side] = None

    async def _sync_side(self, ms: MarketState, side: str, want: Side | None) -> None:
        lo: LiveOrder | None = ms.orders[side]
        meta, cfg = ms.meta, ms.cfg
        if lo and lo.pending:
            return
        if want is None:
            if lo:
                await self._cancel_side(ms, side)
            return
        if lo is None:
            if (self.rate.critical_only or self._throttled(ms)) and not want.reduce_only:
                return
            if time.monotonic() < ms.reject_cooldown_until:
                return
            if not await self._can_place():       # never rest an order without a fresh dead man's switch
                return
            new = await self.gw.place(meta, side == "BUY", want.price, want.size, want.reduce_only, "Alo")
            self._spend(ms)
            if new:
                ms.orders[side] = new
            else:
                err = (self.gw.last_error or "").lower()
                if "margin" in err or "open interest" in err:
                    ms.reject_cooldown_until = time.monotonic() + MARGIN_REJECT_COOLDOWN_S
                    self.events.write("reject", symbol=meta.coin, side=side, reason=self.gw.last_error[:120])
                else:
                    ms.reject_cooldown_until = time.monotonic() + ALO_REJECT_COOLDOWN_S
            return
        # a live order exists
        if lo.filled > 0 or lo.reduce_only != want.reduce_only or (want.reduce_only and want.size != lo.size):
            await self._cancel_side(ms, side)     # partial fill / role change: replace with a fresh order
            return
        tick = meta.tick_for(want.price)
        scale = self.rate.scale
        too_aggressive = (lo.price > want.price) if side == "BUY" else (lo.price < want.price)
        diff_ticks = abs(lo.price - want.price) / tick
        since_ms = (time.monotonic() - lo.last_change) * 1000
        due = diff_ticks >= cfg.requote_ticks * scale and since_ms >= cfg.min_requote_ms * scale
        if not (too_aggressive or (due and not self.rate.critical_only and not self._throttled(ms))):
            return
        if not await self._can_place():
            return
        lo.pending = True
        try:
            res = await self.gw.modify(meta, lo, want.price, want.size)
            if res is None:                       # refused: the order is gone -> cancel, drop, re-place
                await self.gw.cancel(meta, lo)
                if ms.orders[side] is lo:
                    ms.orders[side] = None
        finally:
            lo.pending = False                    # never leave pending set after a raise (Arcus mainnet)
            self._spend(ms)

    def _halted(self, ms: MarketState) -> str | None:
        if self.riskview.mode == Mode.HALT:
            return "halted"
        if ms.mrisk is not None and ms.mrisk.mode == Mode.HALT:
            return "market limit"
        if ms.meta.delisted:
            return "delisted"
        if ms.meta.growth_mode != "enabled" and not self.paper:
            return "growth mode off"
        return None

    async def _quote_market(self, ms: MarketState) -> None:
        now = time.monotonic()
        reg = self._regime()
        stale = now - ms.book_t > self.s.risk.stale_book_s
        halted = self._halted(ms)
        if stale or halted or not ms.meta.mid_present:
            for side in ("BUY", "SELL"):
                await self._sync_side(ms, side, None)
            if halted and ms.position != 0 and not stale:
                await self._emergency_flatten(ms, halted)
            ms.last_note = halted or ("stale book" if stale else "no mid (halted?)")
            return
        fair = self._fair(ms, now)
        io_mid = float(ms.mid)
        ms.last_gap = (fair - io_mid) / io_mid * 1e4 if fair and io_mid > 0 else None
        mrv = ms.mrisk
        own_b = (ms.orders["BUY"].price, ms.orders["BUY"].remaining) if ms.orders["BUY"] else None
        own_a = (ms.orders["SELL"].price, ms.orders["SELL"].remaining) if ms.orders["SELL"] else None
        if self.paper:
            own_b = own_a = None                  # paper orders are not in the real book
        flatten_only = (self.riskview.mode == Mode.FLATTEN_ONLY or (mrv is not None and mrv.mode == Mode.FLATTEN_ONLY)
                        or self.margin_flatten or self.rate.critical_only)
        x = QuoteInputs(ms.bids, ms.asks, own_b, own_a, ms.position, reg,
                        session_ok=self._market_quotes(ms, reg) or reg == "flatten",
                        flatten_only=flatten_only, toxicity_mult=self._mo(ms)[1].mult,
                        gap_bps=ms.last_gap, vol_tripped=bool(ms.vol and ms.vol.tripped),
                        funding_apr=ms.meta.funding_apr)
        q = compute_quote(x, ms.meta, ms.cfg)
        ms.last_note = q.note
        why = self._flatten_reason(ms, reg)
        if why:
            await self._emergency_flatten(ms, why)
            return
        await asyncio.gather(self._sync_side(ms, "BUY", q.bid), self._sync_side(ms, "SELL", q.ask))

    def _flatten_reason(self, ms: MarketState, reg: str) -> str | None:
        """A position must never be carried into a blackout (io can gap there);
        the maker flatten quote may not fill, so cross in the last minutes."""
        if ms.position == 0:
            return None
        if reg == "blackout":
            return "blackout"
        left = self.cal.minutes_to_blackout()
        if reg == "flatten" and left is not None and left < FLATTEN_TAKER_MIN:
            return "pre-blackout"
        return None

    def _flatten_price(self, ms: MarketState, is_buy: bool) -> tuple[Decimal | None, Decimal]:
        """IOC limit: 0.1% through the touch, never further than max_flatten_slippage_bps
        from the mark (Arcus filled 3.4% off fair once without this bound)."""
        ref = ms.meta.mark if ms.meta.mark > 0 else ms.mid
        if ref <= 0 or not ms.bids or not ms.asks:
            return None, ref
        slip = D(str(self.s.risk.max_flatten_slippage_bps)) / D(10_000)
        if not is_buy:
            px = ms.meta.snap(ms.bids[0][0] * D("0.999"), up=False)
            return max(px, ms.meta.snap(ref * (1 - slip), up=True)), ref
        px = ms.meta.snap(ms.asks[0][0] * D("1.001"), up=True)
        return min(px, ms.meta.snap(ref * (1 + slip), up=False)), ref

    async def _emergency_flatten(self, ms: MarketState, why: str) -> None:
        """Taker IOC, reduce-only, capped notional and price. The only taker path."""
        if time.monotonic() - ms.last_flatten < FLATTEN_COOLDOWN_S:
            return
        ms.last_flatten = time.monotonic()
        is_buy = ms.position < 0
        px, ref = self._flatten_price(ms, is_buy)
        if px is None:
            log.error("no mark or book for %s; refusing to flatten blind", ms.meta.coin)
            return
        notional = float(abs(ms.position) * ref)
        if notional > self.s.risk.max_taker_flatten_usd:
            log.error("position $%.2f too large for auto-flatten; manual action needed", notional)
            self.events.write("alert", what="flatten_too_large", symbol=ms.meta.coin, notional=round(notional, 2))
            return
        if notional < float(MIN_ORDER_USD):
            if not ms.dust_logged:
                ms.dust_logged = True
                self.events.write("alert", what="dust_position", symbol=ms.meta.coin, position=str(ms.position),
                                  notional=round(notional, 4), why=why)
            # VERIFY: is a reduce-only IOC below $10 accepted? Try; a refusal is harmless.
        for s_ in ("BUY", "SELL"):
            await self._cancel_side(ms, s_)
        self.events.write("flatten", symbol=ms.meta.coin, why=why, side="BUY" if is_buy else "SELL",
                          size=str(abs(ms.position)), px=str(px), ref=str(ref))
        await self.gw.place(ms.meta, is_buy, px, abs(ms.position), True, "Ioc")
        self._spend(ms)

    # ------------------------------------------------------------------ periodic
    async def _second(self) -> None:
        now = time.monotonic()
        for ms in self.mkts.values():
            if not ms.book_t:
                continue
            mid = float(ms.mid)
            ms.vol.update(mid, now)
            if now - ms.ref_t <= ms.cfg.ref_stale_s and ms.ref_mid > 0:
                ms.fair.update(mid, ms.ref_mid, now)
            mo_mid, mo_fair = self._mo(ms)
            mo_mid.on_price(mid, now)
            fair = self._fair(ms, now)
            mo_fair.on_price(fair if fair else 0.0, now)

    async def _confirm_quiet_books(self) -> None:
        """l2Book frames come on change; a quiet io book must not look stale and
        pull our quotes. Confirm over REST before the stale threshold, only while
        the WS is up (fills and order updates come from it)."""
        if self.streams is None or not self.streams.connected.is_set():
            return
        for coin, ms in self.mkts.items():
            if ms.book_t and time.monotonic() - ms.book_t >= BOOK_CONFIRM_FRAC * self.s.risk.stale_book_s:
                sent = time.monotonic()
                d = await self.rest.l2(coin)
                if ms.book_t < sent:
                    self._apply_book(ms, d)
            if ms.ref_t and time.monotonic() - ms.ref_t >= BOOK_CONFIRM_FRAC * ms.cfg.ref_stale_s:
                sent = time.monotonic()
                d = await self.rest.l2(ms.cfg.ref)
                lv = d.get("levels") or [[], []]
                if ms.ref_t < sent and lv[0] and lv[1]:
                    ms.ref_bid, ms.ref_ask, ms.ref_t = D(lv[0][0]["px"]), D(lv[1][0]["px"]), time.monotonic()

    async def _refresh_meta(self) -> None:
        m, ctxs = await self.rest.meta_ctxs(DEX)
        for u, c in zip(m["universe"], ctxs):
            ms = self.mkts.get(u["name"])
            if not ms:
                continue
            was_growth, was_delisted = ms.meta.growth_mode, ms.meta.delisted
            ms.meta.update_ctx(u, c)
            if ms.meta.delisted and not was_delisted:
                self._kill(f"{u['name']} is DELISTED (settles at Entropy's price)", "delisted", u["name"])
            if ms.meta.growth_mode != "enabled" and was_growth == "enabled" and not self.paper:
                self._kill(f"{u['name']} growth mode is now {ms.meta.growth_mode!r}: fees 10x", "growth_mode_off",
                           u["name"])

    def _kill(self, why: str, what: str, sym: str = "") -> None:
        self.events.write("alert", what=what, symbol=sym, reason=why)
        if self.riskview.mode != Mode.HALT:
            self.risk.halt(why, HALT_FOREVER)
            old = self.riskview.mode
            self.riskview = RiskView(Mode.HALT, self.riskview.week_loss, self.riskview.day_loss, why)
            self.events.write("mode", mode="halt", was=old.value, reason=why, week_loss=round(self.riskview.week_loss, 4))

    async def _marks_and_positions(self) -> dict[str, Decimal] | None:
        if self.paper:
            return await self.gw.positions()
        for _ in range(3):
            before = await self.gw.positions()
            await self._sync_fills()
            after = await self.gw.positions()
            if all(before.get(c, D(0)) == after.get(c, D(0)) for c in self.mkts):
                return after
        log.warning("positions kept moving during the pnl snapshot; skipping this reading")
        return None

    def _mark(self, ms: MarketState) -> Decimal:
        return ms.meta.mark if ms.meta.mark > 0 else ms.mid

    async def _measure_pnl(self) -> float | None:
        """Consistent snapshot: positions, REST fill sync, positions again (skip if they
        moved); value the EXCHANGE position, not our running estimate."""
        pos = await self._marks_and_positions()
        if pos is None:
            return None
        self.ledger.save()
        by = self.ledger.pnl_by_market({c: (pos.get(c, D(0)), self._mark(ms)) for c, ms in self.mkts.items()})
        if by is None:
            return None
        self.market_pnl = {c: by[c] for c in self.mkts if c in by}
        return sum(self.market_pnl.values())

    async def _refresh_risk(self) -> None:
        p = await self._measure_pnl()
        if p is None:
            log.warning("bot pnl not measurable right now; keeping last risk view")
            return
        self.pnl = p
        old = self.riskview.mode
        self.riskview = self.risk.update(self.pnl)
        for c, ms in self.mkts.items():
            lim = ms.cfg.weekly_loss_limit_usd
            if lim > 0 and c in self.market_pnl:
                was = ms.mrisk.mode if ms.mrisk else Mode.NORMAL
                ms.mrisk = self.risk.update_market(c, self.market_pnl[c], lim)
                if ms.mrisk.mode != was:
                    self.events.write("mode", symbol=c, mode=ms.mrisk.mode.value, was=was.value,
                                      reason=ms.mrisk.reason, week_loss=round(ms.mrisk.week_loss, 4))
        killed = [c for c, ms in self.mkts.items() if self._mo(ms)[1].kill_signal]
        if killed and self.riskview.mode != Mode.HALT:
            self.risk.halt(f"30s markout vs fair on {', '.join(killed)} persistently below -kill (picked off)",
                           datetime.now(timezone.utc) + timedelta(hours=2))
            self.riskview = self.risk.update(self.pnl)
        if self.riskview.mode != old:
            self.events.write("mode", mode=self.riskview.mode.value, was=old.value, reason=self.riskview.reason,
                              week_loss=round(self.riskview.week_loss, 4), day_loss=round(self.riskview.day_loss, 4))

    async def _sync_fills(self) -> None:
        """REST is the record: WS fills can be lost without a visible disconnect."""
        if self.paper:
            return
        rows = await self.rest.fills_since(self._fill_cursor_ms - FILL_SYNC_OVERLAP_MS)
        for f in sorted(rows, key=lambda r: r.get("time", 0)):
            if f.get("coin") in self.mkts and str(f.get("tid")) not in self.seen_trades:
                log.warning("booking %s fill %s from REST (missed on the WS)", f.get("coin"), f.get("tid"))
            self._on_api_fill(f)

    async def _sync_funding(self) -> None:
        if self.paper:
            return
        start = max(self.ledger.s.get("last_funding_ms", 0) - FUNDING_OVERLAP_MS,
                    int(time.time() * 1000) - 7 * 86400_000)
        for r in await self.rest.funding_since(start):
            d = r.get("delta") or {}
            if d.get("type") != "funding" or d.get("coin") not in self.mkts:
                continue
            if self.ledger.apply_funding(d["coin"], float(d["usdc"]), int(r["time"])):
                self.events.write("funding", symbol=d["coin"], usdc=d["usdc"], rate=d.get("fundingRate"),
                                  szi=d.get("szi"))
        self.ledger.save()

    async def _reconcile(self) -> None:
        if self.paper:
            return
        await self._sync_fills()
        st = await self.rest.state(DEX)
        pos = {p["position"]["coin"]: D(p["position"]["szi"]) for p in st.get("assetPositions", [])}
        for c, ms in self.mkts.items():
            api = pos.get(c, D(0))
            if api != ms.position:
                log.warning("position drift %s: local %s api %s -> using api", c, ms.position, api)
                ms.position = api
        self._check_margin(st)
        tracked = self._tracked_ids()
        fetch_t = time.monotonic()
        rows = await self.rest.open_orders(DEX)
        self._drop_ghosts({int(r["oid"]) for r in rows}, fetch_t)
        tracked |= self._tracked_ids() | self.closed_ids
        now_ms = time.time() * 1000
        for r in rows:
            ms = self.mkts.get(r.get("coin"))
            oid = int(r["oid"])
            if ms and oid not in tracked and now_ms - float(r.get("timestamp", 0)) > 5000:
                log.warning("cancelling orphan order %s on %s", oid, ms.meta.coin)
                await self.rest.cancel(ms.meta, oid)
                self.rate.spend()

    def _drop_ghosts(self, open_ids: set, fetch_t: float) -> None:
        """Forget tracked orders the exchange no longer lists (Arcus 2026-10-17: the
        dead man's switch cancelled everything while the WS was down and the bot kept
        'quoting' ghosts). Only orders untouched since well before the fetch."""
        for ms in self.mkts.values():
            for side, lo in ms.orders.items():
                if lo and not lo.pending and lo.oid not in open_ids and lo.last_change < fetch_t - GHOST_GRACE_S:
                    log.warning("order %s %s %s@%s is no longer open; dropping it", ms.meta.coin, side, lo.size, lo.price)
                    self.events.write("alert", what="ghost_order", symbol=ms.meta.coin, side=side, oid=lo.oid)
                    ms.orders[side] = None

    def _check_margin(self, st: dict) -> None:
        ms_ = st.get("marginSummary") or {}
        av, used = float(ms_.get("accountValue") or 0), float(ms_.get("totalMarginUsed") or 0)
        self.io_account_value = av
        self.margin_frac = used / av if av > 0 else 0.0
        r = self.s.risk
        if self.margin_frac >= r.margin_alert_frac:
            self.events.write("alert", what="margin", used=round(used, 4), account_value=round(av, 4),
                              frac=round(self.margin_frac, 3))
        flat = self.margin_frac >= r.margin_flatten_frac
        if flat != self.margin_flatten:
            self.margin_flatten = flat
            log.warning("margin %.0f%% used: flatten-only %s", 100 * self.margin_frac, "ON" if flat else "off")
        for p in st.get("assetPositions", []):
            pp = p["position"]
            ms = self.mkts.get(pp["coin"])
            liq = pp.get("liquidationPx")
            if ms and liq and self._mark(ms) > 0:
                dist = abs(float(liq) / float(self._mark(ms)) - 1) * 100
                if dist < r.liq_alert_pct:
                    self.events.write("alert", what="liquidation_near", symbol=pp["coin"], liq=liq,
                                      mark=str(self._mark(ms)), dist_pct=round(dist, 2))

    async def _rate_tick(self) -> None:
        if self.paper:
            return
        self.rate.on_rest(await self.rest.rate_limit())
        if self.rate.critical_only:
            self.events.write("alert", what="rate_allowance_low", surplus=self.rate.surplus)

    def _status(self) -> dict:
        vol = sum(self.ledger.totals(c)["volume"] for c in self.mkts)
        session_pnl = self.pnl - (self.pnl0 if self.pnl0 is not None else self.pnl)
        out = {"bot_pnl": round(self.pnl, 4), "session_pnl": round(session_pnl, 4),
               "mode": self.riskview.mode.value, "reason": self.riskview.reason,
               "week_loss": round(self.riskview.week_loss, 4), "day_loss": round(self.riskview.day_loss, 4),
               "headroom": round(self.risk.headroom(self.pnl), 4), "regime": self._regime(),
               "rate_surplus": self.rate.surplus, "actions_per_h": round(self.rate.per_hour(), 1),
               "margin_frac": round(self.margin_frac, 3), "io_account_value": round(self.io_account_value, 4),
               "markets": {}}
        for c, ms in self.mkts.items():
            t = self.ledger.totals(c)
            b, a = ms.orders["BUY"], ms.orders["SELL"]
            mo_mid, mo_fair = self._mo(ms)
            out["markets"][c] = {"pos": str(ms.position), "mid": str(ms.mid), "bid": str(b.price) if b else None,
                                 "ask": str(a.price) if a else None, "gap_bps": round(ms.last_gap, 2)
                                 if ms.last_gap is not None else None, "note": ms.last_note,
                                 "volume": round(t["volume"], 2), "fees": round(t["fees"], 5),
                                 "funding": round(t["funding"], 5), "pnl": round(self.market_pnl.get(c, 0.0), 4),
                                 "funding_apr": round(ms.meta.funding_apr, 3),
                                 "sigma_fast": round(ms.vol.fast.sigma_bps, 3), "sigma_slow": round(ms.vol.slow.sigma_bps, 3),
                                 **mo_mid.summary("mid"), **mo_fair.summary("fair")}
            if ms.mrisk is not None:
                out["markets"][c].update(week_loss=round(ms.mrisk.week_loss, 4), mkt_mode=ms.mrisk.mode.value)
        out["volume_total"] = round(vol, 2)
        return out

    async def _status_tick(self) -> None:
        st = self._status()
        self.events.write("status", **st)
        log.info("STATUS %s", st)

    async def _every(self, period: float, fn) -> None:
        while self.running:
            try:
                await fn()
            except NETWORK_ERRORS as e:
                log.warning("periodic task %s: network error (%s: %s); retrying in %ss",
                            getattr(fn, "__name__", fn), type(e).__name__, e, period)
            except Exception:
                log.exception("periodic task %s failed", getattr(fn, "__name__", fn))
            await asyncio.sleep(period)

    # ---- alerts ------------------------------------------------------------
    def _on_event(self, kind: str, data: dict) -> None:
        n = getattr(self, "notify", None)
        if n is None or not n.enabled:
            return
        if kind == "alert":
            what = data.get("what", "")
            rest = " ".join(f"{k}={v}" for k, v in data.items() if k != "what")
            n.send(f"alert:{what}:{data.get('symbol', '')}", f"ALERT {what} {rest}".strip(),
                   force=what in ("fee_kill", "delisted", "growth_mode_off"))
        elif kind == "mode":
            who = data.get("symbol") or "bot"
            n.send(f"mode:{who}:{data['mode']}", f"{who} mode {data['was']} -> {data['mode']}"
                   f"{' (' + data['reason'] + ')' if data.get('reason') else ''}, week loss ${data.get('week_loss')}",
                   force=data["mode"] in ("halt", "normal"))
        elif kind == "flatten":
            n.send(f"flatten:{data.get('symbol')}", f"taker flatten {data.get('symbol')} {data.get('side')} "
                   f"{data.get('size')} @ {data.get('px')} ({data.get('why')})")
        elif kind == "start":
            n.send("start", f"bot started ({'paper' if self.paper else 'LIVE'}) markets={list(self.mkts)}", force=True)
        elif kind == "stop":
            n.send("stop", f"bot STOPPED. mode={data.get('mode')} week_loss=${data.get('week_loss')} "
                   f"session_pnl=${data.get('session_pnl')}", force=True)

    async def _watch_ws(self) -> None:
        if self.streams is None:
            return
        now = time.monotonic()
        if self.streams.connected.is_set():
            if self._ws_alerted:
                self.notify.send("ws", f"websocket back after {(now - self._ws_down_since) / 60:.0f} min", force=True)
            self._ws_down_since, self._ws_alerted = None, False
            return
        if self._ws_down_since is None:
            self._ws_down_since = now
        elif not self._ws_alerted and now - self._ws_down_since >= WS_DOWN_ALERT_S:
            self._ws_alerted = True
            self.events.write("alert", what="feed_down", minutes=round((now - self._ws_down_since) / 60, 1))
        for c, ms in self.mkts.items():                # reference feed lost while our own is fine
            if ms.ref_t and now - ms.ref_t > WS_DOWN_ALERT_S:
                self.events.write("alert", what="ref_feed_stale", symbol=c, ref=ms.cfg.ref,
                                  minutes=round((now - ms.ref_t) / 60, 1))

    async def _daily_summary(self) -> None:
        now = datetime.now(WAT)
        day = now.strftime("%Y-%m-%d")
        if now.hour != self.s.summary_hour_wat or self._last_summary_day == day:
            return
        self._last_summary_day = day
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        s = summarize(self.s.log_dir, self.tag, since)
        st = self._status()
        pos = ", ".join(f"{c} {m['pos']}" for c, m in st["markets"].items())
        txt = (f"daily summary {now:%a %d %b %H:%M} WAT (last 24h)\n{format_report(s)}\n"
               f"pnl vs budget: week loss ${st['week_loss']:.3f} of ${self.s.risk.weekly_loss_limit_usd}, "
               f"day loss ${st['day_loss']:.3f}; mode {st['mode']}\npositions: {pos}; margin used "
               f"{100 * st['margin_frac']:.0f}% of ${st['io_account_value']:.2f}; request allowance {st['rate_surplus']}")
        self.events.write("summary", text=txt)
        self.notify.send("summary", txt, force=True)

    # ---- dead man's switch ------------------------------------------------
    # Armed only while there is something to protect: the daily trigger cap (10,
    # per the SDK docs; Arcus burned all 10 during idle hours) must last for the
    # quoting windows. No order is ever placed without a refresh < DMS_FRESH_S old.
    def _dms_live(self) -> bool:
        return not self.paper and self.s.dead_mans_switch

    def _dms_armed(self) -> bool:
        return time.monotonic() - self._dms_ok_t < DMS_FRESH_S

    def _dms_needed(self) -> bool:
        if self._tracked_ids():
            return True
        reg = self._regime()
        return any(self._market_quotes(ms, reg) or ms.position != 0 for ms in self.mkts.values())

    async def _arm_dms(self) -> bool:
        r = await self.rest.schedule_cancel(int(time.time() * 1000) + DMS_LEAD_S * 1000)
        self.rate.spend()
        if r.ok:
            if not self._dms_armed():
                log.info("dead man's switch armed (%ss)", DMS_LEAD_S)
            self._dms_ok_t = time.monotonic()
            return True
        self._dms_ok_t = -1e9
        if time.monotonic() - self._dms_warn_t > 60:
            self._dms_warn_t = time.monotonic()
            log.warning("dead man's switch arm failed (%s); placing no orders until it arms", r.error)
            self.events.write("alert", what="dead_man_arm_failed", error=r.error[:200])
        return False

    async def _can_place(self) -> bool:
        if not self._dms_live() or self._dms_armed():
            return True
        return await self._arm_dms()

    async def _dead_man(self) -> None:
        if not self._dms_live():
            return
        if self._dms_needed():
            await self._arm_dms()
        elif self._dms_ok_t > 0:
            r = await self.rest.schedule_cancel(None)
            self.rate.spend()
            if r.ok:
                self._dms_ok_t = -1e9
                log.info("dead man's switch disarmed: idle with no orders")

    # ------------------------------------------------------------------ startup
    async def _wait_for_network(self) -> list:
        delay = 10.0
        while True:
            try:
                return await self.rest.perp_dexs()
            except NETWORK_ERRORS as e:
                log.error("cannot reach Hyperliquid (%s: %s); retrying in %.0fs%s", type(e).__name__, e, delay,
                          " (if it times out, try bot.ipv4_only = true)" if isinstance(e, asyncio.TimeoutError) else "")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120.0)

    async def _load_markets(self) -> None:
        dexs = await self._wait_for_network()
        need = {c.split(":")[0] for c in self.s.markets} | {m.ref.split(":")[0] for m in self.s.markets.values()}
        dm = {}
        for dex in need:
            dm[dex] = await self.rest.meta_ctxs(dex)
        self.metas = build_metas(dexs, dm)
        for c, cfg in self.s.markets.items():
            if c not in self.metas:
                raise SystemExit(f"Unknown market {c}")
            if cfg.ref not in self.metas:
                raise SystemExit(f"Unknown reference market {cfg.ref} for {c}")
            meta = self.metas[c]
            if meta.delisted:
                raise SystemExit(f"{c} is delisted")
            if meta.growth_mode != "enabled" and not self.s.dry_run:
                raise SystemExit(f"{c} growth mode is {meta.growth_mode!r}: fees would be 10x. Not quoting (kill rule).")
            if cfg.leverage > meta.max_leverage:
                raise SystemExit(f"{c} leverage {cfg.leverage} > max {meta.max_leverage}")
            self.mkts[c] = MarketState(cfg, meta, self.metas[cfg.ref], vol=VolGuard(cfg.vol_guard_mult),
                                       fair=FairValue(cfg.premium_halflife_s, cfg.premium_warmup_s))

    async def _start_live(self) -> None:
        st = await self.rest.state(DEX)
        self._check_margin(st)
        if self.io_account_value <= 0:
            log.error("the account has no USDC on the io dex (clearinghouseState dex=io accountValue=0): "
                      "orders will be refused for margin. Move USDC to the io dex first (README).")
            self.events.write("alert", what="no_io_collateral")
        n = await self.gw.cancel_all(self.metas)
        if n:
            log.warning("cancelled %d leftover order(s) at start", n)
        for c, ms in self.mkts.items():
            r = await self.rest.update_leverage(ms.meta, ms.cfg.leverage, is_cross=False)
            if not r.ok:
                log.error("set isolated %sx on %s failed: %s", ms.cfg.leverage, c, r.error)
                self.events.write("alert", what="leverage_not_set", symbol=c, error=r.error[:200])
        start_ms = int(time.time() * 1000)
        since = min([self.ledger.s.get("last_fill_ms") or start_ms, start_ms - 86400_000])
        rows = await self.rest.fills_since(since - FILL_SYNC_OVERLAP_MS)
        pos = {p["position"]["coin"]: D(p["position"]["szi"]) for p in st.get("assetPositions", [])}
        for c, ms in self.mkts.items():
            ms.position = pos.get(c, D(0))
            mine = sorted((r for r in rows if r.get("coin") == c), key=lambda r: r.get("time", 0))
            if self.ledger.is_new(c):
                self.ledger.init_market(c, ms.position, self._mark(ms), start_ms, [r.get("tid") for r in mine])
            else:
                n = 0
                for r in mine:
                    n += self.ledger.apply_fill(c, r.get("side") == "B", D(r["px"]), D(r["sz"]),
                                                D(r.get("fee") or 0), str(r["tid"]), int(r["time"]), bool(r.get("crossed")))
                if n:
                    log.warning("ledger: booked %d %s fill(s) from while the bot was down", n, c)
            for r in mine:
                self.seen_trades.add(str(r.get("tid")))
        self.ledger.save()
        self._fill_cursor_ms = start_ms
        try:
            fees = await self.rest.user_fees()
            fee_info = {k: fees.get(k) for k in ("userAddRate", "userCrossRate")}
        except Exception:
            fee_info = {}
        self.rate.on_rest(await self.rest.rate_limit())
        await self._sync_funding()
        self.events.write("start", fees=fee_info, rate_surplus=self.rate.surplus,
                          growth={c: ms.meta.growth_mode for c, ms in self.mkts.items()})

    async def _quote_loop(self) -> None:
        while self.running:
            t0 = time.monotonic()
            for ms in self.mkts.values():
                try:
                    await self._quote_market(ms)
                except NETWORK_ERRORS as e:
                    log.warning("quote %s: network error %s", ms.meta.coin, e)
                except Exception:
                    log.exception("quote error %s", ms.meta.coin)
            await asyncio.sleep(max(0.05, 0.25 - (time.monotonic() - t0)))

    async def _on_reconnect(self) -> None:
        await self._reconcile()

    async def run(self) -> None:
        from .stream import Streams
        async with self.rest:
            await self._load_markets()
            if not self.paper:
                await self._start_live()
            else:
                start_ms = int(time.time() * 1000)
                for c, ms in self.mkts.items():
                    if self.ledger.is_new(c):
                        self.ledger.init_market(c, D(0), self._mark(ms), start_ms)
                self.events.write("start", paper=True)
            p = await self._measure_pnl()
            if p is None:
                raise SystemExit("cannot value the bot's positions (no mark); not starting")
            self.pnl = self.pnl0 = p
            self.risk.set_basis("ledger", self.pnl)
            self.riskview = self.risk.update(self.pnl)
            log.info("start: bot pnl=%.4f mode=%s headroom=$%.3f regime=%s", self.pnl, self.riskview.mode.value,
                     self.risk.headroom(self.pnl), self._regime())
            if not self.paper and not self.s.dead_mans_switch:
                log.warning("dead man's switch DISABLED by config: a crash leaves quotes on the book")
            streams = Streams(self.s.ws_url, list(self.mkts), sorted({ms.cfg.ref for ms in self.mkts.values()}),
                              None if self.paper else self.s.account_address, self.on_message,
                              on_reconnect=self._on_reconnect, dump=self.dump, ipv4_only=self.s.ipv4_only)
            self.streams = streams
            tasks = [asyncio.create_task(streams.run()),
                     asyncio.create_task(self._quote_loop()),
                     asyncio.create_task(self._every(1, self._second)),
                     asyncio.create_task(self._every(1, self._confirm_quiet_books)),
                     asyncio.create_task(self._every(15, self._refresh_risk)),
                     asyncio.create_task(self._every(15, self._dead_man)),
                     asyncio.create_task(self._every(30, self._refresh_meta)),
                     asyncio.create_task(self._every(20, self._reconcile)),
                     asyncio.create_task(self._every(300, self._sync_funding)),
                     asyncio.create_task(self._every(60, self._rate_tick)),
                     asyncio.create_task(self._every(60, self._status_tick)),
                     asyncio.create_task(self._every(30, self._watch_ws)),
                     asyncio.create_task(self._every(60, self._daily_summary))]
            try:
                await asyncio.gather(*tasks)
            finally:
                self.running = False
                streams.stop()
                for t in tasks:
                    t.cancel()
                log.info("shutting down: cancelling all orders")
                try:
                    await self.gw.cancel_all(self.metas)
                    if self._dms_live() and self._dms_ok_t > 0:
                        await self.rest.schedule_cancel(None)
                except NETWORK_ERRORS as e:
                    log.error("shutdown cancel failed, Hyperliquid unreachable (%s). %s", e,
                              "The dead man's switch cancels them within 45s." if self._dms_ok_t > 0 else
                              "Run `python -m entropymm cancel-all` once the network is back.")
                except Exception:
                    log.exception("shutdown cancel failed: check open orders manually!")
                self.ledger.save()
                self.events.write("stop", **self._status())
                await self.notify.close()
                if self.dump:
                    self.dump.close()
