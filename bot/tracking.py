from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json
import requests

from bot.touchlog import summarize_touches


class SettlementTracker:
    def __init__(self, gamma_url: str, data_url: str, journal, user_address: str = ''):
        self.gamma_url = gamma_url.rstrip('/')
        self.data_url = data_url.rstrip('/')
        self.journal = journal
        self.user_address = user_address

    def settle_all(self, live_mode: bool) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        for trade in self.journal.unsettled_trades():
            update = self.try_settle_trade(trade, live_mode=live_mode)
            if update:
                updates.append(update)
        return updates

    def try_settle_trade(self, trade: dict[str, Any], live_mode: bool) -> dict[str, Any] | None:
        market = self._get_market(trade['market_slug'])
        if not market:
            return None
        if not market.get('closed'):
            return None

        if live_mode and self.user_address:
            closed = self._get_closed_position(trade)
            if closed:
                updates = {
                    'settled_at': datetime.now(timezone.utc).isoformat(),
                    'settlement_source': 'data_api_closed_positions',
                    'winner_outcome': closed.get('outcome'),
                    'payout_usdc': closed.get('totalBought', 0) + closed.get('realizedPnl', 0),
                    'gross_pnl_usdc': closed.get('realizedPnl', 0) + trade.get('entry_fee_usdc_est', 0),
                    'net_pnl_usdc': closed.get('realizedPnl', 0),
                    'status': 'settled',
                }
                self.journal.update_trade(trade['trade_id'], updates)
                return {'trade_id': trade['trade_id'], **updates}

        outcome_prices = market.get('_parsed_outcomes') or []
        winner = None
        for item in outcome_prices:
            if abs(float(item['price']) - 1.0) < 1e-9:
                winner = item
                break
        if winner is None:
            return None

        won = int(trade['outcome_index']) == int(winner['index'])
        payout = float(trade['shares_net']) if won else 0.0
        gross_pnl = payout - float(trade['amount_usd']) + float(trade['entry_fee_usdc_est'])
        net_pnl = payout - float(trade['amount_usd'])
        updates = {
            'settled_at': datetime.now(timezone.utc).isoformat(),
            'settlement_source': 'gamma_outcome_prices',
            'winner_outcome': winner['label'],
            'payout_usdc': round(payout, 6),
            'gross_pnl_usdc': round(gross_pnl, 6),
            'net_pnl_usdc': round(net_pnl, 6),
            'status': 'settled',
        }
        self.journal.update_trade(trade['trade_id'], updates)
        return {'trade_id': trade['trade_id'], **updates}

    def _get_market(self, slug: str) -> dict[str, Any] | None:
        r = requests.get(f'{self.gamma_url}/markets', params={'slug': slug}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        market = data[0]
        market['outcomes'] = json.loads(market['outcomes']) if isinstance(market.get('outcomes'), str) else market.get('outcomes')
        market['outcomePrices'] = json.loads(market['outcomePrices']) if isinstance(market.get('outcomePrices'), str) else market.get('outcomePrices')
        parsed = []
        try:
            for i, label in enumerate(market.get('outcomes') or []):
                parsed.append({'index': i, 'label': label, 'price': float((market.get('outcomePrices') or [])[i])})
        except Exception:
            parsed = []
        market['_parsed_outcomes'] = parsed
        return market

    def _get_closed_position(self, trade: dict[str, Any]) -> dict[str, Any] | None:
        r = requests.get(
            f'{self.data_url}/closed-positions',
            params={
                'user': self.user_address,
                'market': trade['condition_id'],
                'limit': 50,
            },
            timeout=20,
        )
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            if str(row.get('asset')) == str(trade['token_id']):
                return row
        return None


def build_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(trades)
    settled = [t for t in trades if t.get('settled_at')]
    wins = [t for t in settled if (t.get('net_pnl_usdc') or 0) > 0]
    losses = [t for t in settled if (t.get('net_pnl_usdc') or 0) < 0]
    breakeven = [t for t in settled if (t.get('net_pnl_usdc') or 0) == 0]
    net_pnl = sum(float(t.get('net_pnl_usdc') or 0) for t in settled)
    gross_pnl = sum(float(t.get('gross_pnl_usdc') or 0) for t in settled)
    avg_entry = sum(float(t.get('entry_price') or 0) for t in trades) / total if total else 0.0
    return {
        'total_trades': total,
        'settled_trades': len(settled),
        'open_trades': total - len(settled),
        'wins': len(wins),
        'losses': len(losses),
        'breakeven': len(breakeven),
        'win_rate': (len(wins) / len(settled)) if settled else 0.0,
        'gross_pnl_usdc': round(gross_pnl, 6),
        'net_pnl_usdc': round(net_pnl, 6),
        'avg_entry_price': round(avg_entry, 6),
    }


def write_summary_report(trades: list[dict[str, Any]], reports_dir: str, touches_path: str | None = None, threshold: float | None = None) -> str:
    touch_summary = None
    if touches_path and Path(touches_path).exists() and threshold is not None:
        try:
            touches = json.loads(Path(touches_path).read_text()).get('touches', [])
            touch_summary = summarize_touches(touches, threshold)
        except Exception:
            touch_summary = None

    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'summary': build_summary(trades),
        'touch_summary': touch_summary,
        'trades': trades,
    }
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / 'latest_report.json'
    path.write_text(json.dumps(report, indent=2))
    return str(path)
