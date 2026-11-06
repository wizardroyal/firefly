"""CLI:  python -m entropymm {run,scan,report,cancel-all,alert-test} [-c config.toml]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone


def _setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(os.path.join(log_dir, "bot.log"))])


async def _scan(cfg) -> None:
    """Read-only: every io market with its xyz twin: spread, tick, volume, funding, premium."""
    from .hl import HlRest
    from .meta import tick_for
    async with HlRest(cfg.rest_url, None, "", ipv4_only=cfg.ipv4_only) as r:
        io_m, io_c = await r.meta_ctxs("io")
        xyz_m, xyz_c = await r.meta_ctxs("xyz")
        xyz = {u["name"].split(":")[1]: c for u, c in zip(xyz_m["universe"], xyz_c)}
        print(f"{'market':<12}{'mid':>10}{'spread_bp':>10}{'tick_bp':>8}{'vol24h_$k':>11}{'fund_apr%':>10}"
              f"{'prem_bp':>8}  growth")
        for u, c in sorted(zip(io_m["universe"], io_c), key=lambda uc: -float(uc[1].get("dayNtlVlm") or 0)):
            if u.get("isDelisted"):
                continue
            mid = float(c.get("midPx") or c.get("markPx") or 0)
            b = await r.l2(u["name"])
            lv = b.get("levels") or [[], []]
            sp = ((float(lv[1][0]["px"]) - float(lv[0][0]["px"])) / mid * 1e4) if lv[0] and lv[1] and mid else float("nan")
            from decimal import Decimal
            tk = float(tick_for(Decimal(str(mid)), int(u["szDecimals"]))) / mid * 1e4 if mid else 0
            x = xyz.get(u["name"].split(":")[1])
            xm = float((x or {}).get("midPx") or 0)
            prem = (mid / xm - 1) * 1e4 if xm else float("nan")
            print(f"{u['name']:<12}{mid:>10.3f}{sp:>10.2f}{tk:>8.2f}{float(c.get('dayNtlVlm') or 0) / 1e3:>11,.0f}"
                  f"{float(c.get('funding') or 0) * 24 * 365 * 100:>10.1f}{prem:>8.1f}  {u.get('growthMode')}")
            await asyncio.sleep(0.05)


def _report(cfg, since: str | None) -> None:
    from .metrics import format_report, summarize
    tag = f"{cfg.network}{'-paper' if cfg.dry_run else ''}"
    s = summarize(cfg.log_dir, tag, since)
    if not s["markets"]:
        print(f"no fills in {cfg.log_dir}/{tag}-*.jsonl" + (f" since {since}" if since else ""))
    wl = (s["last_status"] or {}).get("week_loss")
    print(format_report(s, wl if since else None))
    if s["last_status"]:
        ls = s["last_status"]
        print("last status:", {k: ls.get(k) for k in ("ts", "mode", "reason", "week_loss", "day_loss", "headroom",
                                                       "bot_pnl", "rate_surplus", "margin_frac")})


async def _cancel_all(cfg) -> None:
    from .hl import HlRest, Signer
    from .meta import build_metas
    signer = Signer(cfg.agent_key, cfg.account_address if cfg.is_subaccount else None, cfg.is_mainnet)
    async with HlRest(cfg.rest_url, signer, cfg.account_address, ipv4_only=cfg.ipv4_only) as r:
        metas = build_metas(await r.perp_dexs(), {"io": await r.meta_ctxs("io")})
        rows = await r.open_orders("io")
        pairs = [(metas[o["coin"]].asset, int(o["oid"])) for o in rows if o["coin"] in metas]
        print(f"cancelling {len(pairs)} order(s):", await r.cancel_many(pairs))


async def _alert_test(cfg) -> None:
    from .alerts import Notifier
    n = Notifier(cfg.network, cfg.ipv4_only)
    if not n.enabled:
        print("Telegram is off: set ENTROPY_TELEGRAM_TOKEN and ENTROPY_TELEGRAM_CHAT_ID (README).")
        return
    n.send("test", "test alert: if you can read this, alerts work", force=True)
    await n.close(timeout=15)
    print("sent (if nothing arrives, check bot.log for 'telegram alert not sent')")


def main() -> None:
    ap = argparse.ArgumentParser(prog="entropymm")
    ap.add_argument("cmd", choices=["run", "scan", "report", "cancel-all", "alert-test"])
    ap.add_argument("-c", "--config", default="config.toml")
    ap.add_argument("--week", action="store_true", help="report: this budget week (from Monday 00:00 UTC)")
    ap.add_argument("--days", type=float, help="report: the last N days")
    ap.add_argument("--dump-frames", action="store_true", help="run: raw WS + write responses to logs/frames.jsonl")
    a = ap.parse_args()
    from .config import load
    cfg = load(a.config)
    cfg.dump_frames = a.dump_frames
    if a.cmd == "run":
        _setup_logging(cfg.log_dir)
        from .bot import Bot
        logging.getLogger("emm").info("starting %s on %s, markets=%s", "PAPER (no orders sent)" if cfg.dry_run
                                      else "LIVE", cfg.network, list(cfg.markets))

        def _term(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)      # systemd stop -> clean cancel-all
        try:
            asyncio.run(Bot(cfg).run())
        except KeyboardInterrupt:
            pass
    elif a.cmd == "scan":
        asyncio.run(_scan(cfg))
    elif a.cmd == "report":
        since = None
        if a.week:
            from .session import week_start
            since = week_start().isoformat(timespec="seconds")
        elif a.days:
            since = (datetime.now(timezone.utc) - timedelta(days=a.days)).isoformat(timespec="seconds")
        _report(cfg, since)
    elif a.cmd == "cancel-all":
        asyncio.run(_cancel_all(cfg))
    elif a.cmd == "alert-test":
        asyncio.run(_alert_test(cfg))


if __name__ == "__main__":
    main()
