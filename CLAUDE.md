# CLAUDE.md: entropymm

Maker-only market-making bot on Entropy (HIP-3 perp dex "io" on Hyperliquid),
built to generate volume as cheaply as possible, as a bet on a FUTURE Entropy
reward program (none announced). ~$19 capital, $2.00/week loss budget. Track
cost per $ of volume from day one. Owner deploys real money with this code:
correctness and safety beat features. Ported from arcus-mm (copied, never
imported: two live bots must not share code, state, logs, keys or budgets).

## Commands
- Tests: `python -m pytest -q tests` (must stay green; add a test with every fix)
- Read-only market scan: `python -m entropymm scan -c config.toml`
- Paper (no orders): `dry_run = true`, `python -m entropymm run -c config.toml`
- Report: `python -m entropymm report --week` (or `--days 1`)
- Emergency: `python -m entropymm cancel-all -c config.toml`
- Research: `python tools/hl_record.py 3600 rec.jsonl` then
  `python tools/hl_join.py rec*.jsonl --coin io:SNDK --g off,1,1.5,2,3 --session overnight`

## Hard invariants (never break these)
1. Every quote is post-only (`tif "Alo"`). Taker orders exist only in
   `_emergency_flatten`: reduce-only IOC, notional capped by
   `max_taker_flatten_usd`, price bounded by mark +/- `max_flatten_slippage_bps`.
2. Dedicated subaccount; the owner never trades the bot's markets on it. Never
   self-trade. Don't move money in and out (deposits < 5 USDC are lost, $1 per withdrawal).
3. Loss budget = bot ledger (fills + fees + funding, valued at the EXCHANGE
   position), persisted in `state/` and never resettable by a restart. A breach
   needs two consecutive readings to halt; an unreadable risk file halts forever.
4. Secrets only via env (`ENTROPY_AGENT_KEY`, Telegram vars). Never commit
   config.toml, state/, logs/, .env, recordings, or anything with a key.
5. Every write goes through the rate governor (RateBudget + per-market
   `max_actions_per_min`). Never reprice on every book tick (~28k changes/h);
   only when our order is off the desired price.
6. On any exit path: cancel all orders. In live, no order is ever placed without
   a freshly armed dead man's switch (armed only while quoting/holding orders).
7. KILL RULES: growth mode off or realised maker fee > `max_maker_fee_bps` (1 bp),
   io delisted -> halt (persisted, cleared by hand). xyz feed stale -> no
   two-sided quotes. Margin used >= `margin_flatten_frac` -> flatten-only.

## Architecture (entropymm/)
meta.py (asset ids, 5-sig-fig ticks, wire format) · hl.py (async REST + Signer;
SDK used ONLY for `sign_l1_action`) · stream.py (WS l2Book/bbo/trades/orderUpdates/
userFills, ping 30 s) · session.py (ET sessions, blackout calendar, UTC budget
clock, WAT) · quoting.py (pure) · estimators.py (EwmaVol, VolGuard, FairValue,
Markouts per market per reference) · queue.py (back-of-queue model for paper +
sim) · risk.py · ledger.py · ratebudget.py · gateway.py (Live/Paper) ·
alerts.py · metrics.py (EventLog, report) · bot.py (main loop) · __main__.py

Design decision: the official SDK (hyperliquid-python-sdk 0.24) is synchronous
(requests) with a threaded websocket, so the bot uses its own asyncio client and
imports the SDK only for signing. Tests check our action bytes and signatures
are identical to the SDK's.

## Verified facts (live API, 2026-11-06)
- REST `https://api.hyperliquid.xyz` (`/info`, `/exchange`), WS `wss://api.hyperliquid.xyz/ws`.
- `perpDexs`: io = index 10 (EntropyIO). Asset id = 100000 + 10*10000 + index in
  `meta(dex="io").universe` -> io:SNDK = 200002 (SDK: builder dexs at 110000 + i*10000).
- io universe rows: `growthMode: "enabled"`, `onlyIsolated: true`, marginMode
  `noCross` or `strictIsolated`; delisted rows have `isDelisted: true` and no growthMode.
  Delisted on 2026-11-06: SBE, H100, MOONSHOT, SKHX, GOOGL, MU.
- io:SNDK szDecimals 4, maxLeverage 15, ~$43M/day, tick 0.01 at ~$828 (0.12 bp),
  book 1-3 ticks wide. Funding multiplier 0.5 (io:SNDK), hourly; ctx `funding`
  0.0000184/h (~16%/yr) at the time.
- WS `l2Book` data `{coin,time,levels:[[{px,sz,n}],[...]]}`; `bbo` data
  `{coin,time,bbo:[bid|null,ask|null]}`; `trades` rows have `side` B/A (taker), `tid`, `users`.
- `userFills` rows: `{coin,px,sz,side,time,startPosition,dir,closedPnl,hash,oid,crossed,
  fee,tid,cloid,feeToken}`; `fee` is per fill, negative = rebate. `crossed` = taker.
- `userFunding` rows: `{time, delta:{type:"funding",coin,usdc,szi,fundingRate}}`, on the hour.
- `clearinghouseState` with `dex:"io"`: `marginSummary.accountValue/totalMarginUsed`,
  `assetPositions[].position.{coin,szi,leverage{type:"isolated"},liquidationPx,marginUsed}`.
- `userRateLimit`: `nRequestsCap = cumVlm + 10000`, `nRequestsUsed` counts up
  (allowance = 10k + 1 request per $1 traded).
- `openOrders` with `dex:"io"`: `{coin,side,limitPx,sz,oid,timestamp,origSz,cloid}`.
- SDK docstring: scheduleCancel time >= now + 5 s; max 10 triggers per day, reset 00:00 UTC.

## UNVERIFIED (check in the HL docs / live before relying on it)
hyperliquid.gitbook.io and docs.entropy.io were blocked in the build sandbox.
- How a batched order/cancel counts against the address allowance (assumed: per order).
- What a crossing ALO *modify* does, and whether modify keeps the oid (handled both ways;
  a refused modify is treated as "order gone": cancel, drop, re-place).
- scheduleCancel eligibility/minimums for a small account (arm failure = no orders placed).
- Whether reduce-only orders below $10 are accepted (dust positions; `dust_position` alert).
- Whether io margin comes from the main perp balance or must be moved to the io dex
  (`no_io_collateral` alert at start if io accountValue is 0).
- How a HIP-3 trading halt looks in meta/ctx (bot pulls quotes when ctx midPx is null).
- orderUpdates status strings beyond open/filled/canceled (anything else = terminal + logged).

## Measured
- 2026-11-06 paper smoke run, US RTH, 100 s: joins the touch, ~1 fill / 10 s,
  guard engaged at -12 bp during a fast move, inventory cap held at 2 clips.
  ~1800 actions/h at 30/min -> default max_actions_per_min lowered to 20.
- Sim numbers are optimistic (Arcus: sim 1.4x high on volume, 0.4-2 bp too cheap).

## Style
Python 3.11+, asyncio, Decimal for prices/sizes sent to the exchange, stdlib TOML
config, small modules, quoting.py pure. Commit messages imperative, explain the why.
