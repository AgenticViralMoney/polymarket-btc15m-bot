from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams

from bot.polymarket_ws import PolymarketMarketFeed


class GammaMarketDiscovery:
    def __init__(self, gamma_url: str, clob_host: str = 'https://clob.polymarket.com', chain_id: int | None = None):
        self.gamma_url = gamma_url.rstrip('/')
        self.clob_host = clob_host.rstrip('/')
        self.market_feed = PolymarketMarketFeed()
        self.price_client = ClobClient(self.clob_host, chain_id)
        self._subscribed_slug: str | None = None

    def get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        r = requests.get(f"{self.gamma_url}/markets", params={'slug': slug}, timeout=20)
        r.raise_for_status()
        arr = r.json()
        if isinstance(arr, list) and arr:
            market = self._normalize_market(arr[0])
            return self.prepare_market(market)
        return None

    def prepare_market(self, market: dict[str, Any], wait_ready_timeout_seconds: float = 3.0) -> dict[str, Any]:
        market = self._normalize_market(market)
        token_ids = market.get('_parsed_token_ids') or []
        slug = market.get('slug')
        if token_ids:
            if slug != self._subscribed_slug:
                self.market_feed.subscribe(token_ids)
                self._subscribed_slug = slug
            self.market_feed.wait_until_ready(timeout_seconds=wait_ready_timeout_seconds)
            market = self.market_feed.apply_prices_to_market(market)
        market = self._apply_clob_buy_prices(market)
        return market

    def refresh_active_market(self, market: dict[str, Any]) -> dict[str, Any]:
        market = self._normalize_market(market)
        token_ids = market.get('_parsed_token_ids') or []
        slug = market.get('slug')
        if token_ids and slug == self._subscribed_slug:
            market = self.market_feed.apply_prices_to_market(market)
            return self._apply_clob_buy_prices(market)
        return self.prepare_market(market)

    def find_current_btc_15m_markets(self, horizon_steps: int = 8) -> list[dict[str, Any]]:
        now = int(time.time())
        base = now - (now % 900)
        candidate_ts = [
            base - 900,
            base,
            base + 900,
            base + 1800,
            base + 2700,
            base + 3600,
            base + 4500,
            base + 5400,
        ][:horizon_steps]
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ts in candidate_ts:
            slug = f'btc-updown-15m-{ts}'
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

    def list_recent_btc_15m_markets_via_search(self, query: str = 'bitcoin up or down 15 minutes') -> list[dict[str, Any]]:
        r = requests.get(f"{self.gamma_url}/public-search", params={'q': query}, timeout=20)
        r.raise_for_status()
        data = r.json()
        events = data.get('events', []) if isinstance(data, dict) else []
        out: list[dict[str, Any]] = []
        for e in events:
            slug = (e.get('slug') or '').lower()
            if slug.startswith('btc-updown-15m-'):
                market = self.get_market_by_slug(slug)
                if market:
                    out.append(market)
        out.sort(key=lambda m: self._end_ts(m))
        return out

    def _apply_clob_buy_prices(self, market: dict[str, Any]) -> dict[str, Any]:
        market = dict(market)
        outcomes = market.get('_parsed_outcomes') or []
        token_ids = market.get('_parsed_token_ids') or []
        if not outcomes or len(outcomes) != len(token_ids):
            return market
        try:
            prices = self.price_client.get_prices([BookParams(token_id=token_id, side='BUY') for token_id in token_ids])
        except Exception as exc:
            market['_clob_price_error'] = repr(exc)
            return market

        parsed_outcomes = []
        used_live = False
        for outcome, token_id in zip(outcomes, token_ids):
            updated = dict(outcome)
            token_prices = prices.get(token_id, {}) if isinstance(prices, dict) else {}
            buy_price = token_prices.get('BUY') if isinstance(token_prices, dict) else None
            try:
                buy_price = float(buy_price) if buy_price is not None else None
            except Exception:
                buy_price = None
            if buy_price is not None:
                updated['price'] = buy_price
                updated['buy_price'] = buy_price
                used_live = True
            parsed_outcomes.append(updated)

        market['_parsed_outcomes'] = parsed_outcomes
        if used_live:
            market['_live_price_source'] = 'clob_get_prices'
        return market

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
                price = float(prices[i]) if i < len(prices) else 0.5
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
