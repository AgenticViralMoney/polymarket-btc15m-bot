#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp -n .env.example .env || true
chmod +x run_paper.sh run_live.sh install_service.sh

echo "Setup complete. Edit .env, then use one of:"
echo "./run_paper.sh"
echo "./run_live.sh"
echo "./install_service.sh"
