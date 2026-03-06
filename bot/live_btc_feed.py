from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from statistics import pstdev
from typing import Any, Callable

import websocket


@dataclass
class PriceSample:
    ts: float
    price: float


class LiveBTCFeed:
    def __init__(
        self,
        history_seconds: int = 900,
        volatility_lookback_seconds: int = 120,
        stale_after_seconds: float = 5.0,
    ):
        self.history_seconds = history_seconds
        self.volatility_lookback_seconds = volatility_lookback_seconds
        self.stale_after_seconds = stale_after_seconds
        self._samples: deque[PriceSample] = deque()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: websocket.WebSocketApp | None = None
        self._active_source: str | None = None
        self._last_error: str | None = None

        self._sources: list[dict[str, Any]] = [
            {
                'name': 'coinbase',
                'url': 'wss://ws-feed.exchange.coinbase.com',
                'subscribe': self._coinbase_subscribe,
                'parse': self._coinbase_parse,
            },
            {
                'name': 'kraken',
                'url': 'wss://ws.kraken.com/v2',
                'subscribe': self._kraken_subscribe,
                'parse': self._kraken_parse,
            },
        ]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_forever, name='live-btc-feed', daemon=True)
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

    def wait_until_ready(self, timeout_seconds: float = 20.0) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.is_ready():
                return True
            time.sleep(0.2)
        return self.is_ready()

    def is_ready(self) -> bool:
        latest = self.latest_sample()
        if latest is None:
            return False
        return (time.time() - latest.ts) <= self.stale_after_seconds

    def latest_sample(self) -> PriceSample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def get_status(self) -> dict[str, Any]:
        latest = self.latest_sample()
        return {
            'ready': self.is_ready(),
            'active_source': self._active_source,
            'last_error': self._last_error,
            'latest_price': latest.price if latest else None,
            'latest_ts': latest.ts if latest else None,
        }

    def build_market_signal(self, market: dict[str, Any]) -> dict[str, Any]:
        end_dt = datetime.fromisoformat(market['endDate'].replace('Z', '+00:00'))
        end_ts = end_dt.timestamp()
        now = time.time()
        seconds_left = end_ts - now

        latest = self.latest_sample()
        if latest is None:
            return self._not_ready('waiting for live BTC stream', seconds_left)
        if (now - latest.ts) > self.stale_after_seconds:
            return self._not_ready('live BTC stream is stale', seconds_left)

        market_open_ts = end_ts - 300.0
        open_sample = self._nearest_sample(market_open_ts, tolerance_seconds=3.0)
        if open_sample is None:
            return self._not_ready('waiting for next market so the feed can capture the open price', seconds_left)

        second_prices = self._second_prices(max(market_open_ts, latest.ts - self.volatility_lookback_seconds), latest.ts)
        if len(second_prices) < 15:
            return self._not_ready('building BTC volatility history', seconds_left)

        diffs = [second_prices[i] - second_prices[i - 1] for i in range(1, len(second_prices))]
        sigma_per_second = pstdev(diffs) if len(diffs) >= 2 else 0.0
        volatility_floor = max(open_sample.price * 0.00005, 1.0)
        sigma_per_second = max(sigma_per_second, volatility_floor)

        current_price = latest.price
        move_usd = current_price - open_sample.price
        remaining_seconds = max(seconds_left, 1.0)
        z_score = move_usd / (sigma_per_second * math.sqrt(remaining_seconds))
        probability_up = self._normal_cdf(z_score)
        probability_down = 1.0 - probability_up

        return {
            'ready': True,
            'reason': 'live BTC probability ready',
            'price_source': f"{self._active_source or 'live_feed'}_probability",
            'seconds_left': seconds_left,
            'market_open_price': open_sample.price,
            'current_btc_price': current_price,
            'move_usd': move_usd,
            'sigma_per_second': sigma_per_second,
            'z_score': z_score,
            'probability_up': probability_up,
            'probability_down': probability_down,
            'best_label': 'Up' if probability_up >= probability_down else 'Down',
            'best_probability': max(probability_up, probability_down),
            'market_open_sample_ts': open_sample.ts,
            'latest_sample_ts': latest.ts,
            'active_source': self._active_source,
        }

    def apply_signal_to_market(self, market: dict[str, Any], signal: dict[str, Any]) -> dict[str, Any]:
        market = dict(market)
        market['_signal_context'] = signal
        parsed_token_ids = market.get('_parsed_token_ids') or []
        up_token = parsed_token_ids[0] if len(parsed_token_ids) > 0 else None
        down_token = parsed_token_ids[1] if len(parsed_token_ids) > 1 else None
        market['_parsed_outcomes'] = [
            {'index': 0, 'label': 'Up', 'price': float(signal.get('probability_up', 0.0)), 'token_id': up_token},
            {'index': 1, 'label': 'Down', 'price': float(signal.get('probability_down', 0.0)), 'token_id': down_token},
        ]
        market['_live_price_source'] = signal.get('price_source')
        return market

    def _not_ready(self, reason: str, seconds_left: float) -> dict[str, Any]:
        status = self.get_status()
        status.update({
            'ready': False,
            'reason': reason,
            'seconds_left': seconds_left,
            'price_source': 'live_btc_feed_probability',
        })
        return status

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            for source in self._sources:
                if self._stop_event.is_set():
                    return
                self._active_source = source['name']
                self._last_error = None
                self._ws = websocket.WebSocketApp(
                    source['url'],
                    on_message=self._make_on_message(source['parse']),
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._make_on_open(source['subscribe']),
                )
                try:
                    self._ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as exc:
                    self._last_error = repr(exc)
                finally:
                    self._ws = None
                if self.is_ready():
                    time.sleep(1.0)
                else:
                    time.sleep(2.0)
            time.sleep(1.0)

    def _make_on_open(self, subscribe_fn: Callable[[websocket.WebSocketApp], None]) -> Callable[[websocket.WebSocketApp], None]:
        def _on_open(ws: websocket.WebSocketApp) -> None:
            self._last_error = None
            subscribe_fn(ws)
        return _on_open

    def _make_on_message(self, parse_fn: Callable[[str], PriceSample | None]) -> Callable[[websocket.WebSocketApp, str], None]:
        def _on_message(ws: websocket.WebSocketApp, message: str) -> None:
            try:
                sample = parse_fn(message)
            except Exception as exc:
                self._last_error = f'parse_error={exc!r}'
                return
            if sample is None:
                return
            cutoff = sample.ts - self.history_seconds
            with self._lock:
                self._samples.append(sample)
                while self._samples and self._samples[0].ts < cutoff:
                    self._samples.popleft()
        return _on_message

    def _on_close(self, ws: websocket.WebSocketApp, close_status_code: Any, close_msg: Any) -> None:
        if close_status_code or close_msg:
            self._last_error = f'close={close_status_code} {close_msg}'

    def _on_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        self._last_error = repr(error)

    @staticmethod
    def _coinbase_subscribe(ws: websocket.WebSocketApp) -> None:
        ws.send(json.dumps({
            'type': 'subscribe',
            'product_ids': ['BTC-USD'],
            'channels': ['ticker'],
        }))

    @staticmethod
    def _coinbase_parse(message: str) -> PriceSample | None:
        data = json.loads(message)
        if data.get('type') != 'ticker':
            return None
        price = float(data['price'])
        ts = datetime.fromisoformat(data['time'].replace('Z', '+00:00')).timestamp()
        return PriceSample(ts=ts, price=price)

    @staticmethod
    def _kraken_subscribe(ws: websocket.WebSocketApp) -> None:
        ws.send(json.dumps({
            'method': 'subscribe',
            'params': {
                'channel': 'ticker',
                'symbol': ['BTC/USD'],
            },
        }))

    @staticmethod
    def _kraken_parse(message: str) -> PriceSample | None:
        data = json.loads(message)
        if data.get('channel') != 'ticker':
            return None
        entries = data.get('data') or []
        if not entries:
            return None
        entry = entries[0]
        if 'last' not in entry or 'timestamp' not in entry:
            return None
        price = float(entry['last'])
        ts = datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00')).timestamp()
        return PriceSample(ts=ts, price=price)

    def _nearest_sample(self, target_ts: float, tolerance_seconds: float = 3.0) -> PriceSample | None:
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return None

        best: PriceSample | None = None
        best_distance = None
        for sample in samples:
            distance = abs(sample.ts - target_ts)
            if best is None or distance < best_distance:
                best = sample
                best_distance = distance
        if best is None or best_distance is None or best_distance > tolerance_seconds:
            return None
        return best

    def _second_prices(self, start_ts: float, end_ts: float) -> list[float]:
        with self._lock:
            samples = [s for s in self._samples if start_ts <= s.ts <= end_ts]
        if not samples:
            return []

        buckets: dict[int, float] = {}
        for sample in samples:
            buckets[int(sample.ts)] = sample.price

        out: list[float] = []
        last_price: float | None = None
        for second in range(int(start_ts), int(end_ts) + 1):
            if second in buckets:
                last_price = buckets[second]
            if last_price is not None:
                out.append(last_price)
        return out

    @staticmethod
    def _normal_cdf(value: float) -> float:
        return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))
