from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL

from bot.fees import estimate_crypto_taker_fee_usdc, estimate_fee_shares_on_buy
from bot.state import Journal, TradeRecord


@dataclass
class ExecutionResult:
    ok: bool
    status: str
    details: dict[str, Any]
    trade_id: str | None = None


class BaseExecutor:
    def __init__(self, journal: Journal, trade_size_usd: float, max_worst_price: float, min_liquidity_on_best_level: float, stop_loss_price: float, take_profit_price: float):
        self.journal = journal
        self.trade_size_usd = trade_size_usd
        self.max_worst_price = max_worst_price
        self.min_liquidity_on_best_level = min_liquidity_on_best_level
        self.stop_loss_price = stop_loss_price
        self.take_profit_price = take_profit_price

    def execute(self, market: dict[str, Any], token_id: str, outcome: str, outcome_index: int, ref_price: float) -> ExecutionResult:
        raise NotImplementedError

    def stop_loss_exit(self, trade: dict[str, Any], market_price: float) -> ExecutionResult:
        raise NotImplementedError

    def take_profit_exit(self, trade: dict[str, Any], market_price: float) -> ExecutionResult:
        raise NotImplementedError

    def _base_trade_record(self, market: dict[str, Any], token_id: str, outcome: str, outcome_index: int, price: float, status: str, details: dict[str, Any]) -> TradeRecord:
        fee_usdc = estimate_crypto_taker_fee_usdc(self.trade_size_usd, price) if market.get('feesEnabled') else 0.0
        fee_shares = estimate_fee_shares_on_buy(self.trade_size_usd, price) if market.get('feesEnabled') else 0.0
        gross_shares = self.trade_size_usd / max(price, 1e-9)
        net_shares = max(gross_shares - fee_shares, 0.0)
        return TradeRecord(
            mode='live' if details.get('live') else 'paper',
            market_slug=market['slug'],
            market_question=market['question'],
            condition_id=market['conditionId'],
            token_id=token_id,
            outcome=outcome,
            outcome_index=outcome_index,
            entry_price=price,
            amount_usd=self.trade_size_usd,
            shares_gross=gross_shares,
            shares_net=net_shares,
            entry_fee_usdc_est=fee_usdc,
            entry_fee_shares_est=fee_shares,
            end_date=market['endDate'],
            fees_enabled=bool(market.get('feesEnabled')),
            status=status,
            response_status=details.get('response_status'),
            order_id=details.get('order_id'),
            details=details,
        )


class PaperExecutor(BaseExecutor):
    def execute(self, market: dict[str, Any], token_id: str, outcome: str, outcome_index: int, ref_price: float) -> ExecutionResult:
        details = {
            'ref_price': ref_price,
            'worst_price_cap': min(self.max_worst_price, max(ref_price, 0.01)),
            'simulated': True,
            'live': False,
            'stop_loss_price': self.stop_loss_price,
            'take_profit_price': self.take_profit_price,
        }
        record = self._base_trade_record(market, token_id, outcome, outcome_index, ref_price, 'simulated', details)
        trade_id = self.journal.add_trade(record)
        return ExecutionResult(True, 'simulated', details, trade_id=trade_id)

    def take_profit_exit(self, trade: dict[str, Any], market_price: float) -> ExecutionResult:
        exit_price = float(market_price)
        payout = float(trade['shares_net']) * exit_price
        gross_pnl = payout - float(trade['amount_usd']) + float(trade.get('entry_fee_usdc_est') or 0)
        net_pnl = payout - float(trade['amount_usd'])
        details = dict(trade.get('details') or {})
        details['take_profit'] = {
            'trigger_price': float(market_price),
            'exit_price': exit_price,
            'mode': 'paper',
        }
        updates = {
            'settled_at': datetime.now(timezone.utc).isoformat(),
            'settlement_source': 'paper_take_profit',
            'payout_usdc': round(payout, 6),
            'gross_pnl_usdc': round(gross_pnl, 6),
            'net_pnl_usdc': round(net_pnl, 6),
            'status': 'take_profit',
            'details': details,
        }
        self.journal.update_trade(trade['trade_id'], updates)
        return ExecutionResult(True, 'take_profit', updates, trade_id=trade['trade_id'])

    def stop_loss_exit(self, trade: dict[str, Any], market_price: float) -> ExecutionResult:
        exit_price = float(self.stop_loss_price)
        payout = float(trade['shares_net']) * exit_price
        gross_pnl = payout - float(trade['amount_usd']) + float(trade.get('entry_fee_usdc_est') or 0)
        net_pnl = payout - float(trade['amount_usd'])
        details = dict(trade.get('details') or {})
        details['stop_loss'] = {
            'trigger_price': float(market_price),
            'exit_price': exit_price,
            'mode': 'paper',
        }
        updates = {
            'settled_at': datetime.now(timezone.utc).isoformat(),
            'settlement_source': 'paper_stop_loss',
            'payout_usdc': round(payout, 6),
            'gross_pnl_usdc': round(gross_pnl, 6),
            'net_pnl_usdc': round(net_pnl, 6),
            'status': 'stopped_out',
            'details': details,
        }
        self.journal.update_trade(trade['trade_id'], updates)
        return ExecutionResult(True, 'stopped_out', updates, trade_id=trade['trade_id'])


