#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ~/.config/systemd/user
cp polymarket-btc5m.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable polymarket-btc5m
systemctl --user restart polymarket-btc5m
systemctl --user status polymarket-btc5m --no-pager
