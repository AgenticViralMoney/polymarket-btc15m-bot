# Polymarket 5m BTC bot

A clean Python bot for Polymarket BTC 5-minute UP/DOWN markets.

## Strategy
- Ignore the first ~4 minutes of each BTC 5-minute market.
- Monitor only the final 59 seconds.
- If UP or DOWN reaches $0.80 or higher during that last-minute window, buy that side.
- At most one trade per market.

## What it now logs
- Every last-minute sample to `reports/touches.json`
- Rolling summary to `reports/latest_report.json`
- Trade journal to `journal.json`

## What the report contains
- Total trades, settled trades, wins, losses, win rate, gross PnL, net PnL
- Last-minute touch stats: total samples, threshold-crossing samples, threshold-crossing markets, max price seen

## Important
This is still an execution framework, not proof of edge. Crypto-market taker fees now apply on Polymarket crypto markets, including these short-duration BTC markets, and fee drag matters around the 0.80 entry level ([Polymarket fees](https://docs.polymarket.com/trading/fees)). Market orders are submitted using the official client pattern with FOK semantics and market-specific parameters ([Create Order docs](https://docs.polymarket.com/trading/orders/create), [py-clob-client README](https://github.com/Polymarket/py-clob-client)).

## Setup
```bash
chmod +x setup.sh run_paper.sh run_live.sh install_service.sh
./setup.sh
cp .env.example .env
```

## Run paper mode
```bash
./run_paper.sh
```

## Run live mode
```bash
./run_live.sh
```

## Service mode
```bash
./install_service.sh
```

## Files
- `main.py` — runtime loop
- `bot/market_discovery.py` — direct BTC 5-minute market discovery
- `bot/strategy.py` — last-minute threshold logic
- `bot/execution.py` — paper/live execution
- `bot/touchlog.py` — last-minute touch logging
- `bot/tracking.py` — settlement and reporting
- `DEPLOYMENT.md` — VPS instructions

## Credentials
The official Python client authenticates using your private key, Polygon chain ID 137, a funder address, and derived API credentials via `create_or_derive_api_creds()` ([py-clob-client README](https://github.com/Polymarket/py-clob-client)).
