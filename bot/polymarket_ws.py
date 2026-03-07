from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import websocket


@dataclass
class OutcomeQuote:
    asset_id: str
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade_price: float | None = None
    last_update_ts: float | None = None

    @property
    def buy_price(self) -> float | None:
        if self.best_ask is not None:
            return self.best_ask
        return self.last_trade_price


class PolymarketMarketFeed:
    def __init__(
        self,
        ws_url: str = 'wss://ws-subscriptions-clob.polymarket.com/ws/market',
        stale_after_seconds: float = 5.0,
        sync_tolerance_seconds: float = 1.5,
    ):
        self.ws_url = ws_url
        self.stale_after_seconds = stale_after_seconds
        self.sync_tolerance_seconds = sync_tolerance_seconds
        self._lock = threading.Lock()
        self._quotes: dict[str, OutcomeQuote] = {}
        self._asset_ids: list[str] = []
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_error: str | None = None
        self._connected = False

    def subscribe(self, asset_ids: list[str]) -> None:
        asset_ids = [str(x) for x in asset_ids if x]
        if not asset_ids:
            return
        if asset_ids == self._asset_ids and self._thread and self._thread.is_alive():
            return
        self.stop()
        with self._lock:
            self._quotes = {asset_id: OutcomeQuote(asset_id=asset_id) for asset_id in asset_ids}
        self._asset_ids = asset_ids
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_forever, name='polymarket-market-feed', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        self._ws = None
        self._connected = False

    def wait_until_ready(self, timeout_seconds: float = 10.0) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.is_ready():
                return True
            time.sleep(0.2)
        return self.is_ready()

    def is_ready(self) -> bool:
        now = time.time()
        with self._lock:
            quotes = [self._quotes.get(asset_id) for asset_id in self._asset_ids]
        if not quotes or any(q is None for q in quotes):
            return False
        for q in quotes:
            if q.buy_price is None or q.last_update_ts is None:
                return False
            if now - q.last_update_ts > self.stale_after_seconds:
                return False
        timestamps = [q.last_update_ts for q in quotes if q and q.last_update_ts is not None]
        if len(timestamps) >= 2 and max(timestamps) - min(timestamps) > self.sync_tolerance_seconds:
            return False
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            quotes = {
                asset_id: {
                    'best_bid': q.best_bid,
                    'best_ask': q.best_ask,
                    'last_trade_price': q.last_trade_price,
                    'buy_price': q.buy_price,
                    'last_update_ts': q.last_update_ts,
                }
                for asset_id, q in self._quotes.items()
            }
            timestamps = [q.last_update_ts for q in self._quotes.values() if q.last_update_ts is not None]
        sync_gap = (max(timestamps) - min(timestamps)) if len(timestamps) >= 2 else 0.0
        return {
            'connected': self._connected,
            'ready': self.is_ready(),
            'asset_ids': list(self._asset_ids),
            'last_error': self._last_error,
            'sync_gap_seconds': sync_gap,
            'quotes': quotes,
        }

    def apply_prices_to_market(self, market: dict[str, Any]) -> dict[str, Any]:
        market = dict(market)
        status = self.status()
        market['_ws_status'] = status
        if not status.get('ready'):
            market['_live_price_source'] = 'polymarket_ws_not_ready'
            return market

        parsed = []
        used_live = False
        token_ids = market.get('_parsed_token_ids') or []
        for outcome in market.get('_parsed_outcomes') or []:
            updated = dict(outcome)
            idx = int(updated.get('index', 0))
            token_id = str(token_ids[idx]) if idx < len(token_ids) else None
            if token_id:
                with self._lock:
                    q = self._quotes.get(token_id)
                if q:
                    updated['best_bid'] = q.best_bid
                    updated['best_ask'] = q.best_ask
                    updated['last_trade_price'] = q.last_trade_price
                    updated['quote_age_seconds'] = (time.time() - q.last_update_ts) if q.last_update_ts else None
                    if q.buy_price is not None:
                        updated['price'] = q.buy_price
                        used_live = True
            parsed.append(updated)

        market['_parsed_outcomes'] = parsed
        market['_live_price_source'] = 'polymarket_ws' if used_live else 'gamma_outcome_prices'
        return market

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            self._connected = False
            self._ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self._last_error = repr(exc)
            finally:
                self._ws = None
                self._connected = False
            if self._stop_event.is_set():
                break
            time.sleep(2)

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self._connected = True
        self._last_error = None
        ws.send(json.dumps({
            'assets_ids': self._asset_ids,
            'type': 'market',
            'custom_feature_enabled': True,
        }))

    def _on_close(self, ws: websocket.WebSocketApp, close_status_code: Any, close_msg: Any) -> None:
        self._connected = False
        if close_status_code or close_msg:
            self._last_error = f'close={close_status_code} {close_msg}'

    def _on_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        self._connected = False
        self._last_error = repr(error)

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        receipt_ts = time.time()
        try:
            payload = json.loads(message)
        except Exception:
            return
        if isinstance(payload, list):
            for item in payload:
                self._handle_event(item, receipt_ts)
            return
        if isinstance(payload, dict):
            self._handle_event(payload, receipt_ts)

    def _handle_event(self, event: dict[str, Any], receipt_ts: float) -> None:
        event_type = event.get('event_type')
        if not event_type and 'bids' in event and 'asks' in event and 'asset_id' in event:
            event_type = 'book'

        if event_type == 'book':
            asset_id = str(event.get('asset_id'))
            best_bid = self._extract_best_price(event.get('bids'), reverse=True)
            best_ask = self._extract_best_price(event.get('asks'), reverse=False)
            self._update_quote(asset_id, best_bid=best_bid, best_ask=best_ask, last_update_ts=receipt_ts)
            return

        if event_type == 'best_bid_ask':
            asset_id = str(event.get('asset_id'))
            best_bid = self._safe_float(event.get('best_bid'))
            best_ask = self._safe_float(event.get('best_ask'))
            self._update_quote(asset_id, best_bid=best_bid, best_ask=best_ask, last_update_ts=receipt_ts)
            return

        if event_type == 'price_change':
            for change in event.get('price_changes') or []:
                asset_id = str(change.get('asset_id'))
                best_bid = self._safe_float(change.get('best_bid'))
                best_ask = self._safe_float(change.get('best_ask'))
                self._update_quote(asset_id, best_bid=best_bid, best_ask=best_ask, last_update_ts=receipt_ts)
            return

        if event_type == 'last_trade_price':
            asset_id = str(event.get('asset_id'))
            last_trade_price = self._safe_float(event.get('price'))
            self._update_quote(asset_id, last_trade_price=last_trade_price, last_update_ts=receipt_ts)

    def _update_quote(
        self,
        asset_id: str,
        best_bid: float | None = None,
        best_ask: float | None = None,
        last_trade_price: float | None = None,
        last_update_ts: float | None = None,
    ) -> None:
        asset_id = str(asset_id)
        with self._lock:
            quote = self._quotes.get(asset_id)
            if quote is None:
                quote = OutcomeQuote(asset_id=asset_id)
                self._quotes[asset_id] = quote
            if best_bid is not None:
                quote.best_bid = best_bid
            if best_ask is not None:
                quote.best_ask = best_ask
            if last_trade_price is not None:
                quote.last_trade_price = last_trade_price
            if last_update_ts is not None:
                quote.last_update_ts = last_update_ts

    @staticmethod
    def _extract_best_price(levels: Any, reverse: bool) -> float | None:
        if not levels:
            return None
        best = None
        for level in levels:
            if not isinstance(level, dict):
                continue
            value = PolymarketMarketFeed._safe_float(level.get('price'))
            if value is None:
                continue
            if best is None:
                best = value
            elif reverse and value > best:
                best = value
            elif not reverse and value < best:
                best = value
        return best

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None or value == '':
            return None
        try:
            return float(value)
        except Exception:
            return None
