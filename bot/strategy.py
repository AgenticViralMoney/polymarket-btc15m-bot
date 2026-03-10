from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class StrategyDecision:
    should_trade: bool
    reason: str
    chosen_outcome: str | None = None
    chosen_token_id: str | None = None
    chosen_price: float | None = None
    chosen_outcome_index: int | None = None
    seconds_to_resolution: float | None = None
    details: dict[str, Any] | None = None


class Strategy:
    def __init__(
        self,
        min_confidence_price: float,
        max_entry_price: float,
        seconds_before_resolution: int,
        skip_seconds_delayed_markets: bool = True,
        early_entry_floor: float = 0.72,
        early_signal_min_probability: float = 0.83,
    ):
        self.min_confidence_price = min_confidence_price
        self.max_entry_price = max_entry_price
        self.seconds_before_resolution = seconds_before_resolution
        self.skip_seconds_delayed_markets = skip_seconds_delayed_markets
        self.early_entry_floor = early_entry_floor
        self.early_signal_min_probability = early_signal_min_probability

    def _extract_signal_bias(self, market: dict[str, Any]) -> dict[str, Any]:
        signal = market.get('_signal_context') or {}
        if not signal:
            return {'ready': False, 'reason': 'missing signal context'}

        if not signal.get('ready'):
            return {'ready': False, 'reason': signal.get('reason') or 'spot BTC signal not ready'}

        prob_up = signal.get('probability_up')
        prob_down = signal.get('probability_down')

        try:
            prob_up = float(prob_up) if prob_up is not None else None
            prob_down = float(prob_down) if prob_down is not None else None
        except Exception:
            prob_up = None
            prob_down = None

        if prob_up is None and prob_down is None:
            return {'ready': False, 'reason': 'spot BTC signal probabilities unavailable'}

        if prob_up is None:
            best_label = 'Down'
            best_probability = prob_down
        elif prob_down is None:
            best_label = 'Up'
            best_probability = prob_up
        elif prob_up >= prob_down:
            best_label = 'Up'
            best_probability = prob_up
        else:
            best_label = 'Down'
            best_probability = prob_down

        return {
            'ready': True,
            'best_label': best_label,
            'best_probability': float(best_probability),
            'probability_up': prob_up,
            'probability_down': prob_down,
            'price_source': signal.get('price_source'),
        }

    def evaluate(self, market: dict[str, Any]) -> StrategyDecision:
        now = datetime.now(timezone.utc)
        end_dt = datetime.fromisoformat(market['endDate'].replace('Z', '+00:00'))
        seconds_left = (end_dt - now).total_seconds()

        if seconds_left <= 0:
            return StrategyDecision(False, 'market already expired', seconds_to_resolution=seconds_left)

        if seconds_left > self.seconds_before_resolution:
            return StrategyDecision(False, f'too early: {seconds_left:.1f}s left', seconds_to_resolution=seconds_left)

        if not market.get('acceptingOrders'):
            return StrategyDecision(False, 'market not accepting orders', seconds_to_resolution=seconds_left)

        if not market.get('enableOrderBook'):
            return StrategyDecision(False, 'order book disabled', seconds_to_resolution=seconds_left)

        if market.get('closed'):
            return StrategyDecision(False, 'market already closed', seconds_to_resolution=seconds_left)

        if self.skip_seconds_delayed_markets and market.get('secondsDelay') is not None:
            return StrategyDecision(
                False,
                f"market has execution delay: {market.get('secondsDelay')}s",
                seconds_to_resolution=seconds_left,
            )

        source = market.get('_live_price_source')
        ws_status = market.get('_ws_status') or {}
        sync_gap = ws_status.get('sync_gap_seconds')

        if source not in {'polymarket_ws', 'clob_get_prices'}:
            return StrategyDecision(
                False,
                f"live price source not ready ({source})",
                seconds_to_resolution=seconds_left,
                details={'price_source': source, 'ws_status': ws_status},
            )

        if source == 'polymarket_ws' and sync_gap is not None and float(sync_gap) > 1.5:
            return StrategyDecision(
                False,
                f'websocket quotes out of sync: gap={float(sync_gap):.3f}s',
                seconds_to_resolution=seconds_left,
                details={'price_source': source, 'ws_status': ws_status},
            )

        outcomes = market.get('_parsed_outcomes') or []
        token_ids = market.get('_parsed_token_ids') or []
        if len(outcomes) != 2 or len(token_ids) != 2:
            return StrategyDecision(False, 'unexpected market structure', seconds_to_resolution=seconds_left)

        valid_outcomes = [
            outcome for outcome in outcomes
            if outcome.get('price') is not None and 0.0 <= float(outcome['price']) <= 1.0
        ]
        if len(valid_outcomes) != 2:
            return StrategyDecision(
                False,
                'incomplete live prices',
                seconds_to_resolution=seconds_left,
                details={'price_source': source, 'outcomes': outcomes},
            )

        up_price = float(valid_outcomes[0]['price'])
        down_price = float(valid_outcomes[1]['price'])
        price_sum = up_price + down_price
        if price_sum > 1.15:
            return StrategyDecision(
                False,
                f'inconsistent two-sided prices: up+down={price_sum:.3f}',
                seconds_to_resolution=seconds_left,
                details={'up_price': up_price, 'down_price': down_price, 'price_source': source},
            )

        best = max(valid_outcomes, key=lambda x: float(x['price']))
        best_idx = int(best['index'])
        best_price = float(best['price'])
        best_label = best['label']

        if best_price >= self.max_entry_price:
            return StrategyDecision(
                False,
                f"best side above max entry price: {best_label} @ {best_price:.3f}",
                seconds_to_resolution=seconds_left,
                details={
                    'best_label': best_label,
                    'best_price': best_price,
                    'max_entry_price': self.max_entry_price,
                    'price_source': source,
                },
            )

        if best_price >= self.min_confidence_price:
            return StrategyDecision(
                True,
                'standard entry criteria met',
                chosen_outcome=best_label,
                chosen_token_id=token_ids[best_idx],
                chosen_price=best_price,
                chosen_outcome_index=best_idx,
                seconds_to_resolution=seconds_left,
                details={
                    'entry_type': 'standard',
                    'best_label': best_label,
                    'best_price': best_price,
                    'price_source': source,
                },
            )

        if best_price < self.early_entry_floor:
            return StrategyDecision(
                False,
                f"best side below early-entry floor: {best_label} @ {best_price:.3f}",
                seconds_to_resolution=seconds_left,
                details={
                    'entry_type': 'early',
                    'best_label': best_label,
                    'best_price': best_price,
                    'early_entry_floor': self.early_entry_floor,
                    'price_source': source,
                },
            )

        signal = self._extract_signal_bias(market)
        if not signal.get('ready'):
            return StrategyDecision(
                False,
                f"early entry blocked: {signal.get('reason') or 'spot BTC signal not ready'}",
                seconds_to_resolution=seconds_left,
                details={
                    'entry_type': 'early',
                    'best_label': best_label,
                    'best_price': best_price,
                    'price_source': source,
                    'signal': signal,
                },
            )

        signal_best_label = signal['best_label']
        signal_best_probability = float(signal['best_probability'])

        if signal_best_label != best_label:
            return StrategyDecision(
                False,
                f"early entry blocked: spot BTC signal disagrees ({signal_best_label} vs {best_label})",
                seconds_to_resolution=seconds_left,
                details={
                    'entry_type': 'early',
                    'best_label': best_label,
                    'best_price': best_price,
                    'signal_best_label': signal_best_label,
                    'signal_best_probability': signal_best_probability,
                    'price_source': source,
                    'signal': signal,
                },
            )

        if signal_best_probability < self.early_signal_min_probability:
            return StrategyDecision(
                False,
                f"early entry blocked: spot BTC signal too weak ({signal_best_probability:.3f} < {self.early_signal_min_probability:.3f})",
                seconds_to_resolution=seconds_left,
                details={
                    'entry_type': 'early',
                    'best_label': best_label,
                    'best_price': best_price,
                    'signal_best_label': signal_best_label,
                    'signal_best_probability': signal_best_probability,
                    'price_source': source,
                    'signal': signal,
                },
            )

        return StrategyDecision(
            True,
            'early entry criteria met',
            chosen_outcome=best_label,
            chosen_token_id=token_ids[best_idx],
            chosen_price=best_price,
            chosen_outcome_index=best_idx,
            seconds_to_resolution=seconds_left,
            details={
                'entry_type': 'early',
                'best_label': best_label,
                'best_price': best_price,
                'signal_best_label': signal_best_label,
                'signal_best_probability': signal_best_probability,
                'price_source': source,
                'signal': signal,
            },
        )
