# entropymm

A maker-only market-making bot for **Entropy** (HIP-3 dex `io` on Hyperliquid).
Its goal is the most volume for the least cost, as a bet on a future Entropy
reward program (none is announced yet). It starts with io:SNDK, $12 clips
and a $24 inventory cap, inside a $2/week loss budget.

How it quotes: it joins the io touch at the back of the queue. It uses
`xyz:SNDK` (trade.xyz, 10-100x the volume, same API) as the fair-value
reference: `fair = xyz_mid * EWMA(io_mid / xyz_mid)`. When io lags xyz, the
quote that is about to be picked off backs away. Safety rules are ported from
arcus-mm: two-reading budget halts, a persisted ledger, REST as the record,
ghost and orphan sweeps, and a dead man's switch. See CLAUDE.md for the
invariants and the verified API facts.

## Setup
```bash
python -m pip install -r requirements.txt
cp config.example.toml config.toml   # edit account_address
python -m pytest -q tests
```
On Hyperliquid:
1. Create a **dedicated subaccount** for the bot and put the USDC there. Make
   sure the margin is usable on the `io` dex: the bot alerts `no_io_collateral`
   at start if the io account value is 0.
2. Create an **API (agent) wallet** in the Hyperliquid app. It can trade but
   cannot withdraw. Export its private key:
   ```bash
   export ENTROPY_AGENT_KEY=0x...
   ```
3. Optional Telegram alerts: set `ENTROPY_TELEGRAM_TOKEN` and
   `ENTROPY_TELEGRAM_CHAT_ID`, then run `python -m entropymm alert-test`.

## Phases (each gated on evidence)
0. **Research, no orders.** Record >= 60 min in each session (US RTH,
   extended hours, overnight, weekend) and across at least one transition:
   `python tools/hl_record.py 3600 rec-<session>-<date>.jsonl`. Then sweep
   the guard: `python tools/hl_join.py rec-*.jsonl --g off,1,1.5,2,3 --session overnight`.
1. **Paper** on the live feed (`dry_run = true`), >= 2 sessions of each kind.
   GO/NO-GO: the paper 30 s markout vs fair must be >= -0.3 bp over >= 200
   fills, in the sessions we intend to quote (`report --week`).
2. **Live** with $12 clips and a $24 cap, `weekly_loss_limit_usd = 1.0` for
   the first 48 h, and `quote_regimes` limited to the sessions that passed.
3. The full $2/week. Add io:NBIS only after a clean live week on SNDK.

## Running
```bash
python -m entropymm run -c config.toml
```
```bash
python -m entropymm report --week -c config.toml
```
```bash
python -m entropymm cancel-all -c config.toml
```
`deploy/entropymm.service` is a systemd unit (it reads secrets from
`/etc/entropymm.env`).
