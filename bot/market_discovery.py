from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import requests


class GammaMarketDiscovery:
    def __init__(self, gamma_url: str, clob_host: str = 'https://clob.polymarket.com'):
        self.gamma_url = gamma_url.rstrip('/')
        self.clob_host = clob_host.rstrip('/')

    def get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        r = requests.get(f"{self.gamma_url}/markets", params={'slug': slug}, timeout=20)
        r.raise_for_status()
        arr = r.json()
        if isinstance(arr, list) and arr:
            market = self._normalize_market(arr[0])
            return self.refresh_live_prices(market)
        return None

    def find_current_btc_5m_markets(self, horizon_steps: int = 8) -> list[dict[str, Any]]:
        now = int(time.time())
        base = now - (now % 300)
        candidate_ts = [
            base - 300,
            base,
            base + 300,
            base + 600,
            base + 900,
            base + 1200,
            base + 1500,
            base + 1800,
        ][:horizon_steps]
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ts in candidate_ts:
            slug = f'btc-updown-5m-{ts}'
            market = self.get_market_by_slug(slug)
            if not market:
                continue
            if slug in seen:
                continue
            seen.add(slug)
            found.append(market)
        found.sort(key=lambda m: self._end_ts(m))
        return found

    def list_recent_btc_5m_markets_via_search(self, query: str = 'bitcoin up or down 5 minutes') -> list[dict[str, Any]]:
        r = requests.get(f"{self.gamma_url}/public-search", params={'q': query}, timeout=20)
        r.raise_for_status()
        data = r.json()
        events = data.get('events', []) if isinstance(data, dict) else []
        out: list[dict[str, Any]] = []
        for e in events:
            slug = (e.get('slug') or '').lower()
            if slug.startswith('btc-updown-5m-'):
                market = self.get_market_by_slug(slug)
                if market:
                    out.append(market)
        out.sort(key=lambda m: self._end_ts(m))
        return out

    def refresh_live_prices(self, market: dict[str, Any]) -> dict[str, Any]:
        token_ids = market.get('_parsed_token_ids') or []
        outcomes = market.get('_parsed_outcomes') or []
        if len(token_ids) != 2 or len(outcomes) != 2:
            market['_live_price_source'] = 'gamma_outcome_prices'
            return market

        refreshed = []
        used_live_data = False
        for outcome, token_id in zip(outcomes, token_ids):
            updated = dict(outcome)
            try:
                book = self.get_order_book(token_id)
            except Exception:
                book = None

            if book:
                asks = book.get('asks') or []
                bids = book.get('bids') or []
                best_ask = self._safe_float(asks[0].get('price')) if asks else None
                best_bid = self._safe_float(bids[0].get('price')) if bids else None
                best_ask_size = self._safe_float(asks[0].get('size')) if asks else None
                last_trade_price = self._safe_float(book.get('last_trade_price'))

                updated['best_ask'] = best_ask
                updated['best_bid'] = best_bid
                updated['best_ask_size'] = best_ask_size
                updated['last_trade_price'] = last_trade_price

                live_price = best_ask if best_ask is not None else last_trade_price
                if live_price is not None:
                    updated['price'] = live_price
                    used_live_data = True

            refreshed.append(updated)

        market['_parsed_outcomes'] = refreshed
        market['_live_price_source'] = 'clob_book' if used_live_data else 'gamma_outcome_prices'
        return market

    def get_order_book(self, token_id: str) -> dict[str, Any] | None:
        r = requests.get(f"{self.clob_host}/book", params={'token_id': token_id}, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None or value == '':
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _end_ts(m: dict[str, Any]) -> float:
        try:
            return datetime.fromisoformat(m['endDate'].replace('Z', '+00:00')).timestamp()
        except Exception:
            return 0.0

    def _normalize_market(self, m: dict[str, Any]) -> dict[str, Any]:
        m = dict(m)
        m['outcomes'] = self._parse_json_field(m.get('outcomes'))
        m['outcomePrices'] = self._parse_json_field(m.get('outcomePrices'))
        m['clobTokenIds'] = self._parse_json_field(m.get('clobTokenIds'))

        outcomes = m.get('outcomes') or []
        prices = m.get('outcomePrices') or []
        token_ids = m.get('clobTokenIds') or []

        parsed_outcomes = []
        try:
            for i, label in enumerate(outcomes):
                price = float(prices[i])
                parsed_outcomes.append({'index': i, 'label': str(label), 'price': price})
        except Exception:
            parsed_outcomes = []

        m['_parsed_outcomes'] = parsed_outcomes
        m['_parsed_token_ids'] = [str(x) for x in token_ids] if isinstance(token_ids, list) else []
        return m

    @staticmethod
    def _parse_json_field(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, str):
            s = val.strip()
            if s.startswith('[') or s.startswith('{'):
                try:
                    return json.loads(s)
                except Exception:
                    return val
        return val