class LiveExecutor(BaseExecutor):
    def __init__(
        self,
        journal: Journal,
        trade_size_usd: float,
        max_worst_price: float,
        min_liquidity_on_best_level: float,
        stop_loss_price: float,
        take_profit_price: float,
        host: str,
        chain_id: int,
        private_key: str,
        funder_address: str,
        signature_type: int,
    ):
        super().__init__(journal, trade_size_usd, max_worst_price, min_liquidity_on_best_level, stop_loss_price, take_profit_price)
        self.client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder_address,
        )
        creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(creds)

    def _field(self, obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _safe_float(self, value: Any) -> float | None:
        try:
            if value is None or value == '':
                return None
            return float(value)
        except Exception:
            return None

    def _extract_best_ask(self, asks: list[Any]) -> tuple[float | None, float]:
        best_price = None
        best_size = 0.0
        for level in asks or []:
            price = self._safe_float(self._field(level, 'price'))
            size = self._safe_float(self._field(level, 'size')) or 0.0
            if price is None:
                continue
            if best_price is None or price < best_price:
                best_price = price
                best_size = size
        return best_price, best_size

    def _extract_best_bid(self, bids: list[Any]) -> tuple[float | None, float]:
        best_price = None
        best_size = 0.0
        for level in bids or []:
            price = self._safe_float(self._field(level, 'price'))
            size = self._safe_float(self._field(level, 'size')) or 0.0
            if price is None:
                continue
            if best_price is None or price > best_price:
                best_price = price
                best_size = size
        return best_price, best_size

    def preflight(self, token_id: str, ref_price: float) -> dict[str, Any]:
        tick_size = self.client.get_tick_size(token_id)
        neg_risk = self.client.get_neg_risk(token_id)
        book = self.client.get_order_book(token_id)
        asks = self._field(book, 'asks', []) or []
        best_ask, best_ask_size = self._extract_best_ask(asks)
        return {
            'tick_size': tick_size,
            'neg_risk': neg_risk,
            'best_ask': best_ask,
            'best_ask_size': best_ask_size,
            'book_summary': {
                'market': self._field(book, 'market'),
                'asset_id': self._field(book, 'asset_id'),
                'tick_size': self._field(book, 'tick_size'),
                'min_order_size': self._field(book, 'min_order_size'),
            },
            'ref_price': ref_price,
        }

    def stop_loss_preflight(self, token_id: str) -> dict[str, Any]:
        tick_size = self.client.get_tick_size(token_id)
        neg_risk = self.client.get_neg_risk(token_id)
        book = self.client.get_order_book(token_id)
        bids = self._field(book, 'bids', []) or []
        best_bid, best_bid_size = self._extract_best_bid(bids)
        return {
            'tick_size': tick_size,
            'neg_risk': neg_risk,
            'best_bid': best_bid,
            'best_bid_size': best_bid_size,
            'book_summary': {
                'market': self._field(book, 'market'),
                'asset_id': self._field(book, 'asset_id'),
                'tick_size': self._field(book, 'tick_size'),
                'min_order_size': self._field(book, 'min_order_size'),
            },
        }

    def execute(self, market: dict[str, Any], token_id: str, outcome: str, outcome_index: int, ref_price: float) -> ExecutionResult:
        pre = self.preflight(token_id, ref_price)
        best_ask = pre['best_ask']
        if best_ask is None:
            return ExecutionResult(False, 'no_ask_liquidity', pre)
        if best_ask > self.max_worst_price:
            return ExecutionResult(False, 'ask_above_worst_price_cap', pre)
        if pre['best_ask_size'] < self.min_liquidity_on_best_level:
            return ExecutionResult(False, 'insufficient_best_ask_liquidity', pre)

        mo = MarketOrderArgs(
            token_id=token_id,
            amount=self.trade_size_usd,
            side=BUY,
            price=float(best_ask),
            order_type=OrderType.FOK,
        )
        signed = self.client.create_market_order(
            mo,
            PartialCreateOrderOptions(
                tick_size=pre['tick_size'],
                neg_risk=pre['neg_risk'],
            ),
        )
        resp = self.client.post_order(signed, OrderType.FOK)
        details = {
            'response': resp,
            'ref_price': ref_price,
            'best_ask': best_ask,
            'best_ask_size': pre['best_ask_size'],
            'tick_size': pre['tick_size'],
            'neg_risk': pre['neg_risk'],
            'response_status': resp.get('status') if isinstance(resp, dict) else None,
            'order_id': resp.get('orderID') if isinstance(resp, dict) else None,
            'live': True,
            'stop_loss_price': self.stop_loss_price,
            'take_profit_price': self.take_profit_price,
        }
        record = self._base_trade_record(market, token_id, outcome, outcome_index, best_ask, str(resp.get('status', 'submitted')) if isinstance(resp, dict) else 'submitted', details)
        trade_id = self.journal.add_trade(record)
        return ExecutionResult(True, 'submitted', details, trade_id=trade_id)

    def take_profit_exit(self, trade: dict[str, Any], market_price: float) -> ExecutionResult:
        pre = self.stop_loss_preflight(trade['token_id'])
        best_bid = pre['best_bid']
        shares_to_sell = float(trade['shares_net'])
        if best_bid is None:
            return ExecutionResult(False, 'no_bid_liquidity', pre)
        if pre['best_bid_size'] < shares_to_sell:
            return ExecutionResult(False, 'insufficient_best_bid_liquidity', pre)

        mo = MarketOrderArgs(
            token_id=trade['token_id'],
            amount=shares_to_sell,
            side=SELL,
            price=float(best_bid),
            order_type=OrderType.FOK,
        )
        signed = self.client.create_market_order(
            mo,
            PartialCreateOrderOptions(
                tick_size=pre['tick_size'],
                neg_risk=pre['neg_risk'],
            ),
        )
        resp = self.client.post_order(signed, OrderType.FOK)
        payout = shares_to_sell * float(best_bid)
        gross_pnl = payout - float(trade['amount_usd']) + float(trade.get('entry_fee_usdc_est') or 0)
        net_pnl = payout - float(trade['amount_usd'])
        details = dict(trade.get('details') or {})
        details['take_profit'] = {
            'trigger_price': float(market_price),
            'exit_price': float(best_bid),
            'best_bid_size': pre['best_bid_size'],
            'response': resp,
            'mode': 'live',
        }
        updates = {
            'settled_at': datetime.now(timezone.utc).isoformat(),
            'settlement_source': 'live_take_profit',
            'payout_usdc': round(payout, 6),
            'gross_pnl_usdc': round(gross_pnl, 6),
            'net_pnl_usdc': round(net_pnl, 6),
            'status': 'take_profit',
            'details': details,
        }
        self.journal.update_trade(trade['trade_id'], updates)
        return ExecutionResult(True, 'take_profit', updates, trade_id=trade['trade_id'])

    def stop_loss_exit(self, trade: dict[str, Any], market_price: float) -> ExecutionResult:
        pre = self.stop_loss_preflight(trade['token_id'])
        best_bid = pre['best_bid']
        shares_to_sell = float(trade['shares_net'])
        if best_bid is None:
            return ExecutionResult(False, 'no_bid_liquidity', pre)
        if pre['best_bid_size'] < shares_to_sell:
            return ExecutionResult(False, 'insufficient_best_bid_liquidity', pre)

        mo = MarketOrderArgs(
            token_id=trade['token_id'],
            amount=shares_to_sell,
            side=SELL,
            price=float(best_bid),
            order_type=OrderType.FOK,
        )
        signed = self.client.create_market_order(
            mo,
            PartialCreateOrderOptions(
                tick_size=pre['tick_size'],
                neg_risk=pre['neg_risk'],
            ),
        )
        resp = self.client.post_order(signed, OrderType.FOK)
        payout = shares_to_sell * float(best_bid)
        gross_pnl = payout - float(trade['amount_usd']) + float(trade.get('entry_fee_usdc_est') or 0)
        net_pnl = payout - float(trade['amount_usd'])
        details = dict(trade.get('details') or {})
        details['stop_loss'] = {
            'trigger_price': float(market_price),
            'exit_price': float(best_bid),
            'best_bid_size': pre['best_bid_size'],
            'response': resp,
            'mode': 'live',
        }
        updates = {
            'settled_at': datetime.now(timezone.utc).isoformat(),
            'settlement_source': 'live_stop_loss',
            'payout_usdc': round(payout, 6),
            'gross_pnl_usdc': round(gross_pnl, 6),
            'net_pnl_usdc': round(net_pnl, 6),
            'status': 'stopped_out',
            'details': details,
        }
        self.journal.update_trade(trade['trade_id'], updates)
        return ExecutionResult(True, 'stopped_out', updates, trade_id=trade['trade_id'])
