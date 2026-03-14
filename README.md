# BTC 15min Bot

Automated trading bot for Polymarket BTC 15-minute UP/DOWN markets.

## Strategy

- Monitors each BTC 15-minute market during the final window before resolution
- If UP or DOWN reaches the entry threshold (configurable, default 0.80), buys that side
- At most one trade per market
- Reversal blacklist: if one side spikes above 0.95 early in monitoring, that side is blacklisted for the market

## Entry & Exit Rules

| Rule | Default | Env Var |
|---|---|---|
| Entry threshold | 0.80 | `MIN_CONFIDENCE_PRICE` |
| Max entry price | 0.91 | `MAX_WORST_PRICE` |
| Stop loss | 0.50 | `STOP_LOSS_PRICE` |
| Take profit | 0.99 | `TAKE_PROFIT_PRICE` |
| Profit protect arm | 0.90 | `PROFIT_PROTECT_ARM_PRICE` |
| Profit protect exit | 0.87 | `PROFIT_PROTECT_EXIT_PRICE` |
| Trade size | $20 | `TRADE_SIZE_USD` |
| Min liquidity | 12 | `MIN_LIQUIDITY_ON_BEST_LEVEL` |
| Poll interval | 0.2s | `POLL_INTERVAL_SECONDS` |

## Architecture

### Websocket Price Feed

Real-time prices via Polymarket's websocket (`bot/polymarket_ws.py`). Includes:

- **Sync-gap watchdog**: monitors the timestamp difference between Up and Down outcomes. If one side goes stale while the other keeps updating (sync_gap > threshold), forces a WS reconnect
- **Timestamp reset on reconnect**: clears all quote timestamps when reconnecting so the watchdog doesn't immediately re-trigger on stale values
- **Message count guard**: requires 15+ messages on a connection before the watchdog activates, preventing false triggers during sleep between markets (higher than 5min bot due to longer sleep periods)

### Execution

- FOK (Fill or Kill) market orders via the Polymarket CLOB
- **Real on-chain balance check** before every sell (TP/SL) — queries `get_balance_allowance()` and uses `min(estimate, real_balance)` to avoid "not enough balance" errors
- Automatic retry on failed exits

### Profit Protection

- Arms when position price reaches the arm threshold (default 0.90)
- Exits if price drops back below the exit threshold (default 0.87)
- Floor price on profit protect exit is set at entry price — won't sell below what you paid

## Files

- `main.py` — main loop, entry/exit logic, blacklist, profit protect
- `bot/market_discovery.py` — BTC 15-minute market discovery via Gamma API
- `bot/strategy.py` — entry threshold evaluation
- `bot/execution.py` — paper/live execution, TP/SL/profit protect exits
- `bot/polymarket_ws.py` — websocket price feed with sync-gap watchdog
- `bot/touchlog.py` — last-minute touch logging
- `bot/tracking.py` — settlement tracking and journal
- `bot/fees.py` — fee estimation
- `bot/state.py` — state management
- `bot/live_btc_feed.py` — live BTC price reference

## Setup

```bash
chmod +x setup.sh run_paper.sh run_live.sh
./setup.sh
cp .env.example .env
# Edit .env with your private key and settings
```

## Run

```bash
# Paper mode
./run_paper.sh

# Live mode
./run_live.sh
```

## Credentials

Requires a Polygon wallet private key. The bot derives Polymarket API credentials via `create_or_derive_api_creds()` from the [py-clob-client](https://github.com/Polymarket/py-clob-client).
