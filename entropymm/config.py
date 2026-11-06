"""Config loading (TOML, stdlib only). Secrets come from the environment only."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields

NETWORKS = {
    "mainnet": ("https://api.hyperliquid.xyz", "wss://api.hyperliquid.xyz/ws"),
    "testnet": ("https://api.hyperliquid-testnet.xyz", "wss://api.hyperliquid-testnet.xyz/ws"),
}
SESSIONS = {"rth", "pre", "post", "overnight", "weekend"}

# Transitions where io can gap (oracle switch, US open/close, Sunday reopen).
# Times are ET, "HH:MM-HH:MM"; "sun" entries apply on Sundays, the rest on
# trading days. No quotes inside; flatten-only for flatten_lead_min before.
DEFAULT_BLACKOUTS = ["03:55-04:05", "09:25-09:40", "15:55-16:05", "19:55-20:05", "sun 19:50-20:10"]


@dataclass
class MarketCfg:
    symbol: str                          # e.g. "io:SNDK"
    ref_symbol: str = ""                 # fair-value reference, e.g. "xyz:SNDK" ("" = derived io:X -> xyz:X)
    enabled: bool = True
    clip_usd: float = 12.0               # per quote; exchange minimum order value is $10
    max_inventory_usd: float = 24.0      # hard cap on |position| notional (2 clips)
    soft_inventory_frac: float = 0.5     # past this the adding side backs off further
    skew_ticks: float = 2.0              # adding side backs off this many ticks at full inventory
    toxic_backoff_ticks: float = 2.0     # ticks behind the touch per +1.0 of toxicity multiplier
    leverage: int = 3                    # isolated leverage set at start (io is isolated-only)
    requote_ticks: int = 1
    min_requote_ms: int = 750
    # fair-value guard off xyz:  fair = xyz_mid * EWMA(io_mid / xyz_mid)
    premium_halflife_s: float = 120.0
    premium_warmup_s: float = 60.0       # no quotes until the premium has this much history
    guard_bps: float = 1.5               # |fair - io_mid| beyond this -> back off the endangered side
    guard_backoff_ticks: int = 2
    guard_pull_bps: float = 6.0          # beyond this the endangered side is pulled (unless it reduces)
    ref_stale_s: float = 3.0             # xyz feed older than this -> pull both sides
    # volatility guard: fast sigma vs its slow (typical) value
    vol_guard_mult: float = 3.0          # fast >= mult * slow -> only the reducing side is quoted
    # funding lean: back the paying side off when funding is large
    funding_lean_apr: float = 0.0        # 0 = off; e.g. 0.3 = 30%/yr
    funding_lean_ticks: int = 1
    # per-market guards
    weekly_loss_limit_usd: float = 0.0   # 0 = only the account-wide budget
    max_actions_per_min: int = 20        # place/modify budget for repricing (cancels + safety moves exempt)
    quote_regimes: list[str] = field(default_factory=list)  # e.g. ["weekend", "overnight"]; [] = all

    @property
    def ref(self) -> str:
        if self.ref_symbol:
            return self.ref_symbol
        return "xyz:" + self.symbol.split(":", 1)[-1]


@dataclass
class RiskCfg:
    weekly_loss_limit_usd: float = 2.0
    daily_loss_limit_usd: float = 0.75
    caution_frac: float = 1.0            # >= 1 disables (no wider/smaller mode on 1-clip books)
    flatten_only_frac: float = 0.8
    toxic_markout_bps: float = 1.0       # rolling 30s markout below -this -> back off
    kill_markout_bps: float = 3.0        # rolling 50-fill 30s markout below -this -> halt 2h
    toxic_halflife_s: float = 600.0
    stale_book_s: float = 5.0
    max_taker_flatten_usd: float = 40.0  # >= max_inventory_usd + one clip
    max_flatten_slippage_bps: float = 100.0
    max_maker_fee_bps: float = 1.0       # KILL: realised maker fee above this (growth mode ended)
    margin_alert_frac: float = 0.5       # io margin used / io account value
    margin_flatten_frac: float = 0.8
    liq_alert_pct: float = 15.0          # liquidation price this close (%) to mark -> alert
    min_rate_surplus: int = 300          # address request allowance below this -> safety-only + alert


@dataclass
class Settings:
    network: str = "mainnet"
    account_address: str = ""            # the account that holds positions (dedicated subaccount)
    is_subaccount: bool = True           # sign with vaultAddress = account_address
    dry_run: bool = True
    dead_mans_switch: bool = True
    state_dir: str = "state"
    log_dir: str = "logs"
    ipv4_only: bool = False
    blackouts: list[str] = field(default_factory=lambda: list(DEFAULT_BLACKOUTS))
    flatten_lead_min: float = 10.0
    summary_hour_wat: int = 8            # daily Telegram summary at this hour, WAT
    dump_frames: bool = False
    markets: dict[str, MarketCfg] = field(default_factory=dict)
    risk: RiskCfg = field(default_factory=RiskCfg)

    @property
    def rest_url(self) -> str:
        return NETWORKS[self.network][0]

    @property
    def ws_url(self) -> str:
        return NETWORKS[self.network][1]

    @property
    def is_mainnet(self) -> bool:
        return self.network == "mainnet"

    @property
    def agent_key(self) -> str:
        k = os.environ.get("ENTROPY_AGENT_KEY", "")
        if not k and not self.dry_run:
            raise SystemExit("Set ENTROPY_AGENT_KEY (the API/agent wallet private key; trade-only).")
        return k


def _apply(dc, data: dict, where: str):
    names = {f.name for f in fields(dc)}
    for k, v in data.items():
        if k not in names:
            raise SystemExit(f"Unknown config key {where}.{k}")
        setattr(dc, k, v)
    return dc


def load(path: str) -> Settings:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise SystemExit(f"Config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path} is not valid TOML: {e}") from None
    s = Settings()
    bot = dict(raw.get("bot", {}))
    bot.pop("markets", None)
    bot.pop("risk", None)
    _apply(s, bot, "bot")
    if s.network not in NETWORKS:
        raise SystemExit(f"bot.network must be one of {list(NETWORKS)}")
    _apply(s.risk, raw.get("risk", {}), "risk")
    defaults = raw.get("market_defaults", {})
    for sym, m in raw.get("markets", {}).items():
        mc = _apply(MarketCfg(symbol=sym), {**defaults, **m}, f"markets.{sym}")
        if mc.enabled:
            s.markets[sym] = mc
    if not s.markets:
        raise SystemExit("No enabled markets in config.")
    for m in s.markets.values():
        if ":" not in m.symbol:
            raise SystemExit(f"{m.symbol}: use the dex-prefixed coin name, e.g. io:SNDK")
        if not isinstance(m.quote_regimes, list) or not set(m.quote_regimes) <= SESSIONS:
            raise SystemExit(f"markets.{m.symbol}.quote_regimes must be a list from {sorted(SESSIONS)}")
        if not 1 <= m.leverage <= 3:
            raise SystemExit(f"markets.{m.symbol}.leverage must be 1..3")
        if s.risk.max_taker_flatten_usd < m.max_inventory_usd + m.clip_usd:
            raise SystemExit(f"risk.max_taker_flatten_usd must be >= max_inventory_usd + clip_usd ({m.symbol})")
        if m.clip_usd < 10:
            raise SystemExit(f"markets.{m.symbol}.clip_usd must be >= 10 (exchange minimum order value)")
    env_acct = os.environ.get("ENTROPY_ACCOUNT")
    if env_acct:
        s.account_address = env_acct
    if not s.account_address and not s.dry_run:
        raise SystemExit("Set bot.account_address (the bot's dedicated subaccount) or ENTROPY_ACCOUNT.")
    return s
