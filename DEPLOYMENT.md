# VPS deployment

## Recommended box
- Ubuntu 22.04 or 24.04
- 1 vCPU, 1-2 GB RAM is enough for this bot
- Keep timezone on UTC or default server time; the bot uses market timestamps directly

## Upload
Put the project folder on the server, for example:
```bash
scp -r polymarket_5m_bot root@YOUR_SERVER_IP:/root/
```

## First setup
```bash
cd /root/polymarket_5m_bot
chmod +x setup.sh run_paper.sh run_live.sh install_service.sh
./setup.sh
nano .env
```

## Paper mode first
```bash
cd /root/polymarket_5m_bot
./run_paper.sh
```

What to watch:
- it should wait until the last minute of the current BTC 5-minute market
- during the last minute it should log prices every few seconds
- `reports/touches.json` should grow with last-minute samples
- `reports/latest_report.json` should show touch statistics and trade stats

## Live mode
Only after paper mode looks correct:
```bash
cd /root/polymarket_5m_bot
./run_live.sh
```

Required `.env` fields for live:
- `PRIVATE_KEY`
- `FUNDER_ADDRESS`
- optional `TRACKING_USER_ADDRESS` if you want wallet-level PnL tracking from the Data API

## 24/7 service
```bash
cd /root/polymarket_5m_bot
./install_service.sh
journalctl --user -u polymarket-btc5m -f
```

If `systemctl --user` is unavailable in your VPS session, use a root service instead or run under `tmux`.

## Key files
- `reports/touches.json` — every last-minute observation
- `reports/latest_report.json` — rolling summary
- `journal.json` — trade journal

## Safe rollout
1. Run paper mode for at least several hours.
2. Confirm the bot only watches the last minute.
3. Confirm threshold touches are being logged.
4. Confirm only one trade per market is attempted.
5. Then switch to live mode.
